"""Immutable deployment bindings and an explicit, clean source tree."""

import hashlib
import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import recovery
from .release_state import existing_database, read_json, schema
from .store import Conflict

CONFIG_KEYS = {
    "REPOWATCH_LANGFUSE_ENABLED", "LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_TRACING_ENABLED", "REPOWATCH_LANGFUSE_REQUIRED",
}
SHA = r"^[a-f0-9]{40}$"


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise Conflict("release_absolute_path_required")
    return path


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(root, *args):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        capture_output=True, timeout=30, check=False,
        env={key: value for key, value in os.environ.items()
             if not key.startswith(("GIT_", "PYTHON"))},
    )
    if result.returncode:
        raise Conflict("release_git_unavailable")
    return result.stdout.decode("utf-8").strip()


def code_digest(root):
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise Conflict("release_clean_code_required")
    files = git(root, "ls-files", "-z").split("\0")
    digest = hashlib.sha256()
    for name in sorted(files):
        if not name:
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise Conflict("release_code_invalid")
        digest.update(name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def asset_files(root):
    directory = root / "dist"
    if not (directory / "index.html").is_file():
        raise Conflict("release_frontend_required")
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise Conflict("release_frontend_invalid")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = file_digest(path)
    if not files or len(files) > 300 or not any(name.startswith("assets/") for name in files):
        raise Conflict("release_frontend_invalid")
    return files


def config_values(path, digest):
    if file_digest(path) != digest:
        raise Conflict("release_configuration_changed")
    values = read_json(path)
    if not isinstance(values, dict) or not set(values) <= CONFIG_KEYS:
        raise Conflict("release_configuration_invalid")
    if any(not isinstance(value, str) or len(value) > 4096 for value in values.values()):
        raise Conflict("release_configuration_invalid")
    if values.get("REPOWATCH_LANGFUSE_ENABLED") not in {"0", "1"}:
        raise Conflict("release_explicit_monitoring_mode_required")
    enabled = values.get("REPOWATCH_LANGFUSE_ENABLED") == "1"
    if values.get("REPOWATCH_LANGFUSE_REQUIRED") == "1" and not enabled:
        raise Conflict("release_monitoring_required")
    if enabled and (not all(values.get(key) for key in
            ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
            or values.get("LANGFUSE_TRACING_ENABLED", "true").lower() in {"0", "false"}):
        raise Conflict("release_monitoring_required")
    return values


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: int = Field(default=1, ge=1, le=1)
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository: str = Field(default="Gwex323/RepoWatch", pattern=r"^Gwex323/RepoWatch$")
    pr: int = Field(gt=0)
    commit: str = Field(pattern=SHA)
    code_root: str
    python: str
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    frontend: dict[str, str]
    database: str
    database_id: str
    database_schema: dict
    config_ref: str
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_root: str
    port: int = Field(ge=1024, le=65535)
    binding: dict
    previous: str | None = None
    maintenance_task: str | None = None
    workers: bool = True

    @classmethod
    def build(cls, *, root, python, database, config_ref, state_root, port, pr,
              commit, binding, previous=None, maintenance_task=None, workers=True):
        root, python, database, config_ref, state_root = map(
            absolute, (root, python, database, config_ref, state_root))
        if git(root, "rev-parse", "HEAD") != commit:
            raise Conflict("release_commit_mismatch")
        if not python.is_file() or not (root / "scripts/managed.py").is_file():
            raise Conflict("release_entry_required")
        with existing_database(database) as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='release_database_id'").fetchone()
            if not row:
                raise Conflict("release_existing_database_id_required")
            identity, database_schema = row[0], schema(db)
        config_digest = file_digest(config_ref)
        config_values(config_ref, config_digest)
        value = {
            "format": 1, "repository": "Gwex323/RepoWatch", "pr": pr, "commit": commit,
            "code_root": str(root), "python": str(python),
            "source_digest": code_digest(root), "frontend": asset_files(root),
            "database": str(database), "database_id": identity,
            "database_schema": database_schema, "config_ref": str(config_ref),
            "config_digest": config_digest, "state_root": str(state_root),
            "port": port, "binding": binding, "previous": previous,
            "maintenance_task": maintenance_task, "workers": workers,
        }
        return cls(id=recovery.digest(value), **value)

    @classmethod
    def load(cls, path):
        result = cls.model_validate(read_json(absolute(path)))
        if result.id != recovery.digest(result.model_dump(exclude={"id"})):
            raise Conflict("release_manifest_changed")
        for field in ("code_root", "python", "database", "config_ref", "state_root"):
            absolute(getattr(result, field))
        return result

    def validate_files(self, *, rollback=False):
        root = absolute(self.code_root)
        if git(root, "rev-parse", "HEAD") != self.commit or code_digest(root) != self.source_digest:
            raise Conflict("release_code_changed")
        if asset_files(root) != self.frontend:
            raise Conflict("release_frontend_changed")
        values = config_values(absolute(self.config_ref), self.config_digest)
        with existing_database(self.database) as db:
            identity = db.execute(
                "SELECT value FROM settings WHERE key='release_database_id'").fetchone()
            if not identity or identity[0] != self.database_id:
                raise Conflict("release_database_mismatch")
            from .release_state import compatible

            if not compatible(self.database_schema, schema(db)):
                raise Conflict("release_database_incompatible")
            if rollback and schema(db) != self.database_schema:
                # Rollback needs an exact known schema; forward additions require owner review.
                raise Conflict("release_rollback_schema_changed")
        return values

    def command(self, path):
        return [self.python, "-I", str(Path(self.code_root) / "scripts/managed.py"),
                "serve", "--manifest", str(absolute(path))]
