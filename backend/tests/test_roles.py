import json
from types import SimpleNamespace

import httpx
import pytest

from repowatch.adapters.providers import ProviderAdapter
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.application.artifacts import ArtifactService
from repowatch.application.budgets import BudgetService
from repowatch.application.diagnostics import DiagnosticService
from repowatch.application.orchestration import Orchestrator
from repowatch.application.providers import ProviderService
from repowatch.application.roles import RoleService
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.operations.migrate import migrate
from repowatch.worker.role_probe import FIXED, RoleProbe


@pytest.fixture
def kernel(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    values = {}
    vault = SimpleNamespace(
        create=lambda r, s: values.update({r: s}),
        read=lambda r: values[r],
        delete=lambda r: values.pop(r, None),
    )
    providers = ProviderService(store, vault)
    provider = providers.save(
        "provider",
        {
            "name": "fixture",
            "base_url": "https://fixture.example/v1",
            "api": "chat_completions",
            "api_key": "test-role-key",
            "models": [],
        },
    )["providers"][0]
    artifacts = ArtifactService(store, settings.artifacts_dir)
    roles = RoleService(store, artifacts)
    budgets, queue = BudgetService(store, settings.artifacts_dir), Orchestrator(store)
    yield SimpleNamespace(
        settings=settings,
        store=store,
        providers=providers,
        provider=provider,
        roles=roles,
        artifacts=artifacts,
        budgets=budgets,
        queue=queue,
        diagnostics=DiagnosticService(store),
    )
    store.close()


def bind(k, key="processor", *, model="model", operation=None, expected_revision=0):
    return k.roles.set_binding(
        operation or key,
        key,
        {
            "role": "reviewer" if key == "reviewer" else "processor",
            "provider_id": k.provider["id"],
            "selection": {"model": model, "reasoning_effort": None},
        },
        expected_revision=expected_revision,
    )


def wire(*, plain=False, wrong=False):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        messages = body["messages"]
        context = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        tools = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not tools:
            name, arguments = "read_fixture", {}
        elif len(tools) == 1:
            name, arguments = (
                ("write_fixture", {"content": FIXED})
                if context["role"] == "processor"
                else ("read_evidence", {})
            )
        else:
            name = "RoleProbeResult"
            arguments = {**context, "proof": "incorrect" if wrong else tools[0]["proof"]}
        message = {"role": "assistant", "content": "connection ok" if plain else None}
        if not plain:
            message["tool_calls"] = [
                {
                    "id": f"call_{len(tools)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "chat_1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if plain else "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    return respond, calls


def diagnose(k, role, *, key=None, plain=False, wrong=False, model="model"):
    with k.store.read() as records:
        revision = records.one("global_control", singleton=1)["revision"]
    request = k.diagnostics.request(
        key or "diag-" + role,
        "provider_capability",
        {
            "provider_revision_id": k.provider["revision_id"],
            "model": model,
            "role": role,
        },
        BudgetAmounts(
            active_ms=120000, model_calls=10, tokens=100000, repair_rounds=1, recovery_rounds=1
        ),
        expected_revision=revision,
        now=0,
        duration=120,
    )
    token = k.diagnostics.claim(request["task_id"], now=0)
    fixture, calls = wire(plain=plain, wrong=wrong)
    adapter = ProviderAdapter(k.providers, transport=httpx.MockTransport(fixture))
    result = RoleProbe(
        k.diagnostics, adapter, k.budgets, k.queue, k.artifacts, k.roles, clock=lambda: 0
    ).run(token)
    return request, result, calls


def task_run(k, *, key="issue:1"):
    tasks = TaskRecords(k.store)
    repo = tasks.register_repository("repo", admission_mode="all_bugs")
    task = tasks.ensure_task(repo, key, now=0)
    k.budgets.authorize_initial(
        "budget-" + key,
        task,
        BudgetAmounts(
            active_ms=100000,
            model_calls=10,
            tokens=100000,
            repair_rounds=5,
            recovery_rounds=5,
        ),
        expected_revision=1,
        reason="test",
        now=0,
    )
    k.queue.set_pause(False, now=0)
    return task, k.queue.claim(now=0)


def test_role_probe_executes_tools_and_capability_admission_is_not_connection(kernel):
    k = kernel
    bind(k)
    bind(k, "reviewer")
    _, result, calls = diagnose(k, "processor")
    assert result["passed"] and len(calls) == 3
    _, result, calls = diagnose(k, "reviewer")
    assert result["passed"] and len(calls) == 3
    task, token = task_run(k)
    assert set(k.roles.admit(token, now=0)) == {"processor", "reviewer"}
    assert k.budgets.view(task)["used"]["model_calls"] == 0


@pytest.mark.parametrize("plain,wrong", [(True, False), (False, True)])
def test_text_or_unverified_handoff_never_admits_role(kernel, plain, wrong):
    k = kernel
    bind(k)
    _, result, _ = diagnose(k, "processor", plain=plain, wrong=wrong)
    assert not result["passed"]
    _, token = task_run(k)
    with pytest.raises(PermissionError):
        k.roles.admit(token, now=0, keys=("processor",))
    with k.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_new_defaults_and_credential_rotation_do_not_rewrite_old_run_snapshot(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    task, token = task_run(k)
    old = k.roles.admit(token, now=0, keys=("processor",))["processor"]
    k.providers.save(
        "rotate",
        {
            "name": "fixture",
            "base_url": "https://changed.example/v1",
            "api": "responses",
            "api_key": "test-rotated-role-key",
            "models": [],
        },
        provider_id=k.provider["id"],
        expected_revision=1,
    )
    bind(k, model="different-model", operation="change-role", expected_revision=1)
    assert k.roles.admit(token, now=0, keys=("processor",))["processor"] == old
    k.queue.complete(token, now=0, state="queued")
    new_token = k.queue.claim(now=0)
    with k.store.read() as records:
        new = json.loads(records.one("runs", id=new_token.run_id)["snapshot_json"])["roles"][
            "processor"
        ]
        assert new["credential_revision"] != old["credential_revision"]
        assert new["provider_revision_id"] != old["provider_revision_id"]
    with pytest.raises(PermissionError):
        k.roles.admit(new_token, now=0, keys=("processor",))


def test_role_binding_revision_replay_history_and_finite_capability_expiry(kernel):
    k = kernel
    k.roles = RoleService(k.store, k.artifacts, capability_ttl=1)
    first = bind(k)
    assert bind(k) == first
    with pytest.raises(ConflictError):
        bind(k, model="changed")
    request, _, _ = diagnose(k, "processor")
    _, token = task_run(k)
    with pytest.raises(PermissionError):
        k.roles.admit(token, now=2, keys=("processor",))
    with pytest.raises(ConflictError):
        with k.store.write() as records:
            records.change(
                "capability_checks",
                {"diagnostic_task_id": request["task_id"]},
                {"expires_at": 999999},
            )


def test_new_failed_check_supersedes_old_passing_cache(kernel):
    k = kernel
    bind(k)
    diagnose(k, "processor")
    diagnose(k, "processor", key="new-failure", wrong=True)
    _, token = task_run(k)
    with pytest.raises(PermissionError):
        k.roles.admit(token, now=0, keys=("processor",))
