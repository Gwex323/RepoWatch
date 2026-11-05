import io
import json
import os
import tarfile
import time
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from repowatch.adapters.docker_workspaces import DockerWorkspaces
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import ArtifactService
from repowatch.application.budgets import BudgetService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.task_records import TaskRecords
from repowatch.application.workspaces import WorkspaceService
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.dependencies import dependency_plan
from repowatch.domain.scans import inventory
from repowatch.operations.migrate import migrate
from repowatch.worker.runner import Runner


@pytest.mark.skipif(os.environ.get("REPOWATCH_DOCKER_TESTS") != "1", reason="显式 Docker 验收")
@pytest.mark.parametrize("language", ["python", "node"])
def test_actual_offline_dependency_install_preserves_declarations(tmp_path, language):
    if language == "python":
        contents = {"requirements.txt": b"fixturepkg==1.0\n"}
        argv = ["python", "-c", "import fixturepkg; assert fixturepkg.value == 42"]
        image = "python:3.13-slim-bookworm"
    else:
        contents = {
            "package.json": json.dumps(
                {
                    "name": "fixture",
                    "version": "1.0.0",
                    "dependencies": {"fixturepkg": "file:vendor/pkg"},
                }
            ).encode(),
            "vendor/pkg/package.json": b'{"name":"fixturepkg","version":"1.0.0"}',
            "vendor/pkg/index.js": b"module.exports = 42;",
            "package-lock.json": json.dumps(
                {
                    "name": "fixture",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {
                            "name": "fixture",
                            "version": "1.0.0",
                            "dependencies": {"fixturepkg": "file:vendor/pkg"},
                        },
                        "node_modules/fixturepkg": {"resolved": "vendor/pkg", "link": True},
                        "vendor/pkg": {"name": "fixturepkg", "version": "1.0.0"},
                    },
                }
            ).encode(),
        }
        argv = ["node", "-e", "if(require('fixturepkg') !== 42) process.exit(1)"]
        image = "node:24-bookworm-slim"
    objects = SimpleNamespace(
        entries=lambda *args: [
            {"path": path, "oid": path, "mode": "100644", "kind": "blob"} for path in contents
        ],
        blob=lambda repository, oid: contents[oid],
    )
    backend = DockerWorkspaces(tmp_path / "workspaces", objects)
    built_image = None
    if language == "python":
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("fixturepkg.py", "value = 42\n")
            archive.writestr(
                "fixturepkg-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: fixturepkg\nVersion: 1.0\n",
            )
            archive.writestr(
                "fixturepkg-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr("fixturepkg-1.0.dist-info/RECORD", "")
        digest = (
            backend.docker("image", "inspect", image, "--format", "{{.Id}}").stdout.decode().strip()
        )
        context = io.BytesIO()
        with tarfile.open(fileobj=context, mode="w") as archive:
            for name, content in {
                "Dockerfile": (
                    "FROM " + digest + "\nCOPY fixturepkg-1.0-py3-none-any.whl "
                    "/opt/repowatch/wheels/fixturepkg-1.0-py3-none-any.whl\n"
                ).encode(),
                "fixturepkg-1.0-py3-none-any.whl": wheel.getvalue(),
            }.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        built_image = "repowatch-dependency-test:" + uuid4().hex
        backend.docker(
            "build",
            "--network=none",
            "--pull=false",
            "-t",
            built_image,
            "-",
            input_bytes=context.getvalue(),
        )
        image = built_image
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    tasks, queue = TaskRecords(store), Orchestrator(store)
    repository_id = tasks.register_repository(language, admission_mode="all_bugs")
    task_id = tasks.ensure_task(repository_id, "scan:deps", mode="scan", now=time.time())
    budgets = BudgetService(store, settings.artifacts_dir)
    budgets.authorize_initial(
        "budget",
        task_id,
        BudgetAmounts(
            active_ms=600000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="实际离线安装验收",
        now=time.time(),
    )
    queue.set_pause(False, now=time.time())
    token = queue.claim(now=time.time(), ttl=300)
    workspaces = WorkspaceService(store, backend)
    workspace = None
    try:
        workspace = workspaces.create(token, "a" * 40, image=image, now=time.time())
        artifacts = ArtifactService(store, settings.artifacts_dir)
        runner = Runner(
            SimpleNamespace(
                store=store,
                queue=queue,
                budgets=budgets,
                artifacts=artifacts,
            ),
            workspaces,
        )
        plan = dependency_plan(objects, repository_id, inventory(objects, repository_id, "a" * 40))
        assert plan["steps"] and not plan["gaps"]
        result = runner.check(
            token,
            workspace["id"],
            argv,
            timeout=60,
            operation_key="actual-install",
            preparation=plan,
        )
        log = artifacts.require(result["log_artifact_id"], now=time.time())
        assert result["exit_code"] == 0, (artifacts.root / log["relative_path"]).read_text(
            errors="replace"
        )
        workspaces.remove(token, workspace["id"], now=time.time())
        workspace = None
        queue.complete(token, now=time.time())
    finally:
        if workspace and backend.exists(workspace):
            backend.remove(workspace)
        store.close()
        if built_image:
            backend.docker("image", "rm", built_image, check=False)
