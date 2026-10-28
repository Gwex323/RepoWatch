"""Fresh, owned feature worktrees; never operate on the running checkout."""

import base64
import os
import re
import subprocess
import time
from types import SimpleNamespace

from .maintenance_target import REPOSITORY
from .repair_workspace import RepairWorkspace, digest, source_tree
from .store import Conflict


def git(directory, args, token, check):
    check()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
        }
    )
    command = [
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.autocrlf=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *args,
    ]
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    end = time.monotonic() + 120
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                check()
                if time.monotonic() >= end:
                    raise Conflict("maintenance_git_timeout")
        check()
        if process.returncode:
            raise Conflict("maintenance_git_failed")
        return output.decode("utf-8").strip()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def create_workspace(execution, task, token, check):
    if (
        not re.fullmatch(r"maintenance_[a-f0-9]{16}", task["id"])
        or not task.get("issue")
        or task["issue"]["number"] < 1
        or task["branch"] != "feature/" + task["id"].replace("_", "-")
        or not re.fullmatch(r"[a-f0-9]{40}", task["base"])
    ):
        raise Conflict("maintenance_workspace_binding_invalid")
    root = execution.root / "system-maintenance"
    for path in (execution.root, root, root / task["id"]):
        if path.is_symlink() or not path.resolve().is_relative_to(execution.root.resolve()):
            raise Conflict("maintenance_workspace_unsafe")
    root.mkdir(exist_ok=True)
    parent = root / task["id"]
    # Never reset/reuse an interrupted or user-modified checkout.
    parent.mkdir(exist_ok=False)
    bare, checkout = parent / "repository.git", parent / "checkout"
    bare.mkdir()
    checkout.mkdir()
    git(bare, ["init", "--bare"], token, check)
    git(
        bare,
        [
            "fetch",
            "--depth=1",
            "--no-tags",
            "https://github.com/" + REPOSITORY + ".git",
            "refs/heads/" + task["default_branch"],
        ],
        token,
        check,
    )
    if git(bare, ["rev-parse", "FETCH_HEAD"], token, check) != task["base"]:
        raise Conflict("maintenance_upstream_changed")
    source = checkout / "source"
    git(bare, ["worktree", "add", "-b", task["branch"], str(source), "FETCH_HEAD"], token, check)
    if git(source, ["rev-parse", "HEAD"], token, check) != task["base"] or git(
        source, ["status", "--porcelain"], token, check
    ):
        raise Conflict("maintenance_workspace_changed")
    runs = parent / "runs"
    runs.mkdir()
    facade = SimpleNamespace(root=runs, runtime=execution.runtime, directory=lambda _: checkout)
    env = {
        "id": task["id"],
        "config": {
            "language": "python",
            "install": "pip install '.[dev]'",
            "test": "python -m pytest -q",
            "install_network": True,
            "run_mode": "test_only",
            "start": "",
            "port": 8000,
        },
    }
    workspace = RepairWorkspace(facade, task["id"].replace("maintenance_", "repair_"), env, check)
    return workspace


def approved_artifacts(workspace, review):
    """Approval binds the exact tested tree, not just the latest model JSON."""
    workspace.check()
    if not workspace.patch or not workspace.review_history or not workspace.verification_history:
        raise Conflict("maintenance_review_required")
    patch = workspace.patch.model_dump()
    patch_digest = digest(workspace.patch.model_dump_json().encode())
    last = workspace.review_history[-1]
    verified = workspace.verification_history[-1]
    if (
        not review.get("approved")
        or not workspace.review
        or not workspace.review.get("approved")
        or review != workspace.review
        or last["review"] != review
        or last["revision"] != workspace.revision
        or last["patch_digest"] != patch_digest
        or not workspace.verification
        or not workspace.verification.get("passed")
        or verified["revision"] != workspace.revision
        or verified["patch"] != patch
        or verified["verification"] != workspace.verification
        or review.get("handoffs") != ["repair_agent -> independent_reviewer"]
    ):
        raise Conflict("maintenance_review_stale")
    if any(
        not (
            edit["path"].startswith("repowatch/")
            and edit["path"].endswith(".py")
            or edit["path"].startswith("tests/")
            and edit["path"].endswith(".py")
        )
        for edit in patch["edits"]
    ):
        raise Conflict("maintenance_verifier_scope_unsupported")
    if source_tree(workspace.original, workspace.check) != workspace.files:
        raise Conflict("maintenance_source_changed")
    expected = dict(workspace.files)
    for edit in patch["edits"]:
        expected[edit["path"]] = digest(edit["content"].encode())
    if source_tree(workspace.candidate, workspace.check) != expected:
        raise Conflict("maintenance_candidate_changed")
    for key in ("original_suite", "regression_before", "regression_after", "full_suite_after"):
        if key not in workspace.verification:
            raise Conflict("maintenance_verification_incomplete")
    if workspace.verification["original_suite"]["exit_code"] != 0:
        raise Conflict("maintenance_baseline_not_passing")
    return {
        "patch": patch,
        "diff": workspace.diff,
        "verification": workspace.verification,
        "review": review,
        "approved_digest": patch_digest,
        "candidate_digest": digest(str(sorted(expected.items())).encode()),
        "review_history": workspace.review_history,
        "verification_history": workspace.verification_history,
    }
