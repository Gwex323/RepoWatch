"""Validated edits and isolated Docker regression evidence; never execute on the host."""

import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .diagnosis import redact
from .repository_source import MAX_FILES, MAX_SOURCE, ExecutionError
from .source_safety import source_problem
from .store import Conflict

BLOCKED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "dist", "build", "data", "__pycache__",
}
BUILD_DOTFILES = {
    ".github", ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
    ".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.cjs",
    ".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierignore",
    ".pre-commit-config.yaml", ".flake8", ".babelrc", ".browserslistrc",
}
MAX_FILE_BYTES = None
SOURCE_READ_POLICY = "recoverable_source_reads_v1"


class RepairPathRejected(Conflict):
    def __init__(self, reason):
        super().__init__("repair_unsafe_path")
        self.details = {"reason": reason}
        self.recoverable = False


class SourceReadUnavailable(Conflict):
    def __init__(self, name, candidate, reason):
        super().__init__("repair_file_unavailable")
        self.details = {"path": name, "candidate": candidate, "reason": reason}


class SourceContentBlocked(Conflict):
    def __init__(self, name, phase, problem):
        super().__init__("repair_sensitive_content")
        self.details = {"path": name, "phase": phase, **problem}


class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=180)
    original_sha256: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    content: str = Field()


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1500)
    edits: list[FileEdit] = Field(min_length=2)
    regression_paths: list[str] = Field(min_length=1)
    regression_command: str = Field(min_length=1, max_length=4000)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative_name(name):
    parts = name.split("/")
    if (
        len(name) > 180 or "\\" in name or ":" in name or name.startswith("/")
        or any(not part or part in (".", "..") or part.endswith((" ", "."))
               or (part.startswith(".") and part not in BUILD_DOTFILES)
               or re.search(r"[\x00-\x1f\x7f]", part)
               or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", part)
               for part in parts)
    ):
        raise RepairPathRejected("invalid_path")
    if any(part.casefold() in BLOCKED_PARTS for part in parts):
        raise RepairPathRejected("protected_directory")
    if any(word in name.casefold() for word in ("secret", "credential", "token", "password")):
        raise RepairPathRejected("sensitive_name")
    return parts


def safe_path(root, name):
    parts = safe_relative_name(name)
    path = root.joinpath(*parts)
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise RepairPathRejected("symlink")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RepairPathRejected("outside_root")
    return path


def test_path(name):
    path = Path(name)
    lower = path.name.lower()
    return (any(part.lower() in ("tests", "test", "__tests__") for part in path.parts[:-1])
            or lower.startswith("test") and lower.endswith(".py")
            or lower.endswith(("_test.go", ".test.ts", ".test.js", ".test.tsx", ".test.jsx",
                               ".spec.ts", ".spec.js", ".spec.tsx", ".spec.jsx", "_test.py")))


def source_tree(root, check=lambda: None):
    """Hash regular files without following links, for immutable-copy validation."""
    result, folded, total = {}, set(), 0
    if not root.is_dir() or root.is_symlink():
        raise Conflict("repair_source_missing")
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            check()
            path = Path(directory) / name
            if path.is_symlink():
                raise Conflict("repair_unsafe_path")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in folded or not path.is_file():
                raise Conflict("repair_unsafe_path")
            folded.add(relative.casefold())
            hashed = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    check()
                    total += len(chunk)
                    if total > MAX_SOURCE:
                        raise Conflict("repair_source_limit")
                    hashed.update(chunk)
            result[relative] = hashed.hexdigest()
            if len(result) > MAX_FILES:
                raise Conflict("repair_source_limit")
    return result


class RepairWorkspace:
    def __init__(self, execution, task_id, env, check, *, attempt=1):
        self.execution, self.env, self.check = execution, env, check
        if not re.fullmatch(r"repair_[a-f0-9]{16}", task_id):
            raise Conflict("repair_changed")
        parent = execution.root / task_id
        if parent.is_symlink() or not parent.resolve().is_relative_to(execution.root):
            raise Conflict("repair_unsafe_path")
        parent.mkdir(exist_ok=True)
        self.id = task_id + f"-a{attempt}"
        self.root = parent / f"attempt-{attempt}"
        # A failed attempt is retained. Every new local attempt uses a fresh directory.
        self.root.mkdir(exist_ok=False)
        self.original = self.root / "original"
        source = execution.directory(env["id"]) / "source"
        before = source_tree(source, check)
        shutil.copytree(source, self.original, symlinks=False)
        if before != source_tree(source, check) or before != source_tree(self.original, check):
            raise Conflict("repair_source_changed")
        self.files = before
        self.reads = {}
        self.read_count = 0
        self.read_chars = 0
        self.additional_profiles = []
        self.patch = None
        self.diff = ""
        self.verification = None
        self.revision = 1
        self.verification_history = []
        self.review = None
        self.review_history = []
        self.prior_rejected_patches = []
        self.candidate = self.root / "candidate"
        self.baseline = self.root / "baseline"
        self.compatibility = self.root / "compatibility"

    @property
    def ready_for_review(self):
        return bool(self.verification and self.verification.get("passed")
                    and self.review is None)

    def record_review(self, review):
        """Record a validated independent SDK conclusion for the current candidate."""
        self.check()
        if not self.ready_for_review:
            raise Conflict("repair_review_evidence_required")
        if (review.get("approved")
                and self.verification.get("original_suite_after", {}).get("exit_code") != 0
                and not str(review.get("original_suite_rationale", "")).strip()):
            raise Conflict("repair_review_evidence_required")
        if (review.get("approved") and any(
            self.verification.get(profile["name"] + "_original", {}).get("exit_code") != 0
            for profile in self.additional_profiles
        ) and not str(review.get("baseline_rationale", "")).strip()):
            raise Conflict("repair_review_evidence_required")
        changed_tests = {
            edit.path for edit in self.patch.edits
            if edit.path in self.files
            and (test_path(edit.path) or Path(edit.path).name == "conftest.py")
            and digest(edit.content.encode()) != self.files[edit.path]
        }
        if review.get("approved") and changed_tests:
            explained = review.get("test_changes", [])
            if (len(explained) != len(changed_tests)
                    or {item.get("path") for item in explained} != changed_tests
                    or any(not str(item.get("rationale", "")).strip() for item in explained)):
                raise Conflict("repair_review_evidence_required")
        self.review = copy.deepcopy(review)
        self.review_history.append({
            "revision": self.revision,
            "patch_digest": digest(self.patch.model_dump_json().encode()),
            "review": copy.deepcopy(review),
        })

    def index(self):
        eligible = []
        for name in sorted(self.files):
            try:
                path = safe_path(self.original, name)
                if path.is_file():
                    eligible.append(name)
            except (Conflict, OSError):
                continue
        return {"files": eligible[:600], "truncated": len(eligible) > 600}

    @property
    def source_read_budget_low(self):
        """Compatibility property: source reads no longer exhaust a task quota."""
        return False

    def read(self, name, *, candidate=False):
        self.check()
        self.read_count += 1
        root = self.candidate if candidate else self.original
        path = safe_path(root, name)
        if candidate and not root.is_dir():
            raise SourceReadUnavailable(name, candidate, "candidate_not_ready")
        if not candidate and name not in self.files:
            raise SourceReadUnavailable(name, candidate, "not_found")
        try:
            if not path.is_file():
                raise SourceReadUnavailable(name, candidate, "not_found")
            raw = path.read_bytes()
        except OSError:
            raise SourceReadUnavailable(name, candidate, "read_error") from None
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise SourceReadUnavailable(name, candidate, "invalid_utf8") from None
        self.read_chars += len(text)
        problem = source_problem(text, name)
        if problem:
            raise SourceContentBlocked(name, "read", problem)
        if not candidate:
            self.reads[name] = digest(raw)
        return {"path": name, "sha256": digest(raw), "text": text}

    def prepare(self, patch, *, revise=False):
        self.check()
        if self.patch is not None:
            rejected = bool(self.review and self.review.get("approved") is False)
            if (not revise or not self.verification
                    or (self.verification.get("passed") is not False and not rejected)):
                raise Conflict("repair_patch_already_submitted")
        names = [edit.path for edit in patch.edits]
        if len({name.casefold() for name in names}) != len(names):
            raise RepairPathRejected("duplicate_path")
        regression = set(patch.regression_paths)
        if len(regression) != len(patch.regression_paths) or not regression.issubset(names):
            raise Conflict("repair_regression_required")
        source_changes = 0
        for index, edit in enumerate(patch.edits):
            try:
                path = safe_path(self.original, edit.path)
            except RepairPathRejected as exc:
                exc.details["edit_index"] = index
                exc.recoverable = (
                    exc.details["reason"] == "sensitive_name"
                    and edit.path in regression and not edit.original_sha256
                    and edit.path not in self.files and test_path(edit.path)
                )
                raise
            raw = edit.content.encode()
            problem = source_problem(edit.content, edit.path)
            if problem:
                raise SourceContentBlocked(edit.path, "patch", problem)
            if edit.path in regression:
                if (path.exists() or edit.original_sha256 or not test_path(edit.path)
                        or path.name == "__init__.py"):
                    raise Conflict("repair_regression_required")
            else:
                if edit.path in self.files:
                    if not path.is_file():
                        raise Conflict("repair_source_changed")
                    actual = digest(path.read_bytes())
                    if self.reads.get(edit.path) != actual or edit.original_sha256 != actual:
                        raise Conflict("repair_source_changed")
                    if digest(raw) != actual:
                        source_changes += 1
                elif edit.original_sha256 or path.exists():
                    raise Conflict("repair_source_changed")
                else:
                    source_changes += 1
            # Reject new names that alias an existing file on Windows.
            if any(name.casefold() == edit.path.casefold() and name != edit.path
                   for name in self.files):
                raise RepairPathRejected("case_alias")
        if source_changes == 0 or "\0" in patch.regression_command:
            raise Conflict("repair_regression_required")
        if self.patch is not None or self.prior_rejected_patches:
            next_edits = {e.path: (e.original_sha256, e.content) for e in patch.edits}
            previous_patches = [*self.prior_rejected_patches,
                                *(item["patch"] for item in self.verification_history)]
            if self.patch:
                previous_patches.append(self.patch.model_dump())
            for previous in previous_patches:
                previous_edits = {e["path"]: (e["original_sha256"], e["content"])
                                  for e in previous["edits"]}
                if (next_edits == previous_edits
                        and set(patch.regression_paths) == set(previous["regression_paths"])
                        and patch.regression_command.strip()
                        == previous["regression_command"].strip()):
                    raise Conflict("repair_patch_unchanged")
        if self.patch is not None:
            # Keep prior candidates and test evidence intact. Every revision is
            # built from the same original, never from the failed candidate.
            next_revision = self.revision + 1
            revision_root = self.root / f"revision-{next_revision}"
            revision_root.mkdir(exist_ok=False)
            self.revision = next_revision
            self.baseline = revision_root / "baseline"
            self.candidate = revision_root / "candidate"
            self.compatibility = revision_root / "compatibility"
            self.id = self.id.split("-r", 1)[0] + f"-r{self.revision}"
        self.patch = patch
        self.diff = ""
        self.verification = None
        self.review = None
        shutil.copytree(self.original, self.baseline)
        shutil.copytree(self.original, self.candidate)
        fragments = []
        for edit in patch.edits:
            for root in ([self.baseline, self.candidate] if edit.path in regression
                         else [self.candidate]):
                target = safe_path(root, edit.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(edit.content.encode())
            old = (self.original / edit.path).read_text(encoding="utf-8") \
                if edit.path in self.files else ""
            fragments.extend(difflib.unified_diff(
                old.splitlines(keepends=True), edit.content.splitlines(keepends=True),
                fromfile="a/" + edit.path if edit.path in self.files else "/dev/null",
                tofile="b/" + edit.path,
            ))
        self.diff = "".join(fragments)
        shutil.copytree(self.candidate, self.compatibility)
        # Untouched tests are already present in the copied candidate. Restore
        # only validated edits, so unrelated protected filenames remain intact.
        for edit in patch.edits:
            name = edit.path
            if name in self.files and (test_path(name) or Path(name).name == "conftest.py"):
                target = safe_path(self.compatibility, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(safe_path(self.original, name), target)


    def verify(self, event):
        runtime = self.execution.runtime
        ready = runtime.readiness()
        if not ready["ready"]:
            raise ExecutionError(ready["reason"])
        image = runtime.image(self.env["config"]["language"], self.check)
        results = {}
        trials = [
            ("original_suite", self.original, [("original_suite", self.env["config"]["test"])]),
            ("baseline", self.baseline, [("regression_before", self.patch.regression_command)]),
            ("compatibility", self.compatibility, [
                ("original_suite_after", self.env["config"]["test"]),
            ]),
            ("candidate", self.candidate, [
                ("regression_after", self.patch.regression_command),
                ("full_suite_after", self.env["config"]["test"]),
            ]),
        ]
        for index, (label, source, commands) in enumerate(trials):
            env = {**self.env, "id": self.id + f"-{index}",
                   "image_id": image, "state": "ready", "config": dict(self.env["config"])}
            event("testing", label)
            try:
                runtime.cleanup(env)
                runtime.seed(env, source, self.check)
                code, log = runtime.run(env, "setup", self.check)
                results[label + "_setup"] = {"exit_code": code, "log": redact(log)[-16000:]}
                if code:
                    raise Conflict("repair_setup_failed")
                for key, command in commands:
                    self.check()
                    env["config"]["test"] = command
                    code, log = runtime.run(env, "test", self.check)
                    results[key] = {"exit_code": code, "log": redact(log)[-16000:],
                                    "command": command}
            finally:
                self.verification = results
                failing = sys.exc_info()[0] is not None
                try:
                    runtime.cleanup(env)
                except Exception:
                    event("cleanup_failed", label)
                    if not failing:
                        raise
        # These profiles are supplied by the trusted maintenance adapter, not the
        # repair model. Each language uses its own isolated runtime and installs
        # dependencies from the exact original/candidate manifests being checked.
        for profile in self.additional_profiles:
            for variant, source in (("original", self.original), ("candidate", self.candidate)):
                key = profile["name"] + "_" + variant
                env = {**self.env, "id": self.id + "-" + key,
                       "image_id": runtime.image(profile["config"]["language"], self.check),
                       "state": "ready", "config": dict(profile["config"])}
                event("testing", key)
                try:
                    runtime.cleanup(env)
                    runtime.seed(env, source, self.check)
                    code, log = runtime.run(env, "setup", self.check)
                    results[key + "_setup"] = {"exit_code": code, "log": redact(log)[-16000:]}
                    if code:
                        results[key] = {"exit_code": code, "executed": False,
                                        "log": "Setup failed; verification command was not run.",
                                        "command": env["config"]["test"]}
                    else:
                        code, log = runtime.run(env, "test", self.check)
                        results[key] = {"exit_code": code, "executed": True,
                                        "log": redact(log)[-16000:],
                                        "command": env["config"]["test"]}
                finally:
                    self.verification = results
                    failing = sys.exc_info()[0] is not None
                    try:
                        runtime.cleanup(env)
                    except Exception:
                        event("cleanup_failed", key)
                        if not failing:
                            raise
        results["passed"] = (
            results["regression_before"]["exit_code"] not in (0, 5, 126, 127, 137, 143)
            and results["regression_after"]["exit_code"] == 0
            and results["full_suite_after"]["exit_code"] == 0
            and all(results[p["name"] + "_candidate"]["exit_code"] == 0
                    for p in self.additional_profiles)
            and not any(re.search(
                r"(?i)ran 0 tests|no tests ran|no tests collected|collected 0 items"
                r"|\[no test files\]|# tests 0(?:\D|$)",
                results[key]["log"],
            ) for key in ("regression_before", "regression_after", "full_suite_after"))
        )
        self.verification = results
        self.verification_history.append({
            "revision": self.revision, "patch": self.patch.model_dump(),
            "diff": self.diff, "verification": copy.deepcopy(results),
        })
        event("verification_round", json.dumps({
            "revision": self.revision, "passed": results["passed"],
        }))
        if not results["passed"]:
            raise Conflict("repair_tests_failed")
        return results
