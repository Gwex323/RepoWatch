"""P1 浏览器验收的临时回环宿主；全部凭据／仓库均为夹具。"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.application.task_records import block, bump_task
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)
    values = {}
    vault = SimpleNamespace(
        create=lambda r, s: values.update({r: s}),
        read=lambda r: values[r],
        delete=lambda r: values.pop(r, None),
    )
    services = HostServices(
        settings,
        vault=vault,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    repository = services.tasks.register_repository(
        "browser-fixture", admission_mode="all_bugs"
    )
    task = services.tasks.ensure_task(repository, "browser-fixture-issue", now=0)
    services.budgets.authorize_initial(
        "browser-initial",
        task,
        BudgetAmounts(
            active_ms=600000,
            model_calls=20,
            tokens=100000,
            repair_rounds=5,
            recovery_rounds=5,
        ),
        expected_revision=1,
        reason="受控浏览器夹具",
        now=0,
    )
    with services.store.write() as records:
        block(
            records, "no_progress", task, now=0, window=3, fingerprints=["a", "b", "a"]
        )
        bump_task(
            records,
            records.one("tasks", id=task),
            state="blocked",
            outcome="no_progress",
        )
        records.change(
            "budget_ledgers",
            {"owner_task_id": task},
            {
                "used_json": '{"tokens":10}',
                "reserved_json": '{"tokens":20}',
            },
        )
    sessions = LocalSessions(f"http://127.0.0.1:{args.port}")
    app = create_app(
        services,
        sessions=sessions,
        static_dir=Path(__file__).resolve().parents[1] / "frontend" / "dist",
    )
    # Pipe-only IPC for the test parent; never forward this line to console/log artifacts.
    print(
        json.dumps({"bootstrap": sessions.bootstrap_token, "task_id": task}), flush=True
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
