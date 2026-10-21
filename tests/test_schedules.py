import pytest
from fastapi.testclient import TestClient

from repowatch.app import create_app
from repowatch.schedules import RepositorySchedules
from repowatch.store import Conflict, Store


class FakeExecution:
    def __init__(self, pipelines=None, error=None):
        self.pipelines = pipelines or []
        self.error = error
        self.created = []

    def active_pipeline(self, repo_id):
        return self.pipelines[0] if self.pipelines else None

    def create(self, repo_id, *, auto_run=False):
        if self.error:
            raise self.error
        self.created.append((repo_id, auto_run))
        return {"id": "environment_test"}


@pytest.fixture
def schedule_service(tmp_path):
    store = Store(tmp_path / "schedules.db")
    repo = store.add_repository(
        "alice/project", "https://github.com/alice/project"
    )
    execution = FakeExecution()
    return store, repo, execution, RepositorySchedules(store, execution)


def test_schedule_defaults_and_configuration(schedule_service):
    store, repo, _, schedules = schedule_service
    initial = schedules.get(repo["id"])
    assert initial["enabled"] is False
    assert initial["local_time"] == "09:00"
    assert initial["last_run_date"] == ""
    assert initial["last_status"] == "idle"

    updated = schedules.configure(
        repo["id"], enabled=True, local_time="08:30", version=initial["version"]
    )
    assert updated["enabled"] is True
    assert updated["local_time"] == "08:30"
    assert updated["last_status"] == "idle"
    assert updated["version"] == initial["version"] + 1


def test_schedule_requires_valid_time_and_version(schedule_service):
    store, repo, _, schedules = schedule_service
    initial = schedules.get(repo["id"])
    with pytest.raises(Conflict, match="schedule_time_invalid"):
        schedules.configure(
            repo["id"], enabled=True, local_time="24:00", version=initial["version"]
        )
    with pytest.raises(Conflict, match="schedule_changed"):
        schedules.configure(
            repo["id"], enabled=True, local_time="08:30", version=initial["version"] + 20
        )


def test_schedule_triggers_once_per_day(schedule_service):
    store, repo, execution, schedules = schedule_service
    initial = schedules.get(repo["id"])
    schedules.configure(
        repo["id"], enabled=True, local_time="00:00", version=initial["version"]
    )
    first = schedules.trigger(repo["id"])
    second = schedules.trigger(repo["id"])

    assert first["status"] == "scheduled"
    assert first["environment_id"] == "environment_test"
    assert second is None
    assert execution.created == [(repo["id"], True)]
    detail = schedules.get(repo["id"])
    assert detail["last_status"] == "scheduled"
    assert detail["last_run_date"]


def test_schedule_skips_active_pipeline(schedule_service):
    store, repo, _, schedules = schedule_service
    execution = FakeExecution(pipelines=[{"id": "pipeline_test"}])
    schedules.execution = execution
    initial = schedules.get(repo["id"])
    schedules.configure(
        repo["id"], enabled=True, local_time="00:00", version=initial["version"]
    )
    result = schedules.trigger(repo["id"])

    assert result == {"status": "skipped_active", "pipeline_id": "pipeline_test"}
    assert execution.created == []
    assert schedules.get(repo["id"])["last_status"] == "skipped_active"


def test_schedule_records_failure(schedule_service):
    store, repo, _, schedules = schedule_service
    schedules.execution = FakeExecution(error=Conflict("github_active_account_required"))
    initial = schedules.get(repo["id"])
    schedules.configure(
        repo["id"], enabled=True, local_time="00:00", version=initial["version"]
    )
    result = schedules.trigger(repo["id"])

    assert result == {"status": "failed", "detail": "github_active_account_required"}
    detail = schedules.get(repo["id"])
    assert detail["last_status"] == "failed"
    assert detail["last_detail"] == "github_active_account_required"


def test_schedule_summary_counts_enabled_repositories(schedule_service):
    store, repo, _, schedules = schedule_service
    initial = schedules.get(repo["id"])
    assert schedules.summary() == {
        "frequency": "daily", "enabled": False,
        "repositories": 1, "enabled_repositories": 0,
    }
    schedules.configure(
        repo["id"], enabled=True, local_time="08:30", version=initial["version"]
    )
    assert schedules.summary() == {
        "frequency": "daily", "enabled": True,
        "repositories": 1, "enabled_repositories": 1,
    }


def test_schedule_overview_includes_all_repositories(schedule_service):
    store, repo, _, schedules = schedule_service
    other = store.add_repository(
        "bob/service", "https://github.com/bob/service"
    )
    initial = schedules.get(repo["id"])
    schedules.configure(
        repo["id"], enabled=True, local_time="08:30", version=initial["version"]
    )
    schedules.execution = FakeExecution(error=Conflict("github_active_account_required"))
    schedules._record(repo["id"], "failed", "github_active_account_required")

    overview = schedules.list()

    assert overview[0] == {
        "repo_id": "repo_fixture", "repository": "samples / page-window",
        "kind": "fixture", "enabled": False, "local_time": "09:00",
        "last_run_date": "", "last_status": "paused", "last_detail": "",
    }
    assert overview[1:] == [
        {
            "repo_id": repo["id"], "repository": "alice/project", "kind": "github",
            "enabled": True, "local_time": "08:30", "last_run_date": "",
            "last_status": "failed", "last_detail": "github_active_account_required",
        },
        {
            "repo_id": other["id"], "repository": "bob/service", "kind": "github",
            "enabled": False, "local_time": "09:00", "last_run_date": "",
            "last_status": "paused", "last_detail": "",
        },
    ]


def test_schedule_api_requires_owner_and_csrf(tmp_path):
    store = Store(tmp_path / "schedule-api.db")
    repo = store.add_repository(
        "alice/project", "https://github.com/alice/project"
    )
    app = create_app(store.path, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/schedules").status_code == 401
        session = client.post(
            "/api/setup", json={"password": "owner-test-password"}
        ).json()
        headers = {"x-csrf-token": session["csrf"]}
        url = f"/api/repositories/{repo['id']}/schedule"

        assert client.get("/api/schedules").status_code == 200
        assert client.get(url).status_code == 200
        assert client.post(url).status_code == 403
        response = client.post(
            url,
            headers=headers,
            json={"enabled": True, "local_time": "08:30", "version": 1},
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert response.json()["local_time"] == "08:30"
