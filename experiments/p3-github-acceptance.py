"""Drive RepoWatch's real scan/triage/repair/publish path with a fixture model.

The model transport is deterministic and offline; Git, Docker, the business
database and GitHub writes are real. Run from the repository root.
"""

import argparse
import json
import os
import time
from hashlib import sha256
from pathlib import Path

import httpx
from repowatch.adapters.providers import ProviderAdapter
from repowatch.api.host import HostServices
from repowatch.application.task_records import required
from repowatch.configuration import StorageSettings
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.worker.role_probe import RoleProbe
from repowatch.worker.service import Worker

IMAGE = "python:3.13-slim-bookworm"
TASK_CONFIG = {
    "workspace_image": IMAGE,
    "check_timeout": 60,
    "max_input_tokens": 100000,
}
DEFECTS = {
    "mul": {
        "path": "mul.py",
        "marker": b"left + right",
        "fixed": "def mul(left: int, right: int) -> int:\n    return left * right\n",
        "probe": (
            "from mul import mul\n"
            "value = mul(2, 3)\n"
            "if value != 6:\n"
            "    print('TARGET_BUG')\n"
            "    raise SystemExit(1)\n"
            "print('TARGET_OK')\n"
        ),
        "title": "mul() adds instead of multiplying",
        "basis": "the implementation returns left + right although the contract is a product",
        "objective": "Make mul() return the arithmetic product",
        "criteria": ["mul(2, 3) == 6", "mul(2, 0) == 0"],
        "reproduction": "python -c 'from mul import mul; assert mul(2, 3) == 6'",
    },
    "sub": {
        "path": "sub.py",
        "marker": b"right - left",
        "fixed": "def sub(left: int, right: int) -> int:\n    return left - right\n",
        "probe": (
            "from sub import sub\n"
            "value = sub(5, 2)\n"
            "if value != 3:\n"
            "    print('TARGET_BUG')\n"
            "    raise SystemExit(1)\n"
            "print('TARGET_OK')\n"
        ),
        "title": "sub() subtracts in the wrong order",
        "basis": "the implementation returns right - left although the contract is left - right",
        "objective": "Make sub() return the arithmetic difference",
        "criteria": ["sub(5, 2) == 3", "sub(2, 5) == -3"],
        "reproduction": "python -c 'from sub import sub; assert sub(5, 2) == 3'",
    },
}
ACTIVE = DEFECTS["mul"]
REVIEW = canonical_json(
    {
        "passed": True,
        "probe_valid": True,
        "weakened_acceptance": False,
        "findings": [],
        "unrelated_failures": [],
        "rationale": "fixture reviewer inspected the real probe evidence",
    }
)


def triage():
    return {
        "kind": "bug",
        "objective": ACTIVE["objective"],
        "criteria": ACTIVE["criteria"],
        "reproduction_steps": [ACTIVE["reproduction"]],
        "ownership": "available",
        "ownership_evidence": [
            "no assignee and no maintainer claim in the current input"
        ],
        "issue_relation": {
            "relation": "none",
            "evidence": "the complete issue list has no report about this function",
        },
        "pr_relations": [
            {
                "id": "5183900277",
                "relation": "unrelated",
                "evidence": "direct PR fixes add()",
            },
            {
                "id": "5184152666",
                "relation": "unrelated",
                "evidence": "fork PR fixes add()",
            },
            {
                "id": "5185209138",
                "relation": "unrelated",
                "evidence": "direct PR fixes mul()",
            },
        ],
    }


def message_json(message):
    try:
        return json.loads(message["content"])
    except (KeyError, TypeError, ValueError):
        return None


def envelope_from(messages):
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if content.startswith('{"protocol_version"'):
            return json.loads(content)
        try:
            value = json.loads(content)
        except ValueError:
            continue
        if isinstance(value, dict) and {"session_id", "role"} <= set(value):
            return {"__probe__": value}
    return None


def tool_results(messages):
    return [
        value
        for value in (message_json(m) for m in messages if m.get("role") == "tool")
        if value
    ]


_call_counter = 0


def reply(body, name, arguments):
    global _call_counter
    _call_counter += 1
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
                                "id": "call_" + str(_call_counter),
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


def structured(envelope, summary="completed"):
    return (
        "StructuredResult",
        {
            "context": envelope["context"],
            "mode": envelope["mode"],
            "status": "completed",
            "summary": summary,
            "artifacts": [],
            "unresolved": [],
        },
    )


def install_heartbeat_debug():
    import repowatch.worker.heartbeat as heartbeat_module

    def loop(self):
        while not self.stopped.wait(self.interval):
            self._beat()
            if self.error is not None:
                print("HEARTBEAT_ERROR", repr(self.error), flush=True)
                return

    heartbeat_module.ExecutionHeartbeat._loop = loop


def install_github_debug():
    from repowatch.application.publication import GitHubPublisher

    def write(self, method, path, body):
        secret = self.vault.read(self.reference).reveal()
        with httpx.Client(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=30,
        ) as client:
            response = client.request(
                method,
                self.base_url + path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": "Bearer " + secret,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=body,
            )
        if not response.is_success:
            print(
                "GITHUB_WRITE_FAILED",
                method,
                path,
                response.status_code,
                response.text[:800],
                flush=True,
            )
            raise PermissionError("GitHub 写入未确认，须读取对账")
        return response.json()

    GitHubPublisher.write = write


def install_repair_debug():
    import traceback

    from repowatch.worker.repairs import RepairCoordinator

    original = RepairCoordinator.run

    def run(self, token):
        try:
            return original(self, token)
        except BaseException:
            traceback.print_exc()
            raise

    RepairCoordinator.run = run


def install_tool_debug():
    import traceback

    from repowatch.runtime.engine import RuntimeBoundary

    original = RuntimeBoundary.wrap_tool_call

    def wrap_tool_call(self, request, handler):
        try:
            return original(self, request, handler)
        except BaseException:
            print("TOOL_FAILED", request.tool_call.get("name"), flush=True)
            traceback.print_exc()
            raise

    RuntimeBoundary.wrap_tool_call = wrap_tool_call


def probing_finding(results):
    payload = results[0]
    if "content_hex" in payload:
        content = bytes.fromhex(payload["content_hex"])
    elif isinstance(payload.get("content"), str):
        content = payload["content"].encode("utf-8")
    else:
        raise AssertionError("scan payload lacks content")
    unit = payload["unit"]
    marker = ACTIVE["marker"]
    offset = content.find(marker)
    findings = []
    if offset >= 0:
        start = unit["start"] + offset
        findings.append(
            {
                "title": ACTIVE["title"],
                "basis": ACTIVE["basis"],
                "start": start,
                "end": start + len(marker),
            }
        )
    return findings, unit["sha256"]


def wire(request):
    body = json.loads(request.content)
    envelope = envelope_from(body["messages"])
    if envelope is None:
        raise AssertionError("unrecognised model request")
    if len(request.content) > 15000:
        print(
            "MODEL_REQUEST_SIZE",
            envelope.get("mode", "probe"),
            len(request.content),
            len(body["messages"]),
            flush=True,
        )
    tool_count = sum(1 for item in body["messages"] if item.get("role") == "tool")
    results = tool_results(body["messages"])
    if envelope.get("mode") == "reproduce":
        names = [item.get("function", {}).get("name") for item in body.get("tools", [])]
        print(
            "REPAIR_WIRE",
            tool_count,
            names,
            [item.get("role") for item in body["messages"]],
            (json.dumps(results[-1], ensure_ascii=False)[:300] if results else None),
            flush=True,
        )
    if "__probe__" in envelope:
        probe = envelope["__probe__"]
        if tool_count == 0:
            return reply(body, "read_fixture", {})
        if tool_count == 1:
            if probe["role"] == "processor":
                return reply(body, "write_fixture", {"content": results[0]["fixed"]})
            return reply(body, "read_evidence", {})
        return reply(
            body,
            "RoleProbeResult",
            {
                "session_id": probe["session_id"],
                "role": probe["role"],
                "proof": results[0]["proof"],
            },
        )
    mode = envelope["mode"]
    if mode == "scan":
        if tool_count == 0:
            return reply(body, "read_source", {})
        if tool_count == 1:
            findings, input_sha = probing_finding(results)
            return reply(
                body,
                "write_analysis",
                {
                    "analysis": "Reviewed the complete supplied scan chunk against its contract.",
                    "input_sha256": input_sha,
                    "findings": findings,
                    "complete": True,
                },
            )
        return reply(body, *structured(envelope))
    if mode == "investigate":
        if tool_count == 0:
            return reply(body, "read_evidence", {})
        if tool_count == 1:
            return reply(body, "write_analysis", {"analysis": triage()})
        return reply(body, *structured(envelope))
    if mode == "reproduce":
        if tool_count == 0:
            return reply(body, "read_source", {"path": ACTIVE["path"]})
        if tool_count == 1:
            return reply(
                body,
                "write_analysis",
                {
                    "plan": {
                        "files": {"tests/probe.py": ACTIVE["probe"]},
                        "argv": ["python", "-c", "exec(open('tests/probe.py').read())"],
                        "target_failure": "TARGET_BUG",
                        "target_success": "TARGET_OK",
                        "purpose": "arithmetic sum regression",
                    }
                },
            )
        return reply(body, *structured(envelope))
    if mode == "implement":
        if tool_count == 0:
            return reply(body, "read_source", {"path": ACTIVE["path"]})
        if tool_count == 1:
            return reply(
                body,
                "write_source",
                {"path": ACTIVE["path"], "content": ACTIVE["fixed"]},
            )
        return reply(body, *structured(envelope))
    if mode == "review":
        if tool_count == 0:
            return reply(body, "read_evidence", {})
        return reply(body, *structured(envelope, REVIEW))
    raise AssertionError("unsupported stage mode: " + mode)


class Acceptance:
    def __init__(self, data_dir, owner, name, reference, fork):
        self.settings = StorageSettings(Path(data_dir))
        self.services = HostServices(self.settings, clock=time.monotonic)
        self.services.adapter = ProviderAdapter(
            self.services.providers, transport=httpx.MockTransport(wire)
        )
        self.owner, self.name, self.reference, self.fork = owner, name, reference, fork
        self.worker = Worker(self.services)
        self.namespace = sha256(
            str(self.settings.data_dir).casefold().encode()
        ).hexdigest()[:40]

    def now(self):
        return time.monotonic()

    def log(self, message, **details):
        print(json.dumps({"step": message, **details}, ensure_ascii=False), flush=True)

    def bootstrap_provider(self):
        services = self.services
        providers = services.providers.list()
        if providers:
            provider = providers[0]
        else:
            provider = services.providers.save(
                "p3-provider",
                {
                    "name": "p3-fixture",
                    "base_url": "https://fixture.invalid/v1",
                    "api": "chat_completions",
                    "api_key": "fixture-key",
                    "models": [{"model": "fixture-model"}],
                },
            )["providers"][0]
        for key in ("processor", "reviewer"):
            if not any(item["binding_key"] == key for item in services.roles.list()):
                services.roles.set_binding(
                    "bind-" + key,
                    key,
                    {
                        "role": key,
                        "provider_id": provider["id"],
                        "selection": {
                            "model": "fixture-model",
                            "reasoning_effort": None,
                        },
                    },
                    expected_revision=0,
                )
        for role in ("processor", "reviewer"):
            self.diagnose(role, provider)
        self.log("provider_ready", provider=provider["id"])

    def diagnose(self, role, provider):
        services = self.services
        with services.store.read() as records:
            for item in records.find("capability_checks"):
                scope = json.loads(item["scope_json"])
                if (
                    item["passed"]
                    and scope.get("role") == role
                    and scope.get("provider_revision_id") == provider["revision_id"]
                ):
                    return
            revision = records.one("global_control", singleton=1)["revision"]
        request = services.diagnostics.request(
            "p3-diagnose-" + role + "-" + str(int(self.now())),
            "provider_capability",
            {
                "provider_revision_id": provider["revision_id"],
                "model": "fixture-model",
                "role": role,
            },
            BudgetAmounts(
                active_ms=120000,
                model_calls=10,
                tokens=100000,
                repair_rounds=1,
                recovery_rounds=1,
            ),
            expected_revision=revision,
            now=self.now(),
            duration=120,
        )
        token = services.diagnostics.claim(request["task_id"], now=self.now())
        RoleProbe(
            services.diagnostics,
            services.adapter,
            services.budgets,
            services.queue,
            services.artifacts,
            services.roles,
            clock=self.now,
        ).run(token)
        self.log("role_probe", role=role)

    def import_repository(self):
        services = self.services
        data = {
            "owner": self.owner,
            "name": self.name,
            "target_branch": "main",
            "contribution_path": "fork" if self.fork else "direct",
            "fork_owner": "gwx541" if self.fork else None,
            "fork_name": "repowatch-p3-sandbox" if self.fork else None,
            "credential_reference": self.reference,
        }
        preflight = services.repositories.preflight(
            "p3-preflight", data, now=self.now()
        )
        self.log("preflight", identity=preflight["identity"])
        result = services.repositories.save(
            "p3-import",
            preflight["preflight_id"],
            {
                "mode": "all_bugs",
                "allowed_labels": [],
                "excluded_labels": [],
                "severity_labels": {},
                "claim_comment": False,
                "claim_template": "RepoWatch 将按已确认范围处理此 Issue。",
            },
            expected_revision=0,
            now=self.now(),
            scan_policy={"chunk_bytes": 16000, "exclusions": {}},
        )
        self.log("imported", repository=result)
        return result

    def scan_task(self):
        with self.services.store.read() as records:
            tasks = [
                dict(row)
                for row in records.find("tasks", mode="scan")
                if row["state"] not in {"ended", "cancelled"}
            ]
        if not tasks:
            raise AssertionError("no scan task")
        return max(tasks, key=lambda row: row["queued_at"])

    def configure(self, task_id, payload):
        with self.services.store.read() as records:
            revision = records.one("tasks", id=task_id)["revision"]
        self.services.tasks.operate(
            "p3-config-" + task_id + "-" + str(int(self.now() * 1000)),
            task_id,
            expected_revision=revision,
            action="update_configuration",
            payload=payload,
            now=self.now(),
        )

    def budget(self, task_id, key):
        with self.services.store.read() as records:
            ledger = records.one("budget_ledgers", owner_task_id=task_id)
        if ledger is None:
            raise AssertionError("task has no budget ledger")
        if json.loads(ledger["limits_json"]):
            self.log("budget_reused", task=task_id)
            return
        self.services.budgets.authorize_initial(
            "p3-budget-" + key,
            task_id,
            BudgetAmounts(
                active_ms=5_000_000,
                model_calls=80,
                tokens=2_000_000,
                repair_rounds=6,
                recovery_rounds=6,
            ),
            expected_revision=ledger["revision"],
            reason="controlled P3 acceptance",
            now=self.now(),
        )

    def run_until_idle(self, steps=40):
        for _ in range(steps):
            result = self.worker.run_once()
            self.log("worker", result=result)
            if result["state"] == "idle":
                return
            if result["state"] in {"reconciliation_required", "blocked"}:
                raise AssertionError("worker stopped: " + json.dumps(result))
        raise AssertionError("worker did not become idle")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--owner", default="Gwex323")
    parser.add_argument("--name", default="repowatch-p3-sandbox")
    parser.add_argument("--reference", required=True)
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--defect", choices=sorted(DEFECTS), default="mul")
    args = parser.parse_args()
    global ACTIVE
    ACTIVE = DEFECTS[args.defect]

    if os.environ.get("P3_DEBUG_HEARTBEAT"):
        install_heartbeat_debug()
    if os.environ.get("P3_DEBUG_GITHUB"):
        install_github_debug()
    if os.environ.get("P3_DEBUG_REPAIR"):
        install_repair_debug()
    if os.environ.get("P3_DEBUG_TOOL"):
        install_tool_debug()

    run = Acceptance(args.data_dir, args.owner, args.name, args.reference, args.fork)
    services = run.services
    run.bootstrap_provider()
    run.import_repository()
    scan = run.scan_task()
    run.configure(scan["id"], TASK_CONFIG)
    run.budget(scan["id"], "scan")
    services.queue.set_pause(False, now=run.now())
    run.run_until_idle()
    run.log("scan_complete")

    with services.store.read() as records:
        repository = records.one("repositories", github_id="1447899115")
        candidates = [
            dict(row)
            for row in records.find(
                "scan_candidates", batch_id=repository["scan_ready_batch_id"]
            )
            if json.loads(row["finding_json"])["path"] == ACTIVE["path"]
        ]
    if not candidates:
        raise AssertionError("no " + ACTIVE["path"] + " candidate")
    candidate = candidates[0]
    services.scans.decide_candidate(
        "p3-select",
        candidate["id"],
        "select",
        expected_revision=candidate["revision"],
        now=run.now(),
    )
    with services.store.read() as records:
        candidate_task = records.one("scan_candidates", id=candidate["id"])[
            "request_task_id"
        ]
    run.configure(candidate_task, TASK_CONFIG)
    run.budget(candidate_task, "candidate")
    run.run_until_idle()
    run.log("candidate_triage_complete")

    with services.store.read() as records:
        issue_tasks = [
            dict(row)
            for row in records.find("tasks", repository_id=repository["id"])
            if row["issue_id"] is not None
            and row["state"] not in {"ended", "cancelled"}
        ]
    if not issue_tasks:
        raise AssertionError("candidate did not create an issue task")
    issue_task = max(issue_tasks, key=lambda row: row["queued_at"])
    run.configure(issue_task["id"], TASK_CONFIG)
    run.run_until_idle()
    run.log("issue_triage_complete", task=issue_task["id"])

    run.run_until_idle(steps=60)
    with services.store.read() as records:
        task = dict(required(records, "tasks", id=issue_task["id"]))
        intent = records.one(
            "publication_intents", task_id=task["id"], kind="pull_request"
        )
        if intent:
            intent = dict(intent)
    run.log(
        "final",
        task_state=task["state"],
        wait_reason=task["wait_reason"],
        issue_id=task["issue_id"],
        publication=intent,
    )


if __name__ == "__main__":
    main()
