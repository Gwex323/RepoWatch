import importlib

from fastapi.testclient import TestClient

from repowatch.api.app import create_app


def test_health_does_not_start_work() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health").json() == {
            "status": "ok",
            "stage": "p0",
            "version": "0.1.0.dev0",
        }
        assert client.post("/api/v1/tasks").status_code == 404


def test_runtime_dependencies_import_together() -> None:
    for module_name in (
        "deepagents",
        "langgraph.graph",
        "langgraph.checkpoint.sqlite",
        "langchain_openai",
        "sqlalchemy",
        "alembic",
    ):
        assert importlib.import_module(module_name) is not None
