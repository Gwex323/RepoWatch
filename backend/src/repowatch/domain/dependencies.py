import json
import re
import tomllib
from hashlib import sha256
from pathlib import PurePosixPath


def dependency_plan(objects, repository_id, manifest):
    paths = {entry["path"]: entry for entry in manifest["entries"]}
    steps, gaps = [], []
    locks = []
    dependency_files = {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "package.json",
    }
    blobs = {}
    for path, entry in paths.items():
        if PurePosixPath(path).name not in dependency_files:
            continue
        content = objects.blob(repository_id, entry["oid"])
        blobs[path] = content
        decoded = content.decode("utf-8", "replace")
        if re.search(r"(?:https?://[^\s/]+@|git\+ssh:|ssh://|_authToken|password\s*=)", decoded):
            gaps.append({"path": path, "reason": "private_dependency_requires_host_preparation"})
        locks.append({"path": path, "oid": entry["oid"], "sha256": sha256(content).hexdigest()})

    python_manifests = {
        path
        for path in blobs
        if PurePosixPath(path).name
        in {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "uv.lock"}
    }
    javascript_manifests = {
        path for path in blobs if PurePosixPath(path).name in {"package.json", "pnpm-lock.yaml"}
    }
    local_package_paths = set()
    if "package.json" in blobs:
        try:
            root_package = json.loads(blobs["package.json"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            root_package = {}
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for reference in root_package.get(section, {}).values():
                if isinstance(reference, str) and reference.startswith("file:"):
                    local_path = PurePosixPath(reference[5:])
                    if not local_path.is_absolute() and ".." not in local_path.parts:
                        local_package_paths.add(str(local_path / "package.json"))

    for path in sorted(python_manifests | javascript_manifests):
        if PurePosixPath(path).parent != PurePosixPath(".") and path not in local_package_paths:
            gaps.append({"path": path, "reason": "unsupported_nested_dependency_project"})

    root_python_manifests = {
        PurePosixPath(path).name
        for path in python_manifests
        if PurePosixPath(path).parent == PurePosixPath(".")
    }
    project_has_python_dependencies = False
    if "pyproject.toml" in blobs:
        try:
            project = tomllib.loads(blobs["pyproject.toml"].decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            gaps.append({"path": "pyproject.toml", "reason": "invalid_dependency_declaration"})
        else:
            project_table = project.get("project", {})
            project_has_python_dependencies = bool(
                project_table.get("dependencies")
                or project_table.get("optional-dependencies")
                or project.get("dependency-groups")
            )
    if "uv.lock" in root_python_manifests:
        steps.append(["uv", "sync", "--frozen", "--offline", "--all-groups"])
    else:
        requirements = [
            name
            for name in ("requirements.txt", "requirements-dev.txt")
            if name in root_python_manifests
        ]
        for requirements_path in requirements:
            steps.append(
                [
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links=/opt/repowatch/wheels",
                    "--target=/deps/python",
                    "-r",
                    requirements_path,
                ]
            )
        if project_has_python_dependencies:
            gaps.append({"path": "pyproject.toml", "reason": "locked_dependency_plan_required"})
    if "uv.lock" in root_python_manifests and "pyproject.toml" not in root_python_manifests:
        gaps.append({"path": "uv.lock", "reason": "dependency_manifest_missing"})

    if "package.json" in blobs:
        try:
            package = json.loads(blobs["package.json"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            package = None
            gaps.append({"path": "package.json", "reason": "invalid_dependency_declaration"})
        if package is not None and (package.get("dependencies") or package.get("devDependencies")):
            if package.get("workspaces") or "pnpm-workspace.yaml" in paths:
                gaps.append({"path": "package.json", "reason": "unsupported_workspace_layout"})
            elif "pnpm-lock.yaml" in blobs:
                steps.append(
                    ["pnpm", "install", "--offline", "--frozen-lockfile", "--ignore-scripts"]
                )
            elif "package-lock.json" in blobs:
                steps.append(["npm", "ci", "--offline", "--ignore-scripts", "--no-audit"])
            else:
                gaps.append({"path": "package.json", "reason": "supported_lockfile_required"})
    elif any(PurePosixPath(path).name in {"package-lock.json", "pnpm-lock.yaml"} for path in blobs):
        gaps.append({"path": "package.json", "reason": "dependency_manifest_missing"})
    return {"version": 1, "steps": steps, "gaps": gaps, "declarations": locks}
