import pytest

from repowatch.domain.protocols import TaskEnvelope


@pytest.fixture(autouse=True)
def disable_external_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture
def envelope() -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {
            "context": {
                "repository_id": "repo-1",
                "task_id": "task-1",
                "run_id": "run-1",
                "input_revision": 1,
                "acceptance_revision": 1,
                "configuration_id": "config-1",
                "configuration_revision": 1,
                "execution_generation": 1,
                "code": {
                    "base_sha": "a" * 40,
                    "candidate_sha": "b" * 40,
                    "tree_sha": "c" * 40,
                    "content_manifest_sha256": "d" * 64,
                },
            },
            "role": "processor",
            "mode": "implement",
            "objective": "修复夹具缺陷",
            "budget": {
                "owner_task_id": "task-1",
                "ledger_revision": 1,
                "reservation_id": "reserve-1",
            },
            "tool_policy": {"mode": "implement", "allowed": ["read_source", "write_source"]},
        }
    )
