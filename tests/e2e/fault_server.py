"""Isolated browser fixture server; no business workers, model calls or external writes."""

import json
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from repowatch.app import create_app  # noqa: E402
from repowatch.fault_contract import FaultEvent  # noqa: E402
from repowatch.system_faults import enqueue, record_worker_failure  # noqa: E402
from repowatch.task_scopes import ensure_scope  # noqa: E402


def main():
    os.environ["REPOWATCH_LANGFUSE_ENABLED"] = "0"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    with TemporaryDirectory(prefix="repowatch-fault-e2e-") as temporary:
        app = create_app(Path(temporary) / "faults.db", start_worker=False)
        store = app.state.store
        store.setup_password("qa-only-owner-password")
        repo = store.add_repository("owner/fault-fixture", "https://github.com/owner/fault-fixture")
        now = time.time()
        env_id, task_id = "env_1111111111111111", "analysis_1111111111111111"
        config = {
            "language": "python",
            "install": "",
            "test": "python -m pytest",
            "start": "",
            "port": 8000,
            "install_network": False,
        }
        with store.connection() as db:
            db.execute(
                "INSERT INTO github_accounts"
                "(id,login,encrypted_token,state,checked_at,updated) "
                "VALUES(1,'fixture','unusable','verified',?,?)",
                (now, now),
            )
            db.execute("UPDATE github_configuration SET active_account_id=1")
            db.execute(
                "INSERT INTO environments(id,repo_id,account_id,account_version,revision,"
                "config,state,created,updated) VALUES(?,?,1,1,?,?,'ready',?,?)",
                (env_id, repo["id"], "a" * 40, json.dumps(config), now, now),
            )
            db.execute(
                "INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,"
                "trigger_key,status,context,error,created,updated) "
                "VALUES(?,?,?,?,'basis','owner','failed','{}',"
                "'analysis_scope_read_not_followed',?,?)",
                (task_id, env_id, repo["id"], "a" * 40, now - 100, now - 100),
            )
            ensure_scope(
                db,
                db.execute(
                    "SELECT * FROM repository_analyses WHERE id=?",
                    (task_id,),
                ).fetchone(),
            )
            # Keep the original source older than the default ten analysis rows.
            for index in range(12):
                newer_id = f"analysis_{index + 32:016x}"
                db.execute(
                    "INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,"
                    "trigger_key,status,context,created,updated) "
                    "VALUES(?,?,?,?,'basis',?,'completed','{}',?,?)",
                    (
                        newer_id,
                        env_id,
                        repo["id"],
                        "a" * 40,
                        f"newer{index}",
                        now + index,
                        now + index,
                    ),
                )
                ensure_scope(
                    db,
                    db.execute(
                        "SELECT * FROM repository_analyses WHERE id=?",
                        (newer_id,),
                    ).fetchone(),
                )
            for index in range(2):
                enqueue(
                    db,
                    FaultEvent(
                        source_event_key=f"analysis_events:browser{index}",
                        flow="analysis",
                        task_id=task_id,
                        root_task_id=task_id,
                        source_table="analysis_events",
                        source_id=f"browser{index}",
                        occurred=now + index,
                        repo_id=repo["id"],
                        account_id=1,
                        env_id=env_id,
                        attempt=index + 1,
                        role="repository_analyst",
                        stage="repository_analyst",
                        code="analysis_scope_read_not_followed",
                        category="protocol",
                        execution="not_executed",
                        revision="a" * 40,
                        workbench_version="browser-build",
                        model_binding={
                            "model": "fixture-model",
                            "model_version": 3,
                            "provider_version": 5,
                        },
                        budget={"requests": 2, "max_requests": 32, "resumptions": 0},
                        diagnostic={
                            "calls": 2,
                            "matching": 2,
                            "other": 0,
                            "allowed": 1,
                            "reason": "too_many_calls",
                            "accepted": False,
                            "required": "scope",
                            "recovery": "request_mismatch",
                        },
                    ),
                )
        record_worker_failure(store, "fixture_worker")
        app.state.faults.tick()
        uvicorn.run(
            app, host="127.0.0.1", port=int(os.environ.get("REPOWATCH_FAULT_TEST_PORT", "8778"))
        )


if __name__ == "__main__":
    main()
