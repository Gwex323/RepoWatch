"""Download a fixed GitHub revision without executing repository code on the host."""

import gzip
import io
import json
import re
import tarfile
import time
import tomllib
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_SOURCE = 128 * 1024 * 1024
MAX_FILES = 20000
IMAGES = {
    "python": "python:3.13-slim-bookworm",
    "go": "golang:1.25-bookworm",
    "typescript": "node:24-bookworm-slim",
}


class ExecutionError(Exception):
    def __init__(self, code, log=""):
        super().__init__(code)
        self.log = log


def unpack_archive(content, destination):
    """Reject links, special files and nonportable names before writing anything."""
    entries, paths, total, root = [], set(), 0, None
    try:
        # Bound decompressed metadata too, including tar extended headers.
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
            expanded = compressed.read(MAX_SOURCE + MAX_FILES * 2048 + 1)
        if len(expanded) > MAX_SOURCE + MAX_FILES * 2048:
            raise ExecutionError("archive_limit")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_FILES:
                    raise ExecutionError("archive_limit")
                name = member.name.rstrip("/")
                parts = name.split("/")
                if (
                    not parts or name.startswith("/") or "\\" in name or ":" in name
                    or any(p in ("", ".", "..") or p.endswith((" ", "."))
                           or re.search(r"[\x00-\x1f\x7f]", p)
                           or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", p)
                           for p in parts)
                    or any(p.casefold() == ".git" for p in parts)
                    or len(name) > 220 or not (member.isfile() or member.isdir())
                ):
                    raise ExecutionError("unsafe_archive")
                if root is None:
                    root = parts[0]
                if parts[0] != root:
                    raise ExecutionError("unsafe_archive")
                if len(parts) == 1:
                    if not member.isdir():
                        raise ExecutionError("unsafe_archive")
                    continue
                relative = "/".join(parts[1:])
                if relative.casefold() in paths:
                    raise ExecutionError("unsafe_archive")
                paths.add(relative.casefold())
                total += member.size
                if total > MAX_SOURCE or member.size < 0:
                    raise ExecutionError("archive_limit")
                entries.append((relative, member))
            if not entries:
                raise ExecutionError("empty_archive")
            destination.mkdir(parents=True, exist_ok=False)
            for relative, member in entries:
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        output.write(source.read())
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError, ValueError, EOFError) as exc:
        raise ExecutionError("unsafe_archive") from exc


class RepositorySource:
    def __init__(self, github, transport=None):
        self.github = github
        self.transport = transport

    def download(self, repo, account_id, account_version, destination, check):
        token, _ = self.github.validated_identity(account_id, account_version)
        metadata = self.github.client.repository(token, repo["name"])
        name = metadata["repository"]
        metadata, _ = self.github.client.request(f"/repos/{name}/commits/HEAD", token)
        revision = metadata.get("sha", "")
        if not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{40}", revision):
            raise ExecutionError("invalid_revision")
        check()
        try:
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                response = client.get(
                    f"https://api.github.com/repos/{name}/tarball/{revision}",
                    headers={"Authorization": f"Bearer {token}",
                             "X-GitHub-Api-Version": "2022-11-28"},
                )
                if response.status_code != 302:
                    raise ExecutionError("download_denied")
                location = response.headers.get("location", "")
                url = urlsplit(location)
                if (
                    url.scheme != "https" or url.netloc != "codeload.github.com"
                    or url.fragment or not url.path.startswith(f"/{name}/")
                    or not url.path.endswith("/" + revision)
                ):
                    raise ExecutionError("download_redirect_rejected")
                data = bytearray()
                deadline = time.monotonic() + 180
                # Never forward the PAT to a redirect destination.
                with client.stream("GET", location) as archive:
                    if archive.status_code != 200:
                        raise ExecutionError("download_denied")
                    for chunk in archive.iter_bytes(65536):
                        check()
                        if time.monotonic() > deadline:
                            raise ExecutionError("execution_timeout")
                        data.extend(chunk)
                        if len(data) > MAX_ARCHIVE:
                            raise ExecutionError("archive_limit")
        except httpx.HTTPError:
            raise ExecutionError("download_unreachable") from None
        with self.github.store.connection() as db:
            self.github.row(db, account_id, account_version)
        check()
        unpack_archive(data, destination)
        return revision, discover(destination)


def uses_pytest(manifests):
    if re.search(r"(?m)^\s*\[pytest\]\s*$", manifests.get("pytest.ini", "")):
        return True
    project = tomllib.loads(manifests.get("pyproject.toml", ""))
    if isinstance(project.get("tool", {}).get("pytest"), dict):
        return True
    requirements = "\n".join(manifests.get(name, "") for name in (
        "requirements.txt", "requirements-dev.txt",
    ))
    if re.search(r"(?im)^\s*pytest(?:\s|[<>=!~;\[@]|$)", requirements):
        return True
    metadata = project.get("project", {})
    dependencies = metadata.get("dependencies", [])
    optional = metadata.get("optional-dependencies", {})
    if not isinstance(dependencies, list) or not isinstance(optional, dict):
        raise ValueError("invalid_python_dependencies")
    dependencies = dependencies.copy()
    for group in optional.values():
        if not isinstance(group, list):
            raise ValueError("invalid_python_dependencies")
        dependencies.extend(group)
    return any(isinstance(item, str) and re.match(
        r"(?i)^pytest(?:\s|[<>=!~;\[@]|$)", item,
    ) for item in dependencies)


def discover(source):
    def text(name):
        file = source / name
        if not file.is_file() or file.stat().st_size > 256000:
            return ""
        return file.read_text(encoding="utf-8")

    config = {
        "language": "python", "install": "", "test": "", "start": "",
        "port": 8000, "install_network": False,
    }
    languages = [lang for lang, markers in (
        ("python", ("pyproject.toml", "requirements.txt", "setup.py")),
        ("go", ("go.mod",)), ("typescript", ("package.json",)),
    ) if any((source / marker).is_file() for marker in markers)]
    if len(languages) != 1:
        return {**config, "detected": False, "reason": "ambiguous_or_unsupported"}
    language = languages[0]
    config["language"] = language
    try:
        if language == "python":
            project = tomllib.loads(text("pyproject.toml")).get("project", {})
            if not isinstance(project, dict):
                raise ValueError()
            pytest = uses_pytest({name: text(name) for name in (
                "pyproject.toml", "requirements.txt", "requirements-dev.txt", "pytest.ini",
            )})
            install = "python -m venv /workspace/home/venv"
            if (source / "requirements.txt").is_file():
                install += " && /workspace/home/venv/bin/pip install -r requirements.txt"
            if (source / "pyproject.toml").is_file() or (source / "setup.py").is_file():
                install += " && /workspace/home/venv/bin/pip install -e ."
            if pytest and (source / "requirements-dev.txt").is_file():
                install += " && /workspace/home/venv/bin/pip install -r requirements-dev.txt"
            config["install"] = install
            if pytest:
                config["test"] = "/workspace/home/venv/bin/python -m pytest"
            elif (source / "tests").is_dir():
                config["test"] = "/workspace/home/venv/bin/python -m unittest discover -s tests"
            if (source / "app.py").is_file():
                config["start"] = "/workspace/home/venv/bin/python app.py"
        elif language == "go":
            config.update(install="go mod download", test="go test ./...", start="go run .")
        else:
            manifest = json.loads(text("package.json"))
            scripts = manifest.get("scripts", {})
            if not isinstance(scripts, dict):
                raise ValueError()
            if (source / "yarn.lock").exists() or (source / "pnpm-lock.yaml").exists():
                return {**config, "detected": False, "reason": "package_manager_override"}
            config.update(
                install="npm ci" if (source / "package-lock.json").is_file() else "npm install",
                test="npm test" if "test" in scripts else "",
                start="npm start" if "start" in scripts else
                "npm run dev -- --host 0.0.0.0" if "dev" in scripts else "",
                port=3000 if "start" in scripts else 5173,
            )
    except (ValueError, UnicodeError, AttributeError):
        return {**config, "detected": False, "reason": "invalid_manifest"}
    return {**config, "detected": True, "reason": ""}
