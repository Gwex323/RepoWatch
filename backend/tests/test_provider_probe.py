import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from repowatch.runtime.provider_probe import (
    FIXED,
    HANDOFF_SYSTEM_PROMPT,
    ProbeConfiguration,
    ProbeSelection,
    handoff_diagnostics,
    probe_from_file,
    run_probe,
)


@pytest.fixture
def configuration():
    return ProbeConfiguration.model_validate(
        {
            "providers": [
                {
                    "name": f"provider-{suffix}",
                    "base_url": f"https://{suffix}.example/v1",
                    "credential_env": f"TEST_PROVIDER_{suffix.upper()}",
                    "processor": {"model": "processor-model"},
                    "reviewer": {"model": "reviewer-model"},
                }
                for suffix in ("a", "b")
            ],
        }
    )


@pytest.fixture
def credentials():
    return {
        "TEST_PROVIDER_A": "secret-a-do-not-persist",
        "TEST_PROVIDER_B": "secret-b-do-not-persist",
    }


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_strings(item)


class WireFixture:
    def __init__(
        self,
        *,
        omit_usage=False,
        wrong_task=False,
        text_only=False,
        fail=False,
        loop=False,
        bad_summary=False,
        summary_transform=None,
        fail_status=401,
    ):
        self.requests = []
        self.steps = {}
        self.omit_usage = omit_usage
        self.wrong_task = wrong_task
        self.text_only = text_only
        self.fail = fail
        self.loop = loop
        self.bad_summary = bad_summary
        self.summary_transform = summary_transform
        self.fail_status = fail_status

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        assert body["store"] is False
        assert request.headers["Authorization"].startswith("Bearer secret-")
        if self.fail:
            return httpx.Response(
                self.fail_status,
                json={
                    "error": {
                        "message": "secret-a-do-not-persist secret-b-do-not-persist",
                        "type": "authentication_error",
                    }
                },
            )
        responses = request.url.path.endswith("/responses")
        assert request.url.path.endswith("/responses" if responses else "/chat/completions")
        assert body["max_output_tokens" if responses else "max_tokens"] == 2048
        assert body["tool_choice"] == "auto"
        assert "max_completion_tokens" not in body
        payloads = []
        for text in all_strings(body):
            try:
                payloads.append(json.loads(text))
            except ValueError:
                pass
        envelope = next(item for item in payloads if isinstance(item, dict) and "context" in item)
        assert responses == envelope["context"]["run_id"].startswith("responses-")
        assert "secret-a-do-not-persist" not in request.content.decode()
        assert "secret-b-do-not-persist" not in request.content.decode()
        key = (request.url.host, responses, body["model"])
        step = self.steps.get(key, 0)
        self.steps[key] = step + 1
        if step == 0 or self.loop:
            name, arguments = "read_source", {"path": "fixture.py"}
        elif step == 1:
            if envelope["role"] == "processor":
                name, arguments = "write_source", {"content": FIXED}
            else:
                name, arguments = "read_evidence", {}
        else:
            handoff_summary = next(
                item["handoff_summary"]
                for item in payloads
                if isinstance(item, dict) and "handoff_summary" in item
            )
            if self.summary_transform:
                handoff_summary = self.summary_transform(handoff_summary)
            context = envelope["context"]
            if self.wrong_task:
                context = {**context, "task_id": "wrong-task"}
            name, arguments = (
                "StructuredResult",
                {
                    "context": context,
                    "mode": envelope["mode"],
                    "status": "completed",
                    "summary": "wrong-summary" if self.bad_summary else handoff_summary,
                    "artifacts": [],
                    "unresolved": [],
                },
            )
        if responses:
            output = [
                {
                    "type": "function_call",
                    "id": f"fc_{step}",
                    "call_id": f"call_{step}",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed",
                }
            ]
            if self.text_only:
                output = [
                    {
                        "type": "message",
                        "id": f"msg_{step}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ]
            response = {
                "id": f"resp_{step}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": body["model"],
                "output": output,
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            if self.text_only:
                message = {"role": "assistant", "content": "done"}
            response = {
                "id": f"chatcmpl_{step}",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if self.text_only else "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        if self.omit_usage:
            response.pop("usage", None)
        return httpx.Response(200, json=response)


def test_both_interfaces_and_roles_execute_real_sdk_graph(configuration, credentials):
    fixture = WireFixture()
    report = run_probe(configuration, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "passed"
    assert report["execution_mode"] == "http_fixture"
    assert report["gate_passed"] is False
    assert len(report["matrix"]) == 8
    assert len(fixture.requests) == report["model_calls"] == 24
    for cell in report["matrix"]:
        assert cell["usage"]["reported_input_tokens"] == 33
        assert cell["usage"]["reported_output_tokens"] == 21
        assert cell["usage"]["unknown_usage_calls"] == 0
        assert cell["usage"]["actual_cost"] is None
        assert cell["http"]["requests_started"] == 3
        assert cell["http"]["responses_received"] == 3
        assert cell["http"]["successful_responses"] == 3
        assert cell["handoff"]["summary_exact"] is True
        assert cell["handoff"]["summary_contains_proof"] is True
    for _, payload in fixture.requests:
        assert any(HANDOFF_SYSTEM_PROMPT in value for value in all_strings(payload))
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in credentials.values())


def test_missing_credentials_preflights_entire_matrix(configuration, credentials):
    credentials.pop("TEST_PROVIDER_B")
    with patch("repowatch.runtime.provider_probe.ProbeChatModel", side_effect=AssertionError):
        report = run_probe(configuration, credentials)
    assert report["status"] == "not_passed"
    assert report["model_calls"] == 0
    assert all(cell["status"] == "not_run" for cell in report["matrix"])


@pytest.mark.parametrize(
    "status,expected_error",
    [
        (401, "authentication_or_permission"),
        (429, "rate_or_quota_limit"),
        (500, "provider_http_error"),
    ],
)
def test_http_failure_has_no_retry_or_secret_report(
    configuration, credentials, status, expected_error
):
    fixture = WireFixture(fail=True, fail_status=status)
    report = run_probe(configuration, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert len(fixture.requests) == report["model_calls"] == 8
    assert all(cell["error"] == expected_error for cell in report["matrix"])
    assert all(cell["http_status"] == status for cell in report["matrix"])
    assert all(secret not in json.dumps(report) for secret in credentials.values())
    assert all(cell["usage"]["unknown_usage_calls"] == 1 for cell in report["matrix"])


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_connection_diagnostic_is_one_small_request_without_tools(configuration, credentials, api):
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert "tools" not in payload and "tool_choice" not in payload
        assert payload["max_output_tokens" if api == "responses" else "max_tokens"] == 32
        if api == "responses":
            assert payload["store"] is False
            body = {"object": "response", "status": "completed", "output": []}
        else:
            assert "store" not in payload
            body = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        return httpx.Response(200, json=body)

    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(handler),
        selection=ProbeSelection(
            provider="provider-a", api=api, role="processor", stage="connection"
        ),
    )
    assert report["status"] == "passed"
    assert report["diagnostic_only"] is True and report["gate_passed"] is False
    assert len(requests) == report["model_calls"] == 1
    assert sum(cell["status"] == "not_run" for cell in report["matrix"]) == 7
    cell = next(cell for cell in report["matrix"] if cell["status"] == "passed")
    assert cell["http"]["status_codes"] == [200]
    assert cell["usage"]["unknown_usage_calls"] == 1


def test_tool_diagnostic_executes_tools_without_structured_handoff(configuration, credentials):
    fixture = WireFixture()
    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(fixture),
        selection=ProbeSelection(
            provider="provider-a", api="chat_completions", role="processor", stage="tools"
        ),
    )
    assert report["status"] == "passed" and report["gate_passed"] is False
    assert report["model_calls"] == 2
    assert report["matrix"][0]["tools_executed"] == ["read_source", "write_source"]
    assert all(
        tool["function"]["name"] != "StructuredResult" for tool in fixture.requests[0][1]["tools"]
    )


def test_filtered_live_probe_cannot_pass_gate_and_ignores_unselected_missing_key(
    configuration, credentials
):
    credentials.pop("TEST_PROVIDER_B")
    fixture = WireFixture()
    with patch(
        "httpx.HTTPTransport.handle_request",
        autospec=True,
        side_effect=lambda transport, request: fixture(request),
    ):
        report = run_probe(
            configuration, credentials, selection=ProbeSelection(provider="provider-a")
        )
    assert report["status"] == "passed"
    assert report["execution_mode"] == "live"
    assert report["gate_passed"] is False
    assert report["model_calls"] == 12


def test_unknown_provider_selection_has_zero_calls(configuration, credentials):
    fixture = WireFixture()
    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(fixture),
        selection=ProbeSelection(provider="not-configured"),
    )
    assert report["preflight_errors"] == [{"code": "selection_not_found"}]
    assert report["model_calls"] == 0 and fixture.requests == []
    assert report["gate_passed"] is False


@pytest.mark.parametrize("status", [400, 503])
def test_connection_failure_records_http_status_without_retry(configuration, credentials, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "private failure"}})

    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(handler),
        selection=ProbeSelection(
            provider="provider-a", api="chat_completions", role="processor", stage="connection"
        ),
    )
    cell = report["matrix"][0]
    assert report["status"] == "not_passed" and len(requests) == 1
    assert cell["error"] == "provider_http_error" and cell["http_status"] == status
    assert cell["http"]["responses_received"] == 1
    assert "private failure" not in json.dumps(report)


def test_transport_failure_distinguishes_started_request_from_received_response(
    configuration, credentials
):
    def handler(request):
        raise httpx.ConnectError("private failure secret-a-do-not-persist", request=request)

    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(handler),
        selection=ProbeSelection(provider="provider-a", api="chat_completions", role="processor"),
    )
    assert report["model_calls"] == 1
    assert report["matrix"][0]["http"]["requests_started"] == 1
    assert report["matrix"][0]["http"]["responses_received"] == 0
    assert "private failure" not in json.dumps(report)


@pytest.mark.parametrize("status", [400, 503])
def test_http_diagnostics_are_allowlisted_and_do_not_save_error_prose(
    configuration, credentials, status
):
    def handler(request):
        return httpx.Response(
            status,
            headers={"x-request-id": "request-fixture-123"},
            json={
                "error": {
                    "code": "InvalidParameter",
                    "param": "tool_choice.secret",
                    "type": "secret-type",
                    "message": (
                        "private text secret-a-do-not-persist; no available channel "
                        "(request id: fixture-request-456)"
                    ),
                },
            },
        )

    report = run_probe(configuration, credentials, transport=httpx.MockTransport(handler))
    for cell in report["matrix"]:
        assert cell["http"]["status_codes"] == [status]
        assert cell["http"]["request_ids"] == ["fixture-request-456", "request-fixture-123"]
        assert cell["http"]["provider_codes"] == ["InvalidParameter"]
        assert cell["http"]["rejected_parameters"] == ["tool_choice"]
        assert cell["http"]["error_hints"] == ["no_available_channel"]
    serialized = json.dumps(report)
    assert all(
        text not in serialized
        for text in ("private text", "secret-type", "tool_choice.secret", *credentials.values())
    )


def test_unknown_error_fields_are_not_saved(configuration, credentials):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "private-code",
                    "param": "private-param",
                    "message": "private prose",
                }
            },
        )

    report = run_probe(configuration, credentials, transport=httpx.MockTransport(handler))
    assert all(
        cell["http"]["provider_codes"] == [] and cell["http"]["rejected_parameters"] == []
        for cell in report["matrix"]
    )
    assert "private-" not in json.dumps(report) and "private prose" not in json.dumps(report)


def test_explicit_chat_profile_reaches_sdk_wire(configuration, credentials):
    payload = configuration.model_dump()
    payload["providers"][0].update(
        chat_token_parameter="max_completion_tokens", tool_choice="required"
    )
    configured = ProbeConfiguration.model_validate(payload)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"code": "InvalidParameter"}})

    report = run_probe(
        configured,
        credentials,
        transport=httpx.MockTransport(handler),
        selection=ProbeSelection(provider="provider-a", api="chat_completions", role="processor"),
    )
    assert report["model_calls"] == 1
    assert requests[0]["max_completion_tokens"] == 2048 and "max_tokens" not in requests[0]
    assert requests[0]["tool_choice"] == "required"


def test_truncated_response_is_reported_without_generated_content(configuration, credentials):
    fixture = WireFixture(text_only=True)

    def handler(request):
        response = fixture(request)
        body = response.json()
        body["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=body)

    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(handler),
        selection=ProbeSelection(provider="provider-a", api="chat_completions", role="reviewer"),
    )
    cell = next(cell for cell in report["matrix"] if cell["status"] == "failed")
    assert cell["failure_detail"] == "output_truncated"
    assert cell["http"]["output_truncated"] is True


def test_capability_failure_records_controlled_reason(configuration, credentials):
    fixture = WireFixture(bad_summary=True)
    report = run_probe(configuration, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert all(cell["error"] == "capability_rejected" for cell in report["matrix"])
    assert all(cell["failure_detail"] == "summary_proof_mismatch" for cell in report["matrix"])
    assert all(secret not in json.dumps(report) for secret in credentials.values())


@pytest.mark.parametrize(
    "transform",
    [
        lambda value: " " + value,
        lambda value: value + " completed",
        lambda value: value.removeprefix("probe:"),
        lambda value: "probe:" + "wrong-proof",
    ],
)
def test_handoff_summary_stays_exact_and_diagnostics_exclude_model_text(
    configuration,
    credentials,
    transform,
):
    generated = []

    def alter(value):
        modified = transform(value)
        generated.append((value, modified))
        return modified

    report = run_probe(
        configuration,
        credentials,
        transport=httpx.MockTransport(WireFixture(summary_transform=alter)),
        selection=ProbeSelection(provider="provider-a", api="chat_completions", role="processor"),
    )
    cell = report["matrix"][0]
    assert cell["failure_detail"] == "summary_proof_mismatch"
    assert cell["handoff"]["summary_exact"] is False
    assert report["gate_passed"] is False
    original, modified = generated[0]
    assert cell["handoff"]["summary_contains_proof"] == (original[6:] in modified)
    assert cell["handoff"]["summary_has_probe_prefix"] == modified.startswith("probe:")
    assert cell["handoff"]["summary_has_surrounding_whitespace"] == (modified != modified.strip())
    assert cell["handoff"]["summary_length"] == len(modified)
    serialized = json.dumps(report)
    assert original not in serialized and modified not in serialized


def test_missing_usage_stays_unknown(configuration, credentials):
    report = run_probe(
        configuration, credentials, transport=httpx.MockTransport(WireFixture(omit_usage=True))
    )
    assert report["status"] == "passed"
    assert all(cell["usage"]["unknown_usage_calls"] == 3 for cell in report["matrix"])
    assert all(cell["usage"]["actual_cost"] is None for cell in report["matrix"])


def test_wrong_task_handoff_is_rejected(configuration, credentials):
    report = run_probe(
        configuration, credentials, transport=httpx.MockTransport(WireFixture(wrong_task=True))
    )
    assert report["status"] == "not_passed"
    assert all(cell["error"] == "invalid_handoff" for cell in report["matrix"])
    assert all(cell["failure_detail"] == "invalid_handoff" for cell in report["matrix"])
    assert all(
        cell["handoff_diagnostics"]["kind"] == "context_mismatch" for cell in report["matrix"]
    )


def test_plain_text_cannot_pass_and_loops_are_bounded(configuration, credentials):
    fixture = WireFixture(text_only=True)
    report = run_probe(configuration, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert len(fixture.requests) == 8
    assert all(cell["error"] == "invalid_handoff" for cell in report["matrix"])
    diagnostics = [cell["handoff_diagnostics"] for cell in report["matrix"]]
    assert all(item["kind"] == "missing_structured_result" for item in diagnostics)
    assert all(item["final_tool_names"] == [] for item in diagnostics)
    assert all(item["final_text_length"] > 0 for item in diagnostics)


def test_handoff_diagnostics_omit_validation_input_values():
    class Sample(BaseModel):
        count: int

    with pytest.raises(ValidationError) as captured:
        Sample.model_validate({"count": "model-produced-secret-text"})

    diagnostics = handoff_diagnostics(captured.value)
    assert diagnostics == {
        "kind": "validation",
        "final_tool_names": [],
        "final_text_length": 0,
        "field_count": 1,
        "fields": ["count:int_parsing"],
    }
    assert "model-produced-secret-text" not in json.dumps(diagnostics)


def test_repeated_tools_stop_at_the_call_limit(configuration, credentials):
    fixture = WireFixture(loop=True)
    report = run_probe(configuration, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert len(fixture.requests) == 32
    assert all(cell["error"] == "budget_exhausted" for cell in report["matrix"])


def test_global_call_limit_survives_cell_changes(configuration, credentials):
    payload = configuration.model_dump()
    payload["limits"]["max_calls"] = 1
    limited = ProbeConfiguration.model_validate(payload)
    fixture = WireFixture()
    report = run_probe(limited, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert len(fixture.requests) == 1
    assert report["model_calls"] == 1


def test_deadline_prevents_new_calls(configuration, credentials):
    payload = configuration.model_dump()
    payload["limits"]["total_seconds"] = 1e-12
    limited = ProbeConfiguration.model_validate(payload)
    fixture = WireFixture()
    report = run_probe(limited, credentials, transport=httpx.MockTransport(fixture))
    assert report["status"] == "not_passed"
    assert report["model_calls"] == 0
    assert fixture.requests == []
    assert all(cell["error"] == "budget_exhausted" for cell in report["matrix"])


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/v1#secret",
        "file:///secret",
        "https:///missing-host",
    ],
)
def test_reject_credential_bearing_or_invalid_addresses(configuration, url):
    payload = configuration.model_dump()
    payload["providers"][0]["base_url"] = url
    with pytest.raises(ValidationError):
        ProbeConfiguration.model_validate(payload)


def test_reject_plaintext_keys_and_duplicate_providers(configuration):
    payload = configuration.model_dump()
    payload["providers"][0]["api_key"] = "secret"
    with pytest.raises(ValidationError):
        ProbeConfiguration.model_validate(payload)
    payload["providers"][0].pop("api_key")
    payload["providers"][1]["name"] = payload["providers"][0]["name"]
    with pytest.raises(ValidationError):
        ProbeConfiguration.model_validate(payload)


def test_example_is_valid_and_does_not_call_without_keys():
    path = Path(__file__).resolve().parents[2] / "experiments/providers.example.json"
    report = probe_from_file(path, {})
    assert report["status"] == "not_passed"
    assert report["model_calls"] == 0
    assert len(report["matrix"]) == 8


def test_invalid_config_never_leaks_validation_inputs(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text('{"api_key": "a-secret-that-must-not-appear"}', encoding="utf-8")
    report = probe_from_file(path, {})
    assert report["status"] == "not_passed"
    assert "a-secret-that-must-not-appear" not in json.dumps(report)
    assert report["model_calls"] == 0


def test_missing_config_emits_eight_unexecuted_cells(tmp_path):
    report = probe_from_file(tmp_path / "missing.json", {})
    assert report["gate_passed"] is False
    assert report["model_calls"] == 0
    assert len(report["matrix"]) == 8
    assert all(cell["status"] == "not_run" for cell in report["matrix"])


def test_redirect_does_not_forward_credentials(configuration, credentials):
    requested_hosts = []

    def redirect(request):
        requested_hosts.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://unexpected.example/v1/responses"})

    report = run_probe(configuration, credentials, transport=httpx.MockTransport(redirect))
    assert report["status"] == "not_passed"
    assert len(requested_hosts) == 8
    assert set(requested_hosts) == {"a.example", "b.example"}
