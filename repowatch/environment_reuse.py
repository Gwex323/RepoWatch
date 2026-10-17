"""Conservative dependency snapshots, never mutable runtime-volume reuse."""

import hashlib
import json
import logging
import re
import sys
import time

from .repair_workspace import source_tree
from .repository_source import ExecutionError, discover
from .store import Conflict, uid

CACHE_SLOTS = 3


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dependency_snapshots (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, account_id INTEGER NOT NULL,
            signature TEXT NOT NULL, image_id TEXT NOT NULL, profile TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL,
            UNIQUE(repo_id,account_id,signature)
        );
    """)


def source_digest(source, check=lambda: None):
    payload = json.dumps(source_tree(source, check), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def dependency_plan(source, config, image_id):
    """Unknown install inputs fall back to full setup; do not guess build dependencies."""
    try:
        source_tree(source)  # Reject links/oversized input before reading dependency manifests.
        detected = discover(source)
        language, command = config["language"], config["install"].strip()
        if language != detected["language"] or not detected["detected"]:
            return None
        if any(source.rglob("node_modules")):
            return None
        files = {}
        profile = None
        if language == "python":
            shadowed = set(sys.stdlib_module_names) | {"sitecustomize", "usercustomize", "pip"}
            if any(path.stem in shadowed for path in source.iterdir()):
                return None
            if any((source / name).exists() for name in (
                "pyproject.toml", "setup.py", "setup.cfg", "pip.conf", ".pip",
            )) or command != detected["install"]:
                return None
            for path in sorted(source.glob("requirements*.txt")):
                text = path.read_text(encoding="utf-8")
                # Flat registry pins only: local/editable/URL/include inputs require full setup.
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?"
                        r"\s*==\s*[A-Za-z0-9_.+-]+(?:\s+#.*)?", line,
                    ):
                        return None
                files[path.name] = text
            profile = "python_registry_pins"
        elif language == "go":
            if command != "go mod download" or (source / "go.work").exists():
                return None
            mod = (source / "go.mod").read_text(encoding="utf-8")
            if re.search(r"=>\s*(?:\.|/|[A-Za-z]:)", mod):
                return None
            for name in ("go.mod", "go.sum"):
                if (source / name).is_file():
                    files[name] = (source / name).read_text(encoding="utf-8")
            profile = "go_modules"
        elif language == "typescript":
            if command != "npm ci" or (source / ".npmrc").exists():
                return None
            manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
            lock = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
            hooks = {"preinstall", "install", "postinstall", "prepublish", "prepare",
                     "preprepare", "postprepare"}
            packages = lock.get("packages")
            if (manifest.get("workspaces") or hooks.intersection(manifest.get("scripts", {}))
                    or not isinstance(packages, dict) or not packages):
                return None
            for path, package in packages.items():
                if (not isinstance(package, dict) or package.get("hasInstallScript")
                        or package.get("link") or path and not
                        str(package.get("resolved", "")).startswith("https://")):
                    return None
            for name in ("package.json", "package-lock.json"):
                files[name] = (source / name).read_text(encoding="utf-8")
            profile = "npm_locked_no_hooks"
        if not profile or sum(len(text) for text in files.values()) > 1_000_000:
            return None
        payload = {"policy": 1, "profile": profile, "language": language,
                   "install": command, "image_id": image_id, "files": files}
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {"signature": signature, "profile": profile}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, Conflict):
        return None


class EnvironmentReuse:
    def _exact_environment(self, env, revision, check, *, config=None):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT e.* FROM environments e WHERE e.repo_id=? AND e.account_id=? "
                "AND e.revision=? AND e.id!=? AND e.installed=1 "
                "AND e.state IN ('ready','stopped','test_failed') "
                "AND NOT EXISTS (SELECT 1 FROM execution_jobs j WHERE j.env_id=e.id "
                "AND j.state IN ('queued','running')) ORDER BY e.updated DESC LIMIT 10",
                (env["repo_id"], env["account_id"], revision, env["id"]),
            ).fetchall()
        digest = source_digest(self.directory(env["id"]) / "source", check)
        for previous in rows:
            previous_config = json.loads(previous["config"] or "{}")
            if config and (
                config.get("project_profile", {}).get("kind") !=
                previous_config.get("project_profile", {}).get("kind") or
                config.get("project_profile", {}).get("kind") == "python_desktop" and
                not previous_config.get("desktop_approved")
            ):
                continue
            if config and any(previous_config.get(key, "service" if key == "run_mode" else None)
                              != config.get(key, "service" if key == "run_mode" else None)
                              for key in ("language", "install", "test", "start", "port",
                                          "install_network", "run_mode")):
                continue
            try:
                if digest == source_digest(self.directory(previous["id"]) / "source", check):
                    return previous["id"]
            except (OSError, Conflict):
                continue
        return None

    def _dependency_setup(self, env, check):
        source = self.directory(env["id"]) / "source"
        plan = dependency_plan(source, env["config"], env["image_id"])
        capable = all(callable(getattr(self.runtime, name, None)) for name in (
            "snapshot_dependencies", "restore_dependencies", "delete_dependencies",
        ))
        cache = None
        if plan and capable:
            with self.store.connection() as db:
                cache = db.execute(
                    "SELECT * FROM dependency_snapshots WHERE repo_id=? AND account_id=? "
                    "AND signature=? AND state='ready'",
                    (env["repo_id"], env["account_id"], plan["signature"]),
                ).fetchone()
        if cache:
            try:
                self.runtime.restore_dependencies(env, source, cache["id"], check)
                check()
                self.update(env["id"], preparation="dependencies_reused",
                            dependency_digest=plan["signature"], cache_source_env_id=cache["id"])
                return 0, "RepoWatch: 已从只读依赖快照准备独立环境；新源码仍需重新测试。"
            except ExecutionError as exc:
                if str(exc) in ("execution_cancelled", "execution_interrupted"):
                    raise
                with self.store.connection() as db:
                    db.execute("UPDATE dependency_snapshots SET state='invalid' WHERE id=?",
                               (cache["id"],))
                self.runtime.cleanup(env)
        self.runtime.seed(env, source, check)
        code, log = self.runtime.run(env, "setup", check)
        check()
        self.update(env["id"], preparation="full_install", cache_source_env_id=None,
                    dependency_digest=plan["signature"] if plan else None)
        if code == 0 and plan and capable:
            self._save_dependency_snapshot(env, plan, check)
        return code, log

    def _save_dependency_snapshot(self, env, plan, check):
        cache_id = uid("cache")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM dependency_snapshots WHERE repo_id=? AND account_id=? "
                          "AND signature=?", (env["repo_id"], env["account_id"],
                                              plan["signature"])).fetchone():
                return
            count = db.execute(
                "SELECT count(*) FROM dependency_snapshots WHERE repo_id=? AND account_id=?",
                (env["repo_id"], env["account_id"]),
            ).fetchone()[0]
            if count >= CACHE_SLOTS:
                return
            db.execute("INSERT INTO dependency_snapshots VALUES(?,?,?,?,?,?,?,?)",
                       (cache_id, env["repo_id"], env["account_id"], plan["signature"],
                        env["image_id"], plan["profile"], "building", time.time()))
        try:
            self.runtime.snapshot_dependencies(env, cache_id, check)
            check()
            with self.store.connection() as db:
                db.execute("UPDATE dependency_snapshots SET state='ready' WHERE id=?", (cache_id,))
        except Exception as exc:
            try:
                self.runtime.delete_dependencies(env, cache_id)
                with self.store.connection() as db:
                    db.execute("DELETE FROM dependency_snapshots WHERE id=?", (cache_id,))
            except Exception:
                with self.store.connection() as db:
                    db.execute("UPDATE dependency_snapshots SET state='invalid' WHERE id=?",
                               (cache_id,))
                logging.getLogger(__name__).warning("Dependency snapshot cleanup pending.")
            if isinstance(exc, ExecutionError) and str(exc) in (
                "execution_cancelled", "execution_interrupted",
            ):
                raise
            logging.getLogger(__name__).warning(
                "Dependency snapshot unavailable; full setup retained.",
            )
