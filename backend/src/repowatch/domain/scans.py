import json
import tomllib
from hashlib import sha256
from pathlib import PurePosixPath

from repowatch.domain.task_records import content_digest


def readiness(repository):
    return {
        key: repository[key]
        for key in ("id", "target_revision", "import_epoch", "target_sha", "scan_policy_revision")
    }


def inventory(objects, repository_id, sha, *, chunk_bytes=16000, exclusions=None):
    if type(chunk_bytes) is not int or not 256 <= chunk_bytes <= 64000:
        raise ValueError("扫描块必须在 256—64000 字节内")
    exclusions = exclusions or {}
    entries, units = [], []
    for entry in objects.entries(repository_id, sha):
        path = entry["path"]
        record = {**entry, "module": str(PurePosixPath(path).parent)}
        if entry["mode"] == "160000":
            record.update(disposition="gap", reason="submodule_content_missing")
        else:
            content = objects.blob(repository_id, entry["oid"])
            record.update(size=len(content), sha256=sha256(content).hexdigest())
            reason = exclusions.get(path)
            if reason:
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("排除项需要版本化依据")
                record.update(disposition="excluded", reason=reason)
            elif entry["mode"] == "120000":
                record.update(disposition="link", reason="link_metadata_review")
            elif content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                record.update(disposition="gap", reason="lfs_entity_missing")
            elif b"\0" in content:
                record.update(disposition="excluded", reason="binary_content_policy_v1")
            else:
                record["disposition"] = "review"
            if record["disposition"] in {"review", "link"}:
                for start in range(0, max(len(content), 1), chunk_bytes):
                    end = min(start + chunk_bytes, len(content))
                    unit = {
                        "path": path,
                        "oid": entry["oid"],
                        "start": start,
                        "end": end,
                        "module": record["module"],
                        "sha256": sha256(content[start:end]).hexdigest(),
                    }
                    unit["id"] = content_digest(unit)
                    units.append(unit)
        entries.append(record)
    unknown = set(exclusions) - {entry["path"] for entry in entries}
    if unknown:
        raise ValueError("排除策略包含不存在的路径")
    return {
        "version": 1,
        "sha": sha,
        "entries": entries,
        "units": units,
        "policy": {"chunk_bytes": chunk_bytes, "exclusions": exclusions},
    }


def checks_for(objects, repository_id, manifest):
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    checks = []
    for path in ("requirements.txt", "requirements-dev.txt"):
        if path in entries:
            content = objects.blob(repository_id, entries[path]["oid"]).decode("utf-8", "replace")
            if any(line.strip().lower().startswith("pytest") for line in content.splitlines()):
                checks.append(
                    {
                        "id": "python:pytest",
                        "argv": ["python", "-m", "pytest"],
                        "basis": path,
                    }
                )
                break
    if "package.json" in entries:
        package = json.loads(objects.blob(repository_id, entries["package.json"]["oid"]))
        for name in ("test", "lint", "typecheck", "build"):
            if name in package.get("scripts", {}):
                manager = (
                    "pnpm"
                    if "pnpm-lock.yaml" in entries
                    else "yarn"
                    if "yarn.lock" in entries
                    else "npm"
                )
                checks.append(
                    {
                        "id": "js:" + name,
                        "argv": [manager, "run", name],
                        "basis": "package.json#scripts." + name,
                    }
                )
    if "pyproject.toml" in entries:
        project = tomllib.loads(
            objects.blob(repository_id, entries["pyproject.toml"]["oid"]).decode()
        )
        dependencies = project.get("project", {}).get("dependencies", []) + [
            value
            for group in project.get("dependency-groups", {}).values()
            for value in group
            if isinstance(value, str)
        ]
        tools = project.get("tool", {})
        if not any(check["id"] == "python:pytest" for check in checks) and (
            "pytest" in tools or any(value.lower().startswith("pytest") for value in dependencies)
        ):
            checks.append(
                {
                    "id": "python:pytest",
                    "argv": ["python", "-m", "pytest"],
                    "basis": "pyproject.toml pytest declaration",
                }
            )
        if "ruff" in tools:
            checks.append(
                {
                    "id": "python:ruff",
                    "argv": ["python", "-m", "ruff", "check", "."],
                    "basis": "pyproject.toml#tool.ruff",
                }
            )
    if any(path.startswith("tests/") and path.endswith(".py") for path in entries):
        if not any(check["id"] == "python:pytest" for check in checks):
            checks.append(
                {
                    "id": "python:pytest",
                    "argv": ["python", "-m", "pytest"],
                    "basis": "tracked Python tests",
                }
            )
    if not checks:
        checks.append(
            {
                "id": "repository:no-declared-checks",
                "argv": [],
                "basis": "fixed_git_tree_without_supported_check_declaration_v1",
                "applicability": "not_applicable",
            }
        )
    return checks


def baseline_outcome(result, log):
    if (
        result.get("control_stopped", False)
        or result["timed_out"]
        or result["oom_killed"]
        or result["log_truncated"]
        or result["exit_code"] in {None, 125, 126, 127}
    ):
        return "unable"
    lowered = log.lower()
    if any(
        marker in lowered
        for marker in (
            b"no module named",
            b"command not found",
            b"modulenotfounderror",
            b"cannot find module",
            b"no tests ran",
            b"no tests found",
            b"error collecting",
            b"missing script",
        )
    ):
        return "unable"
    return "passed" if result["exit_code"] == 0 else "existing_failed"
