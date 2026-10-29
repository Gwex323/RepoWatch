"""Local-owner commands. Planning, authorization, merge and deployment stay separate."""

import argparse
import json
import re
import sys
from pathlib import Path

from .github_accounts import GitHubAccounts, GitHubError
from .release_github import ReleaseAuthority
from .release_manifest import Manifest, absolute, git
from .release_runtime import Supervisor, serve
from .release_state import (
    ExistingStore,
    Journal,
    atomic_json,
    existing_database,
    initialize,
    read_json,
)
from .releases import ReleaseManager
from .store import Conflict, Store


def parser():
    result = argparse.ArgumentParser(description="RepoWatch managed releases")
    commands = result.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init")
    init.add_argument("--database", required=True)
    plan = commands.add_parser("plan")
    for name in ("code-root", "python", "database", "config-ref", "state-root"):
        plan.add_argument("--" + name, required=True)
    plan.add_argument("--pr", required=True, type=int)
    plan.add_argument("--port", type=int, default=8765)
    plan.add_argument("--maintenance-task")
    plan.add_argument("--no-workers", action="store_true")
    for name in ("authorize", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--database", required=True)
        command.add_argument("--kind", choices=("merge", "deploy"), required=True)
        command.add_argument("--target", required=True)
        if name == "authorize":
            command.add_argument("--resume-original", action="store_true")
    merge = commands.add_parser("merge")
    merge.add_argument("--database", required=True)
    merge.add_argument("--pr", type=int, required=True)
    merge.add_argument("--head", required=True)
    merge.add_argument("--state-root", required=True)
    for name in ("deploy", "rollback", "reconcile", "serve", "probe"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True)
        if name == "probe":
            command.add_argument("--database", required=True)
    monitor = commands.add_parser("supervise")
    monitor.add_argument("--state-root", required=True)
    return result


def run(args):
    if args.action == "serve":
        return serve(absolute(args.manifest))
    if args.action == "supervise":
        return Supervisor(absolute(args.state_root)).run()
    if args.action == "probe":
        manifest = Manifest.load(args.manifest)
        database = absolute(args.database)
        if (not database.is_relative_to(Path(manifest.state_root) / "backups")
                or not database.name.endswith(".probe.db")):
            raise Conflict("release_probe_database_invalid")
        with existing_database(database):
            pass
        Store(database)
        return {"state": "migration_probed"}
    if args.action in {"deploy", "rollback", "reconcile"}:
        manifest = Manifest.load(args.manifest)
        store = ExistingStore(absolute(manifest.database))
    else:
        with existing_database(absolute(args.database)):
            pass
        store = ExistingStore(absolute(args.database))
    authority = ReleaseAuthority(store, GitHubAccounts(store))
    if args.action == "init":
        with store.connection() as db:
            initialize(db)
        return {"state": "initialized", "database_id": store.setting("release_database_id")}
    if args.action in {"authorize", "revoke"}:
        if not re.fullmatch(r"[a-f0-9]{" + ("40" if args.kind == "merge" else "64") + "}",
                            args.target):
            raise Conflict("release_authorization_target_invalid")
        if args.action == "revoke":
            with store.connection() as db:
                db.execute("UPDATE release_authorizations SET revoked=1 WHERE kind=? AND target=?",
                           (args.kind, args.target))
            return {"state": "revoked", "kind": args.kind, "target": args.target}
        binding = authority.authorize(args.kind, args.target,
                                      resume_original=args.resume_original)
        return {"state": "authorized", "kind": args.kind, "target": args.target,
                "binding": binding}
    if args.action == "plan":
        with store.connection() as db:
            account = db.execute("SELECT a.id,a.version FROM github_accounts a "
                                 "JOIN github_configuration c ON c.active_account_id=a.id "
                                 "WHERE a.state='verified'").fetchone()
        if not account:
            raise Conflict("release_account_required")
        binding = {"account_id": account["id"], "account_version": account["version"]}
        _, _, repository = authority.target.authority(**binding)
        binding["repository_id"] = repository["id"]
        commit = git(absolute(args.code_root), "rev-parse", "HEAD")
        authority.merged(args.pr, commit, binding)
        previous = read_json(absolute(args.state_root) / "selection.json", {}).get("current")
        manifest = Manifest.build(
            root=args.code_root, python=args.python, database=args.database,
            config_ref=args.config_ref, state_root=args.state_root, port=args.port,
            pr=args.pr, commit=commit, binding=binding, previous=previous,
            maintenance_task=args.maintenance_task, workers=not args.no_workers)
        path = Path(manifest.state_root) / "manifests" / (manifest.id + ".json")
        if path.exists() and read_json(path) != manifest.model_dump():
            raise Conflict("release_manifest_changed")
        atomic_json(path, manifest.model_dump())
        return {"id": manifest.id, "manifest": str(path), "state": "planned_not_authorized"}
    if args.action == "merge":
        if not re.fullmatch(r"[a-f0-9]{40}", args.head):
            raise Conflict("release_pr_head_invalid")
        return authority.merge(args.pr, args.head, Journal(absolute(args.state_root)))
    manager = ReleaseManager(authority)
    if args.action == "reconcile":
        return manager.reconcile(absolute(args.manifest))
    return manager.deploy(absolute(args.manifest)) if args.action == "deploy" else manager.rollback(
        absolute(args.manifest))


def main():
    try:
        result = run(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=True))
    except (Conflict, GitHubError) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc)}))
        sys.exit(2)
    except Exception:
        print(json.dumps({"state": "blocked", "reason": "release_command_failed"}))
        sys.exit(2)
