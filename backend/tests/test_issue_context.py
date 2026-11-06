from types import SimpleNamespace

import httpx
import pytest
from test_roles import kernel as kernel

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.application.github_ports import GitHubReadError
from repowatch.application.github_reads import issue_input
from repowatch.application.issue_context import IssueContexts
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.triage import TriageAnalysis, classify_analysis
from repowatch.worker.repairs import RepairCoordinator


def test_issue_context_reads_all_comments_open_prs_and_merged_timeline_pr():
    calls = []

    def wire(request):
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/issues/1"):
            body = {"id": 10, "number": 1, "state": "open", "body": "bug"}
        elif path.endswith("/comments"):
            body = [{"id": 20, "body": "working on this", "author_association": "MEMBER"}]
        elif path.endswith("/timeline"):
            body = [
                {
                    "source": {
                        "issue": {
                            "number": 3,
                            "repository_url": "https://api.github.com/repos/owner/repo",
                            "pull_request": {},
                        }
                    }
                }
            ]
            body[0]["source"]["issue"]["pull_request"] = {"url": "linked"}
        elif path.endswith("/pulls"):
            body = [{"number": 2}]
        elif path.endswith("/files"):
            body = [{"filename": "source.py", "patch": "fix"}]
        elif path.endswith("/pulls/2"):
            body = {"id": 22, "number": 2, "state": "open"}
        elif path.endswith("/pulls/3"):
            body = {"id": 33, "number": 3, "state": "closed", "merged_at": "2026-11-06T00:00:00Z"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body)

    result = GitHubReadAdapter(transport=httpx.MockTransport(wire)).issue_context(
        "owner", "repo", 1
    )
    assert result["complete"]
    assert [item["id"] for item in result["pull_requests"]] == [22, 33]
    assert calls.count("/repos/owner/repo/issues/1") == 2


def test_incomplete_relation_read_is_not_treated_as_no_pr():
    def wire(request):
        return httpx.Response(200, json=[], headers={"Link": '<next>; rel="next"'})

    with pytest.raises(GitHubReadError, match="collection_incomplete"):
        GitHubReadAdapter(transport=httpx.MockTransport(wire)).collection("/fixture", max_pages=2)


@pytest.mark.parametrize(
    ("relation", "state", "merged", "expected"),
    [
        ("same", "open", False, "external_repair"),
        ("unrelated", "open", False, "auto_repair"),
        ("uncertain", "open", False, "needs_information"),
        ("same", "closed", True, "auto_repair"),
    ],
)
def test_external_pr_relation_controls_wait_and_does_not_use_file_overlap(
    relation, state, merged, expected
):
    analysis = TriageAnalysis(
        kind="bug",
        objective="fix",
        criteria=["probe"],
        reproduction_steps=["run"],
        ownership="available",
        ownership_evidence=["no maintainer claims"],
        pr_relations=[
            {"id": "1", "relation": relation, "evidence": "reviewed description and patch"}
        ],
    )
    context = {
        "decisions": {},
        "managed_pr_id": None,
        "facts": {
            "pull_requests": [
                {
                    "id": 1,
                    "state": state,
                    "merged": merged,
                    "merged_at": "2026-11-06T00:00:00Z" if merged else None,
                    "merge_commit_sha": "a" * 40 if merged else None,
                    "base": {"ref": "main"},
                }
            ]
        },
    }
    decision = classify_analysis(analysis, remote_context=context)
    assert decision.result == expected
    if merged:
        assert decision.reason_code == "external_merge_verification"
        assert decision.evidence["merge_commit_sha"] == "a" * 40


@pytest.mark.parametrize(
    ("resolved", "ancestor", "expected"),
    [(True, True, True), (True, False, False), (False, False, False)],
)
def test_merged_external_pr_requires_commit_in_current_target_history(resolved, ancestor, expected):
    class Objects:
        def repository(self, identifier):
            assert identifier == "repo"
            return "fixture"

        def run(self, bare, *arguments):
            if arguments[0:2] == ("rev-parse", "--verify"):
                if not resolved:
                    raise RuntimeError("missing object")
                return b"a" * 40 + b"\n"
            if not ancestor:
                raise RuntimeError("not ancestor")
            return b""

    coordinator = RepairCoordinator.__new__(RepairCoordinator)
    coordinator.objects = Objects()
    repository = {"id": "repo", "target_branch": "main", "target_sha": "b" * 40}
    remote = {
        "facts": {
            "pull_requests": [
                {
                    "id": 7,
                    "merged_at": "2026-11-06T00:00:00Z",
                    "merge_commit_sha": "a" * 40,
                    "base": {"ref": "main"},
                }
            ]
        }
    }
    result = coordinator.merged_pull(repository, remote, "7")
    assert (result is not None) is expected


def test_new_pr_invalidates_feedback_and_hands_active_repair_back_to_triage(kernel, monkeypatch):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace()
    repository = services.tasks.register_repository("context-fixture", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repository, "issue:1", issue_id="101", now=0)
    issue = {"id": 101, "number": 1, "state": "open"}
    services.tasks.observe(task, "issue:1", "v1", issue_input(issue), now=0)
    services.budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=1000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "target_sha": "a" * 40,
                "scan_ready_sha": "a" * 40,
            },
        )
        records.change("tasks", {"id": task}, {"mode": "implement"})
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    facts = {"issue": issue, "comments": [], "pull_requests": [], "complete": True}

    class Port:
        def __init__(self, **kwargs):
            pass

        def issue_context(self, *args):
            return facts

    monkeypatch.setattr("repowatch.application.issue_context.GitHubReadAdapter", Port)
    reader = IssueContexts(services)
    first = reader.read(token)
    facts["pull_requests"] = [{"id": 701, "state": "open", "number": 7}]
    with pytest.raises(ConflictError, match="PR"):
        reader.read(token)
    services.queue.complete(token, now=0, state="queued")
    with services.store.read() as records:
        current = records.one("tasks", id=task)
        assert current["mode"] == "triage"
        assert current["feedback_revision"] == 2
        assert records.one("global_control", singleton=1)["paused"] == 0
        assert len(records.find("issue_contexts", task_id=task)) == 2
        assert len({item["digest"] for item in records.find("issue_contexts", task_id=task)}) == 2
        assert first["digest"] in {
            item["digest"] for item in records.find("issue_contexts", task_id=task)
        }
