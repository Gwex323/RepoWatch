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
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)
    services = HostServices(settings, vault=SimpleNamespace())
    repository = services.tasks.register_repository(
        "p2-browser", admission_mode="all_bugs"
    )
    sha = "a" * 40
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "target_sha": sha,
                "github_owner": "fixture",
                "github_name": "p2-scan",
            },
        )
    task_id = services.tasks.ensure_task(
        repository, "p2-browser-scan", mode="scan", now=time.time()
    )
    services.budgets.authorize_initial(
        "budget",
        task_id,
        BudgetAmounts(
            active_ms=600000,
            tokens=10000,
            model_calls=10,
            repair_rounds=5,
            recovery_rounds=5,
        ),
        expected_revision=1,
        reason="受控浏览器夹具",
        now=time.time(),
    )
    services.queue.set_pause(False, now=time.time())
    token = services.queue.claim(now=time.time(), ttl=300)
    services.scans.objects = SimpleNamespace(
        entries=lambda *args: [
            {
                "path": "fixture.py",
                "oid": "b" * 40,
                "mode": "100644",
                "kind": "blob",
            }
        ],
        blob=lambda *args: b"value = 1\n",
    )
    batch = services.scans.begin(token, sha, {"image": "fixture"}, now=time.time())
    unit = services.scans.gaps(token, batch, now=time.time())["units"][0]
    inputs = services.scans.input(token, batch, unit["id"], now=time.time())
    services.scans.accept(
        token,
        batch,
        unit["id"],
        inputs["artifact_id"],
        {
            "input_sha256": unit["sha256"],
            "complete": True,
            "analysis": "已核对实际分块的内容、边界、配置及潜在错误路径。",
            "findings": [
                {
                    "title": "浏览器候选",
                    "basis": "本地夹具中的待核实边界",
                    "start": 0,
                    "end": unit["end"],
                },
                {
                    "title": "待排除候选",
                    "basis": "重复扫描的低置信线索",
                    "start": 0,
                    "end": unit["end"],
                },
            ],
        },
        now=time.time(),
    )
    services.queue.complete(
        token, now=time.time(), state="waiting", wait_reason="human_information"
    )
    services.queue.set_pause(True, now=time.time())
    sessions = LocalSessions(f"http://127.0.0.1:{args.port}")
    app = create_app(
        services,
        sessions=sessions,
        static_dir=Path(__file__).resolve().parents[1] / "frontend" / "dist",
    )
    print(
        json.dumps({"bootstrap": sessions.bootstrap_token, "task_id": task_id}),
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
