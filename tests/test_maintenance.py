"""Phase-four contracts: synthetic model, Docker and GitHub; no live writes."""

import asyncio
import base64
import hashlib
import json
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_github_accounts import TOKEN_A, MemoryVault
from test_model_providers import choice, configure, create
from test_repair_feedback import tool
from test_repository_analysis import ReviewModel, final_message

from repowatch.app import create_app
from repowatch.fault_contract import FaultEvent
from repowatch.github_accounts import GitHubAccounts, GitHubClient
from repowatch.maintenance import Maintenance, diagnosis_for
from repowatch.maintenance_model import classify_maintenance
from repowatch.maintenance_target import LOGIN, REPOSITORY
from repowatch.maintenance_workspace import approved_artifacts, create_workspace
from repowatch.model_providers import ModelProviders
from repowatch.repair_model import run_repair
from repowatch.repair_workspace import RepairWorkspace, digest
from repowatch.store import Conflict, Store
from repowatch.system_faults import SystemFaults, enqueue

SOURCE = "def bounded(value):\n    return value < 1\n"
PATH = "repowatch/sample.py"
BASE = "a" * 40


def answer(packet, classification="internal_defect"):
    return {
        "classification": classification,
        "title": "修复调用批次上限",
        "root_cause": "合法双调用被拒绝。",
        "invariant": "allow_two_calls",
        "path": PATH,
        "symbol": "bounded",
        "start_line": 1,
        "end_line": 2,
        "quote": SOURCE.rstrip(),
        "expected": "接受两条合法调用。",
        "observed": "实际仅接受一条。",
        "facts": list(packet["facts"]),
    }


class Remote:
    def __init__(self):
        self.login, self.push, self.name = LOGIN, True, REPOSITORY
        self.issues, self.prs, self.refs, self.calls = [], [], {}, []
        self.lose_pr, self.hide_pr = False, False
        self.lose_issue = False
        self.reject_pr, self.comments = False, {}
        self.blob = hashlib.sha1(
            b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE.encode()
        ).hexdigest()
        self.client = GitHubClient(httpx.MockTransport(self.handle))

    def item(self, payload, number, kind):
        return {
            **payload,
            "number": number,
            "html_url": f"https://github.com/{REPOSITORY}/{kind}/{number}",
            "user": {"login": LOGIN, "id": 101},
            "state": "open",
        }

    def handle(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        data = json.loads(request.content) if request.content else {}
        if path == "/user":
            result = {"id": 101, "login": self.login, "type": "User"}
        elif path == f"/repos/{REPOSITORY}":
            result = {
                "id": 99,
                "full_name": self.name,
                "default_branch": "actual-main",
                "permissions": {"push": self.push},
            }
        elif "/git/ref/heads/" in path:
            ref = path.split("/heads/")[1]
            commit = BASE if ref == "actual-main" else self.refs.get(ref)
            if not commit:
                return httpx.Response(404)
            result = {"object": {"sha": commit}}
        elif path.endswith("/git/refs"):
            assert data["ref"].startswith("refs/heads/feature/")
            self.refs[data["ref"].removeprefix("refs/heads/")] = data["sha"]
            result = {"object": {"sha": data["sha"]}}
        elif "/git/trees/" in path:
            result = {
                "truncated": False,
                "tree": [
                    {
                        "path": PATH,
                        "type": "blob",
                        "mode": "100644",
                        "size": len(SOURCE),
                        "sha": self.blob,
                    }
                ],
            }
        elif "/git/blobs/" in path:
            result = {"content": base64.b64encode(SOURCE.encode()).decode()}
        elif path.endswith("/git/commits"):
            assert data["parents"] == [BASE]
            result = {"sha": "c" * 40}
        elif "/git/commits/" in path:
            result = {"tree": {"sha": "b" * 40}}
        elif path.endswith(("/git/blobs", "/git/trees")):
            result = {"sha": "b" * 40}
        elif path.endswith("/issues"):
            if method == "POST":
                assert not self.refs
                self.issues.append(self.item(data, len(self.issues) + 1, "issues"))
                if self.lose_issue:
                    self.lose_issue = False
                    raise httpx.ReadError("synthetic lost issue response")
                result = self.issues[-1]
            else:
                assert request.url.params["state"] == "all"
                result = self.issues
        elif "/issues/" in path and "/comments" not in path:
            result = next(i for i in self.issues if i["number"] == int(path.rsplit("/", 1)[1]))
        elif path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            comments = self.comments.setdefault(number, [])
            if method == "POST":
                identifier = len(comments) + 1
                kind = "pull" if any(pr["number"] == number for pr in self.prs) else "issues"
                comments.append(
                    {
                        **data,
                        "id": identifier,
                        "user": {"id": 101},
                        "html_url": f"https://github.com/{REPOSITORY}/{kind}/{number}#issuecomment-{identifier}",
                    }
                )
                result = comments[-1]
            else:
                result = comments
        elif path.endswith("/pulls"):
            if method == "POST":
                if self.reject_pr:
                    return httpx.Response(403, json={"message": "synthetic refusal"})
                assert self.issues and data["base"] == "actual-main"
                item = self.item(data, len(self.prs) + 1, "pull")
                item["head"] = {
                    "ref": data["head"],
                    "sha": self.refs[data["head"]],
                    "repo": {"full_name": REPOSITORY},
                }
                item["base"] = {"ref": data["base"]}
                self.prs.append(item)
                if self.lose_pr:
                    self.lose_pr = False
                    raise httpx.ReadError("synthetic lost response")
                result = item
            else:
                result = [] if self.hide_pr else self.prs
        elif "/pulls/" in path:
            result = self.prs[int(path.rsplit("/", 1)[1]) - 1]
        else:
            raise AssertionError((method, path))
        return httpx.Response(200, json=result)


class Runtime:
    def readiness(self):
        return {"ready": True}

    def image(self, language, check):
        check()
        return "synthetic"

    def seed(self, env, source, check):
        check()
        assert (source / PATH).is_file()

    def cleanup(self, env):
        pass

    def run(self, env, action, check):
        check()
        return (1 if action == "test" and env["id"].endswith("-1") else 0), "1 test executed"


class Model(ReviewModel):
    def __init__(self):
        self.calls, self.reviews, self.submissions = 0, 0, 0
        self.classification, self.workspace = "internal_defect", None
        self.reject_once = True

    def status(self):
        return {"enabled": True}

    async def classify_maintenance(self, packet, event):
        result = answer(packet, self.classification)

        class DiagnosisModel(ReviewModel):
            async def get_response(self, system_instructions, input, **kwargs):
                assert not kwargs["tools"] and not kwargs["handoffs"]
                return final_message(json.dumps(result))

        return await classify_maintenance(DiagnosisModel(), packet, event)

    async def repair(self, context, event, *, workspace):
        assert context["system_maintenance"] is True
        assert context["confirmation"]["authorization"] == "maintenance_policy"
        self.workspace = workspace
        return await run_repair(self, context, event, workspace=workspace)

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        tools = {tool.name for tool in kwargs["tools"]}
        if kwargs["handoffs"]:
            return tool(self.calls, kwargs["handoffs"][0].tool_name, {})
        if "submit_and_test_repair" in tools:
            if not self.workspace.reads:
                return tool(self.calls, "read_repair_source", {"path": PATH})
            self.submissions += 1
            patch = {
                "summary": "修复双调用限制。",
                "edits": [
                    {
                        "path": PATH,
                        "original_sha256": digest(SOURCE.encode()),
                        "content": ("def bounded(value):\n"
                                    f"    return value <= 2  # revision {self.submissions}\n"),
                    },
                    {
                        "path": "tests/test_maintenance_regression.py",
                        "original_sha256": "",
                        "content": ("from repowatch.sample import bounded\n\n"
                                    "def test_two():\n    assert bounded(2)\n"),
                    },
                ],
                "regression_paths": ["tests/test_maintenance_regression.py"],
                "regression_command": "python -m pytest -q tests/test_maintenance_regression.py",
            }
            return tool(self.calls, "submit_and_test_repair", {"patch": patch})
        self.reviews += 1
        return final_message(
            json.dumps(
                {
                    "approved": not self.reject_once or self.reviews > 1,
                    "summary": "独立核对补丁与验证证据。",
                    "risks": [],
                }
            )
        )


@pytest.fixture
def maintenance(tmp_path):
    store, remote, model = Store(tmp_path / "maintenance.db"), Remote(), Model()
    github = GitHubAccounts(store, client=remote.client, vault=MemoryVault())
    github.connect(LOGIN, TOKEN_A)
    root = tmp_path / "workspaces"
    root.mkdir()
    execution = SimpleNamespace(store=store, root=root, runtime=Runtime())
    repairs = SimpleNamespace(_recovery_binding=lambda *_: "basis")

    def workspace_factory(execution, task, token, check):
        assert remote.issues, "Issue must precede source checkout"
        check()
        checkout = root / task["id"]
        source = checkout / "source"
        (source / "repowatch").mkdir(parents=True)
        (source / PATH).write_bytes(SOURCE.encode())
        runs = checkout / "runs"
        runs.mkdir()
        facade = SimpleNamespace(root=runs, runtime=execution.runtime, directory=lambda _: checkout)
        env = {
            "id": task["id"],
            "config": {"language": "python", "setup": "true", "test": "pytest"},
        }
        workspace = RepairWorkspace(
            facade, task["id"].replace("maintenance_", "repair_"), env, check,
        )
        workspace.additional_profiles = [
            {"name": name, "config": dict(env["config"])}
            for name in ("frontend_build", "python_static")
        ]
        return workspace

    service = Maintenance(execution, model, github, repairs, workspace_factory=workspace_factory)
    return service, remote, model


def fault(service, category="protocol"):
    with service.store.connection() as db:
        enqueue(
            db,
            FaultEvent(
                source_event_key="fixture:" + str(time.time_ns()),
                flow="service",
                task_id="worker",
                source_table="worker",
                source_id="fixture",
                occurred=time.time(),
                category=category,
                code="analysis_scope_batch_invalid",
                execution="not_executed",
                diagnostic={"reason": "too_many_calls", "calls": 2, "allowed": 1},
            ),
        )
    SystemFaults(service.store).tick()


def enabled(maintenance):
    service, _, _ = maintenance
    service.configure(version=1, enabled=True)
    fault(service)
    return service


def test_default_off_and_categorical_exclusions_do_not_create_business_confirmation(maintenance):
    service, remote, model = maintenance
    assert not service.state()["enabled"] and service.state()["merge"] == "owner_only"
    fault(service)
    asyncio.run(service.tick())
    assert not service.list()
    service.configure(version=1, enabled=True)
    fault(service, "model_timeout")
    asyncio.run(service.tick())
    assert service.list()[0]["state"] == "not_internal" and not remote.issues and not model.calls
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM analysis_findings").fetchone()[0] == 0


def test_wrong_identity_repository_and_permissions_cannot_enable(maintenance):
    service, remote, _ = maintenance
    for field, invalid, restored in (
        ("login", "someone-else", LOGIN),
        ("push", False, True),
        ("name", "Gwex323/Other", REPOSITORY),
    ):
        setattr(remote, field, invalid)
        with pytest.raises(Conflict):
            service.configure(version=1, enabled=True)
        setattr(remote, field, restored)
    assert not remote.issues and not service.execution.root.joinpath("system-maintenance").exists()


def test_coordinator_route_does_not_invalidate_claimed_maintenance(maintenance):
    service = maintenance[0]
    providers = ModelProviders(service.store, service.github)
    saved = create(providers)
    configure(providers, {"default": choice(saved)})
    enabled(maintenance)
    task = service.claim()
    providers.select(version=providers.state()["version"], role="system_coordinator",
                     choice=choice(saved, "specialized-model"))
    service.guard(task)
    providers.select(version=providers.state()["version"], role="repair_agent",
                     choice=choice(saved, "specialized-model"))
    with pytest.raises(Conflict, match="maintenance_basis_changed"):
        service.guard(task)


def test_existing_maintenance_basis_is_not_blindly_rebound(maintenance):
    service = enabled(maintenance)
    task = service.claim()
    with service.store.connection() as db:
        occurrence = db.execute("SELECT * FROM system_fault_occurrences WHERE id=?",
                                (task["occurrence_id"],)).fetchone()
        task["basis"] = service.basis(db, occurrence, legacy_routing=True)
        db.execute("UPDATE maintenance_tasks SET basis=? WHERE id=?", (task["basis"], task["id"]))
    service.guard(task)
    with service.store.connection() as db:
        db.execute("UPDATE agent_model_settings SET version=version+1")
    with pytest.raises(Conflict, match="maintenance_basis_changed"):
        service.guard(task)


def test_internal_chain_rejects_then_retests_and_publishes_chinese_pr_without_resolving(
    maintenance,
):
    service = enabled(maintenance)
    asyncio.run(service.tick())
    task = service.list()[0]
    assert task["state"] == "pr_created", task["error"]
    assert len(task["artifacts"]["review_history"]) == 2
    assert [r["review"]["approved"] for r in task["artifacts"]["review_history"]] == [False, True]
    assert task["budget"]["requests"] == task["requests"] > 1
    remote = maintenance[1]
    assert len(remote.issues) == len(remote.prs) == 1
    assert all(
        section in remote.prs[0]["body"]
        for section in (
            "## 概述",
            "## 变更内容",
            "## 设计方案/说明",
            "## 验证/验收",
            "## review记录",
            "Refs #1",
        )
    )
    remote.prs[0]["merged_at"] = "2026-10-29T00:00:00Z"
    assert service.reconcile(task["id"], task["version"])["state"] == "waiting_deployment"
    with service.store.connection() as db:
        assert db.execute("SELECT state FROM system_fault_occurrences").fetchone()[0] == "blocked"
    assert not any("merge" in path for _, path in remote.calls)


def test_title_only_match_is_not_reused_and_lost_pr_receipt_is_not_reposted(maintenance):
    service = enabled(maintenance)
    remote = maintenance[1]
    remote.issues.append(
        remote.item({"title": "自身维护：修复调用批次上限", "body": "完全不同的根因"}, 1, "issues")
    )
    remote.lose_pr = True
    asyncio.run(service.tick())
    task = service.list()[0]
    assert task["state"] == "blocked" and len(remote.issues) == 2 and len(remote.prs) == 1
    assert service.retry_publication(task["id"], task["version"])["state"] == "pr_created"
    assert sum(method == "POST" and path.endswith("/pulls") for method, path in remote.calls) == 1


def test_unproven_classification_and_post_review_mutation_cannot_publish(maintenance):
    service = enabled(maintenance)
    maintenance[2].classification = "target_defect"
    asyncio.run(service.tick())
    assert service.list()[0]["state"] == "not_internal" and not maintenance[1].issues
    maintenance[2].classification = "internal_defect"
    fault(service)
    asyncio.run(service.tick())
    workspace = maintenance[2].workspace
    workspace.check = lambda: None
    workspace.verification["passed"] = False
    with pytest.raises(Conflict, match="maintenance_review_stale"):
        approved_artifacts(workspace, workspace.review)
    workspace.verification["passed"] = True
    (workspace.candidate / PATH).write_text("print('modified after review')", encoding="utf-8")
    with pytest.raises(Conflict, match="maintenance_candidate_changed"):
        approved_artifacts(workspace, workspace.review)
    packet = {
        "facts": {"fault.code": "x", "fault.execution": "not_executed"},
        "sources": {PATH: SOURCE},
    }
    with pytest.raises(Conflict, match="maintenance_internal_evidence_required"):
        diagnosis_for({**answer(packet), "quote": "invented source"}, packet)


def test_lost_issue_response_is_reconciled_without_reposting_or_checkout(maintenance):
    service = enabled(maintenance)
    remote = maintenance[1]
    remote.lose_issue = True
    asyncio.run(service.tick())
    task = service.list()[0]
    assert task["state"] == "blocked" and task["issue"] is None and len(remote.issues) == 1
    assert service.reconcile(task["id"], task["version"])["issue"]["number"] == 1
    assert sum(method == "POST" and path.endswith("/issues") for method, path in remote.calls) == 1
    assert not list(service.execution.root.iterdir())


def test_policy_revocation_stops_pending_model_and_no_issue_is_created(maintenance):
    service = enabled(maintenance)

    async def waiting(packet, event):
        event("request_started", "")
        service.configure(version=2, enabled=False)
        await asyncio.sleep(0.2)
        raise AssertionError("cancel expected")

    service.model.classify_maintenance = waiting
    asyncio.run(service.tick())
    assert service.list()[0]["error"] == "maintenance_basis_changed" and not maintenance[1].issues


def test_manual_root_evidence_reuses_issue_and_pr_with_durable_comments(maintenance):
    service = enabled(maintenance)
    remote = maintenance[1]
    body = f"{PATH} bounded analysis_scope_batch_invalid allow_two_calls"
    remote.issues.append(remote.item({"title": "人工登记的问题", "body": body}, 1, "issues"))
    item = remote.item({"title": "已在处理", "body": body + " Refs #1"}, 2, "pull")
    item["head"] = {"ref": "feature/existing", "repo": {"full_name": REPOSITORY}}
    remote.prs.append(item)
    asyncio.run(service.tick())
    task = service.list()[0]
    assert task["state"] == "linked_pr", task["error"]
    assert len(remote.issues) == len(remote.prs) == 1
    assert set(remote.comments) == {1, 2}
    assert not maintenance[2].calls


def test_publication_retry_rechecks_branch_after_complete_receipt(maintenance):
    service = enabled(maintenance)
    remote = maintenance[1]
    remote.reject_pr = True
    asyncio.run(service.tick())
    task = service.list()[0]
    assert task["state"] == "blocked" and remote.refs[task["branch"]] == "c" * 40
    remote.refs[task["branch"]] = "d" * 40
    remote.reject_pr = False
    result = service.retry_publication(task["id"], task["version"])
    assert result["state"] == "blocked" and result["error"] == "maintenance_branch_conflict"
    assert not remote.prs


def test_analysis_protocol_stop_is_not_a_budget_reset_or_maintenance_veto(maintenance):
    from repowatch.coordination import source_budget
    from repowatch.report_scheduler import begin

    service = maintenance[0]
    repo = service.store.add_repository("fixture/project", "https://github.com/fixture/project")
    now = time.time()
    with service.store.connection() as db:
        db.execute(
            "INSERT INTO environments(id,repo_id,account_id,account_version,created,updated) "
            "VALUES('env_fixture',?,101,1,?,?)",
            (repo["id"], now, now),
        )
        db.execute(
            "INSERT INTO repository_analyses(id,env_id,repo_id,revision,fingerprint,trigger_key,"
            "status,created,updated) "
            "VALUES('analysis_fixture','env_fixture',?,?,'f','t','failed',?,?)",
            (repo["id"], BASE, now, now),
        )
        begin(db, "analysis_fixture", now)
        db.execute("UPDATE analysis_report_recovery SET stop_reason='non_recoverable',requests=5")
        occurrence = {"flow": "analysis", "task_id": "analysis_fixture"}
        source_budget(db, occurrence, reserve=True, maintenance=True)
        assert db.execute("SELECT requests FROM analysis_report_recovery").fetchone()[0] == 6
        source_budget(db, occurrence, capacity=True)
        db.execute("UPDATE analysis_report_recovery SET requests=32")
        source_budget(db, occurrence, reserve=True, maintenance=True)
        assert db.execute("SELECT requests FROM analysis_report_recovery").fetchone()[0] == 33


def test_git_workspace_is_fresh_feature_and_never_overwrites_existing_checkout(
    maintenance, tmp_path, monkeypatch
):
    service = enabled(maintenance)
    task = service.claim()
    task.update(
        base=BASE,
        default_branch="actual-main",
        branch="feature/" + task["id"].replace("_", "-"),
        issue={"number": 1},
    )
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-b", "actual-main"], cwd=origin, check=True, capture_output=True
    )
    (origin / "sample.py").write_text(SOURCE, encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=origin, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    task["base"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=origin, text=True
    ).strip()
    from repowatch import maintenance_workspace

    original = maintenance_workspace.git

    def local_git(directory, args, token, check):
        if args[0] == "fetch":
            assert args[3] == "https://github.com/" + REPOSITORY + ".git"
            return subprocess.check_output(
                ["git", "-c", "protocol.file.allow=always", *args[:3], str(origin), *args[4:]],
                cwd=directory,
                text=True,
            )
        return original(directory, args, token, check)

    monkeypatch.setattr(maintenance_workspace, "git", local_git)
    workspace = create_workspace(service.execution, task, "synthetic", lambda: None)
    assert workspace.env["config"]["install"] == "pip install '.[dev]'"
    assert workspace.env["config"]["install_network"] is True
    assert (workspace.original / "sample.py").read_text() == SOURCE
    checkout = service.execution.root / "system-maintenance" / task["id"] / "checkout" / "source"
    (checkout / "sample.py").write_text("user change", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_workspace(service.execution, task, "synthetic", lambda: None)
    assert (checkout / "sample.py").read_text() == "user change"
    assert (
        subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=checkout, text=True
        ).strip()
        == task["branch"]
    )


def test_maintenance_api_requires_login_csrf_and_version(tmp_path):
    app = create_app(tmp_path / "api.db", start_worker=False, credential_vault=MemoryVault())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/system/maintenance/tasks").status_code == 401
        client.post("/api/setup", json={"password": "test-password-123"})
        assert client.get("/api/system/maintenance").json()["enabled"] is False
        assert (
            client.post(
                "/api/system/maintenance/settings", json={"enabled": False, "version": 1}
            ).status_code
            == 403
        )
        csrf = client.get("/api/session").json()["csrf"]
        response = client.post(
            "/api/system/maintenance/settings",
            headers={"X-CSRF-Token": csrf},
            json={"enabled": False, "version": 999},
        )
        assert response.status_code == 409
