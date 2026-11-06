import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)
    services = HostServices(settings, vault=SimpleNamespace())
    repo = services.tasks.register_repository("p3-browser", admission_mode="all_bugs")
    task = services.tasks.ensure_task(
        repo, "issue:acceptance", issue_id="acceptance", now=time.time()
    )
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"manual_priority": 100})
    candidate = services.tasks.ensure_task(repo, "candidate:browser", now=time.time())
    with services.store.write() as records:
        records.add(
            "triage_decisions",
            {
                "id": "candidate-lookup",
                "task_id": candidate,
                "input_revision": 1,
                "feedback_revision": 1,
                "admission_revision": 1,
                "result": "needs_information",
                "reason_code": "issue_lookup_required",
                "evidence_json": canonical_json(
                    {"lookup_digest": "fixture-lookup", "issues": []}
                ),
            },
        )
        records.change(
            "tasks",
            {"id": candidate},
            {"state": "waiting", "wait_reason": "information"},
        )
    relation_task = services.tasks.ensure_task(
        repo, "issue:relations", issue_id="601", now=time.time()
    )
    services.budgets.authorize_initial(
        "fixture-budget",
        relation_task,
        BudgetAmounts(
            active_ms=600000,
            model_calls=10,
            tokens=10000,
            repair_rounds=5,
            recovery_rounds=5,
        ),
        expected_revision=1,
        reason="browser fixture",
        now=time.time(),
    )
    with services.store.write() as records:
        records.change(
            "tasks", {"id": task}, {"state": "waiting", "wait_reason": "approval"}
        )
    services.queue.set_pause(False, now=time.time())
    token = services.queue.claim(now=time.time())
    facts = {
        "issue": {"id": 601, "number": 6},
        "comments": [],
        "pull_requests": [
            {
                "id": 701,
                "number": 7,
                "title": "fixture competing PR",
                "state": "open",
                "html_url": "https://github.com/fixture/repo/pull/7",
            }
        ],
    }
    artifact_id = services.artifacts.register(
        token,
        "fixture-context",
        canonical_json(facts).encode(),
        context={"producer": "docker_runner"},
        now=time.time(),
    )
    with services.store.write() as records:
        records.add(
            "issue_contexts",
            {
                "id": "fixture-relations",
                "task_id": relation_task,
                "run_id": token.run_id,
                "digest": "fixture-context",
                "artifact_id": artifact_id,
                "facts_json": canonical_json(facts),
                "created_at": time.time(),
            },
        )
        records.add(
            "publication_intents",
            {
                "id": "fixture-receipt",
                "task_id": relation_task,
                "repository_id": repo,
                "issue_id": "601",
                "kind": "pull_request",
                "state": "unknown",
                "request_json": "{}",
                "request_sha256": "fixture",
                "created_at": time.time(),
            },
        )
    services.queue.complete(
        token, now=time.time(), state="waiting", wait_reason="fixture_relations"
    )
    services.queue.set_pause(True, now=time.time())
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"state": "queued", "wait_reason": None})
    sessions = LocalSessions(f"http://127.0.0.1:{args.port}")
    app = create_app(
        services,
        sessions=sessions,
        static_dir=Path(__file__).resolve().parents[1] / "frontend" / "dist",
    )
    print(
        json.dumps(
            {
                "bootstrap": sessions.bootstrap_token,
                "task_id": task,
                "candidate_id": candidate,
                "relation_task_id": relation_task,
            }
        ),
        flush=True,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
