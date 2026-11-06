import json
import os
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from test_roles import bind, diagnose
from test_roles import kernel as kernel

from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts, BudgetDecision, BudgetDenied, RenewalPlan
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime
from repowatch.runtime.git_handoff import create_fixture
from repowatch.worker.repairs import RepairCoordinator


@pytest.mark.skipif(os.environ.get("REPOWATCH_DOCKER_TESTS") != "1", reason="显式 Docker 验收")
@pytest.mark.parametrize(
    ("external", "kind"),
    [
        ("none", "bug"),
        ("resolved", "bug"),
        ("unresolved", "bug"),
        ("none", "feature"),
        ("none", "refactor"),
        ("stuck", "bug"),
        ("sdk", "bug"),
        ("supplement", "bug"),
        ("review_retry", "bug"),
        ("renew", "bug"),
        ("claim", "bug"),
        ("claim", "feature"),
        ("claim", "refactor"),
    ],
)
def test_real_docker_repair_probe_freeze_review_and_evidence(
    kernel, tmp_path, monkeypatch, external, kind
):
    services = kernel
    bind(services)
    bind(services, "reviewer")
    if external == "sdk":
        diagnose(services, "processor")
        diagnose(services, "reviewer")
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace(read=lambda reference: None)
    repo = services.tasks.register_repository("repair-fixture", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", issue_id="101", now=0)
    inputs = {
        "number": 1,
        "id": 101,
        "github_id": "101",
        "title": "sum",
        "body": "sum fails",
        "state": "open",
        "labels": [],
        "assignees": [],
    }
    services.tasks.observe(task, "issue:1", "v1", inputs, now=0)
    content = {"objective": "correct sum", "criteria": ["sum(2,3) equals 5"]}
    if kind in {"feature", "refactor"}:
        content["plan"] = "change addition implementation"
    if kind == "refactor":
        content.update(
            structural_goal="use add function", behavior_preservation="preserve identity"
        )
    services.tasks.operate(
        "acceptance",
        task,
        expected_revision=2,
        action="set_acceptance",
        payload={
            "kind": kind,
            "content": content,
        },
        now=0,
    )
    if kind != "bug":
        with services.store.read() as records:
            revision = records.one("tasks", id=task)["revision"]
        services.tasks.operate(
            "approve", task, expected_revision=revision, action="approve_acceptance", now=0
        )
    calls = []
    supplement_requested = False
    candidate_reviews = 0

    class Runtime:
        def run(self, token, plan):
            nonlocal supplement_requested, candidate_reviews
            calls.append((plan.mode, token.run_id))
            bindings = {item.tool.name: item.tool for item in plan.tools}
            events = []
            if plan.mode == "reproduce":
                bindings["read_source"].invoke({"path": "calc.py"})
                bindings["write_analysis"].invoke(
                    {
                        "plan": {
                            "files": {
                                "tests/probe.py": (
                                    "from calc import add\nvalue = add(2,3)\n"
                                    "if value != 5:\n print('TARGET_BUG')\n"
                                    " raise SystemExit(1)\nprint('TARGET_OK')\n"
                                )
                            },
                            "argv": ["python", "-c", "exec(open('tests/probe.py').read())"],
                            "target_failure": "TARGET_BUG",
                            "target_success": "TARGET_OK",
                            "purpose": "sum regression",
                            "behavior_argv": [
                                "python",
                                "-c",
                                "from calc import add; assert add(2, 0) == 2",
                            ]
                            if kind == "refactor"
                            else [],
                        }
                    }
                )
                events.append({"tool": "read_source"})
                summary = "planned"
            elif plan.mode == "implement":
                if external == "renew" and sum(mode == "implement" for mode, _ in calls) == 2:
                    raise BudgetDenied(BudgetDecision("renewal_required"))
                bindings["read_source"].invoke({"path": "calc.py"})
                if external != "stuck":
                    bindings["write_source"].invoke(
                        {
                            "path": "calc.py",
                            "content": "def add(left, right):\n return left + right\n",
                        }
                    )
                summary = "implemented"
            else:
                review_input = json.loads(bindings["read_evidence"].invoke({}))
                if external == "supplement" and not supplement_requested:
                    bindings["request_check"].invoke(
                        {
                            "argv": ["python", "-c", "print('SUPPLEMENT_OK')"],
                            "timeout": 30,
                        }
                    )
                    supplement_requested = True
                if review_input["phase"] == "candidate":
                    candidate_reviews += 1
                review_passed = not (
                    external in {"review_retry", "renew"}
                    and review_input["phase"] == "candidate"
                    and candidate_reviews == 1
                )
                summary = canonical_json(
                    {
                        "passed": review_passed,
                        "probe_valid": True,
                        "weakened_acceptance": False,
                        "findings": [] if review_passed else ["adjust implementation style"],
                        "unrelated_failures": [],
                        "rationale": "independent fixture review",
                    }
                )
            result = SimpleNamespace(summary=summary, status="completed")
            plan.verifier(result, events)
            return result

    coordinator = RepairCoordinator(services, Runtime())
    if external == "sdk":

        def wire(request):
            body = json.loads(request.content)
            envelope = next(
                json.loads(item["content"])
                for item in body["messages"]
                if item["role"] == "user"
                and isinstance(item.get("content"), str)
                and item["content"].startswith('{"protocol_version"')
            )
            mode = envelope["mode"]
            results = [item for item in body["messages"] if item["role"] == "tool"]
            if not results:
                calls.append((mode, envelope["context"]["run_id"]))
                name = "read_evidence" if mode == "review" else "read_source"
                arguments = {} if mode == "review" else {"path": "calc.py"}
            elif len(results) == 1 and mode in {"reproduce", "implement"}:
                if mode == "implement":
                    name, arguments = (
                        "write_source",
                        {
                            "path": "calc.py",
                            "content": "def add(left, right):\n return left + right\n",
                        },
                    )
                else:
                    name, arguments = (
                        "write_analysis",
                        {
                            "plan": {
                                "files": {
                                    "tests/probe.py": (
                                        "from calc import add\n"
                                        "if add(2,3) != 5:\n print('TARGET_BUG')\n"
                                        " raise SystemExit(1)\n"
                                        "print('TARGET_OK')\n"
                                    )
                                },
                                "argv": ["python", "-c", "exec(open('tests/probe.py').read())"],
                                "target_failure": "TARGET_BUG",
                                "target_success": "TARGET_OK",
                                "purpose": "sum regression",
                            }
                        },
                    )
            else:
                summary = (
                    canonical_json(
                        {
                            "passed": True,
                            "probe_valid": True,
                            "weakened_acceptance": False,
                            "findings": [],
                            "unrelated_failures": [],
                            "rationale": "fixture review",
                        }
                    )
                    if mode == "review"
                    else "completed"
                )
                name, arguments = (
                    "StructuredResult",
                    {
                        "context": envelope["context"],
                        "mode": mode,
                        "status": "completed",
                        "summary": summary,
                        "artifacts": [],
                        "unresolved": [],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": str(len(results)),
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )

        services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
        coordinator.runtime = FormalRuntime(services)
    fixture = create_fixture(tmp_path / "git")
    original = coordinator.objects.import_bundle(repo, fixture["bundle"])
    implementation = b"def add(left, right):\n return left - right\n"
    if kind == "refactor":
        implementation = b"def add(left, right):\n return sum([left, right])\n"
        original_run = Runtime.run

        def structural_probe(self, token, plan):
            if plan.mode != "reproduce":
                return original_run(self, token, plan)
            calls.append((plan.mode, token.run_id))
            bindings = {item.tool.name: item.tool for item in plan.tools}
            bindings["read_source"].invoke({"path": "calc.py"})
            bindings["write_analysis"].invoke(
                {
                    "plan": {
                        "files": {
                            "tests/probe.py": (
                                "import inspect\nfrom calc import add\n"
                                "if 'left + right' not in inspect.getsource(add):\n"
                                " print('TARGET_BUG')\n raise SystemExit(1)\nprint('TARGET_OK')\n"
                            )
                        },
                        "argv": ["python", "-c", "exec(open('tests/probe.py').read())"],
                        "target_failure": "TARGET_BUG",
                        "target_success": "TARGET_OK",
                        "purpose": "remove intermediate collection while preserving addition",
                        "behavior_argv": [
                            "python",
                            "-c",
                            "from calc import add; assert add(2, 3) == 5",
                        ],
                    }
                }
            )
            result = SimpleNamespace(summary="planned", status="completed")
            plan.verifier(result, [{"tool": "read_source"}])
            return result

        monkeypatch.setattr(Runtime, "run", structural_probe)
    if external == "resolved":
        implementation = b"def add(left, right):\n return left + right\n"
    base = coordinator.objects.commit_files(
        repo,
        original,
        {"calc.py": ("100644", implementation)},
        message="fixture",
    )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repo},
            {
                "target_sha": base["sha"],
                "scan_ready_sha": base["sha"],
                "github_owner": "fixture",
                "github_name": "fixture",
                "target_branch": "main",
            },
        )
        state = records.one("tasks", id=task)
        records.change(
            "tasks",
            {"id": task},
            {
                "mode": "implement",
                "configuration_json": canonical_json(
                    {"workspace_image": "python:3.13-slim-bookworm"}
                ),
            },
        )
        records.add(
            "triage_decisions",
            {
                "id": "decision",
                "task_id": task,
                "input_revision": state["input_revision"],
                "feedback_revision": state["feedback_revision"],
                "admission_revision": 1,
                "result": "auto_repair",
                "reason_code": "bug",
                "evidence_json": canonical_json(
                    {
                        "analysis": {
                            "kind": kind,
                            "objective": "correct sum",
                            "criteria": ["sum(2,3) equals 5"],
                            "reproduction_steps": ["call add"],
                            "ownership": "available",
                            "ownership_evidence": ["no claim"],
                            "plan": content.get("plan", ""),
                            "structural_goal": content.get("structural_goal", ""),
                            "behavior_preservation": content.get("behavior_preservation", ""),
                            "pr_relations": [
                                {"id": "501", "relation": "same", "evidence": "linked fix"}
                            ]
                            if external in {"resolved", "unresolved"}
                            else [],
                        }
                    }
                ),
            },
        )
    services.repositories = SimpleNamespace(require_ready=lambda *args, **kwargs: True)
    remote = {
        "digest": "fixed",
        "facts": {"issue": inputs, "pull_requests": []},
        "decisions": {},
        "managed_pr_id": None,
    }
    if external in {"resolved", "unresolved"}:
        remote["facts"]["pull_requests"] = [
            {
                "id": 501,
                "state": "closed",
                "merged_at": "2026-11-06T00:00:00Z",
                "merge_commit_sha": base["sha"],
                "base": {"ref": "main"},
            }
        ]
    if external != "claim":
        monkeypatch.setattr(
            "repowatch.worker.repairs.IssueContexts.read", lambda self, token: remote
        )
    else:
        remote["facts"].update(comments=[], complete=True)

        class ClaimPort:
            def __init__(self, **kwargs):
                pass

            def _path(self, owner, name):
                return "/repos/fixture/fixture"

            def issue_context(self, *args):
                return deepcopy(remote["facts"])

            def write(self, method, path, body):
                comment = {"id": 501, "body": body["body"], "user": {"login": "worker"}}
                remote["facts"]["comments"].append(comment)
                return comment

        monkeypatch.setattr("repowatch.application.issue_context.GitHubReadAdapter", ClaimPort)
        monkeypatch.setattr("repowatch.application.publication.GitHubPublisher", ClaimPort)
    monkeypatch.setattr(coordinator.objects, "fetch", lambda *args, **kwargs: base["sha"])

    def published(publisher, token, session_id, context):
        if external == "claim":
            from repowatch.application.acceptance import acceptance_view

            with services.store.read() as records:
                current = records.one("tasks", id=task)
                current_acceptance = acceptance_view(records, current)
                assert current["feedback_revision"] == state["feedback_revision"]
                assert current_acceptance["current"]
                if kind != "bug":
                    assert current_acceptance["approved"]
                assert (
                    records.one("publication_intents", task_id=task, kind="claim")["state"]
                    == "confirmed"
                )
            assert len(context["facts"]["comments"]) == 1
        session, frozen = publisher.evidence.require_current(token, session_id)
        assert session["state"] == "verified"
        evidence = json.loads(session["evidence_json"])
        assert evidence["base_probe"]["exit_code"] == 1
        assert evidence["candidate_probe"]["exit_code"] == 0
        assert evidence["review_run_id"] != token.run_id
        with pytest.raises(PermissionError, match="两侧真实"):
            publisher.evidence.confirm(token, session_id, {**evidence, "base_probe": {}})
        with pytest.raises(PermissionError, match="不一致"):
            publisher.evidence.confirm(
                token,
                session_id,
                {**evidence, "review": {**evidence["review"], "rationale": "forged"}},
            )
        publisher.evidence.confirm(token, session_id, evidence)
        workspace = coordinator.workspaces.require(token, session["workspace_id"], now=0)
        with pytest.raises(PermissionError):
            coordinator.backend.write_source(workspace, task, "calc.py", b"wrong")
        changed = {**workspace, "mode": "implement"}
        coordinator.backend.write_source(changed, task, "untracked.py", b"unexpected")
        with pytest.raises(PermissionError, match="内容变化"):
            publisher.evidence.require_current(token, session_id)
        services.queue.complete(token, now=0, state="waiting", wait_reason="pr_review")
        return {"state": "waiting", "reason": "fixture_publication"}

    monkeypatch.setattr("repowatch.application.publication.Publisher.publish", published)
    services.budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=3000000, model_calls=30, tokens=1000000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0, ttl=300)
    if external == "claim":
        with services.store.write() as records:
            records.change(
                "repositories",
                {"id": repo},
                {
                    "identity_json": canonical_json({"execution_login": "worker"}),
                    "policy_json": canonical_json(
                        {"claim_comment": True, "claim_template": "Working on issue"}
                    ),
                },
            )
    try:
        if external == "stuck":
            with pytest.raises(PermissionError, match="无进展"):
                coordinator.run(token)
            with services.store.read() as records:
                assert records.one("global_control", singleton=1)["paused"] == 1
                assert records.one("block_reasons", task_id=task, code="no_progress")
                assert not records.find("publication_intents", task_id=task)
                assert len(records.find("progress_attempts", task_id=task)) == 4
            return
        if external == "renew":
            with pytest.raises(BudgetDenied):
                coordinator.run(token)
            result = services.budgets.request_renewal(
                token,
                RenewalPlan(("resolve remaining review finding",), "keep scope", "review passes"),
                now=0,
            )
            with services.store.read() as records:
                assert records.one("global_control", singleton=1)["paused"] == 0
                assert records.one("tasks", id=task)["wait_reason"] == "budget_renewal"
                assert records.find("budget_progress", owner_task_id=task)
                assert len(records.find("progress_attempts", task_id=task)) == 1
                assert not records.find("publication_intents", task_id=task)
            assert result
            assert services.budgets.view(task)["used"]["repair_rounds"] == 2
            return
        result = coordinator.run(token)
        if external == "resolved":
            assert result == {"state": "ended", "reason": "externally_resolved"}
            assert [mode for mode, _ in calls] == ["reproduce", "review"]
            assert services.budgets.view(task)["used"]["repair_rounds"] == 0
            with services.store.read() as records:
                assert records.one("tasks", id=task)["outcome"] == "externally_resolved"
                assert not records.find("publication_intents", task_id=task)
                assert not records.find("repair_sessions", task_id=task)
                assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
            return
        assert result["reason"] == "fixture_publication"
        expected_modes = ["reproduce", "review", "implement", "review"]
        if external == "supplement":
            expected_modes.insert(2, "review")
        if external == "review_retry":
            expected_modes.extend(["implement", "review"])
        assert [mode for mode, _ in calls] == expected_modes
        reviews = [run_id for mode, run_id in calls if mode == "review"]
        assert len(set(reviews)) == len(reviews)
        assert token.run_id not in reviews
        assert services.budgets.view(task)["used"]["repair_rounds"] == (
            2 if external == "review_retry" else 1
        )
        with services.store.read() as records:
            assert len(records.find("distillation_intents", task_id=task)) == 1
    finally:
        with services.store.read() as records:
            workspaces = list(records.find("workspaces", task_id=task))
        for workspace in workspaces:
            snapshot = json.loads(workspace["snapshot_json"])
            if coordinator.backend.exists(snapshot):
                coordinator.backend.remove(snapshot)
