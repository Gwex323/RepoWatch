"""Rejected reads may correct file type, without reading documents or relaxing patch gates."""

import ast
import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall
from test_execution import service  # noqa: F401
from test_repairs import RepairModel, confirm, enable, pending  # noqa: F401

from repowatch.repair_model import repair_failure_code, run_repair
from repowatch.repair_workspace import Patch, RepairPathRejected, RepairWorkspace


def rejected_read(number, path="ADAPTER_NOTES.md"):
    return ModelResponse(output=[ResponseFunctionToolCall(
        id=f"rejected_{number}", call_id=f"rejected_{number}", type="function_call",
        name="read_repair_source", arguments=json.dumps({"path": path, "candidate": False}),
    )], usage=Usage(requests=1), response_id=None)


@pytest.mark.parametrize("denied", [1, 2])
def test_document_read_can_correct_to_source_and_finish_verified_repair(pending, denied):  # noqa: F811
    repairs, _, report, runtime, publisher = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    (source / "ADAPTER_NOTES.md").write_text("unread-private-document", encoding="utf-8")
    task = confirm(pending)
    enable(repairs)

    class CorrectingModel(RepairModel):
        rejected = 0
        inputs = None

        async def get_response(self, system_instructions, input, **kwargs):
            self.inputs = input
            if self.rejected < denied:
                self.rejected += 1
                return rejected_read(self.rejected)
            if self.calls == 0:
                outputs = [ast.literal_eval(i["output"]) for i in input
                           if isinstance(i, dict) and i.get("type") == "function_call_output"]
                assert len(outputs) == denied
                for result in outputs:
                    assert result["error"] == "repair_source_path_rejected"
                    assert result["available_paths"] == ["app.py"]
                    assert not {"text", "sha256", "path"} & result.keys()
            return await super().get_response(system_instructions, input, **kwargs)

    model = CorrectingModel()

    async def repair(context, event, *, workspace):
        return await run_repair(model, context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created"
    assert len(runtime.test_calls) == 7 and len(publisher.calls) == 1
    details = [json.loads(e["value"]) for e in result["events"]
               if e["kind"] == "source_path_rejected"]
    assert [d["corrections_remaining"] for d in details] == [1, 0][:denied]
    assert all(d["recoverable"] and d["reason"] == "unsupported_file_type" for d in details)
    assert "ADAPTER_NOTES.md" not in json.dumps(details)
    assert "unread-private-document" not in json.dumps(result) + str(model.inputs)


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "environment" / "source"
    source.mkdir(parents=True)
    (source / "app.py").write_text("print('ok')", encoding="utf-8")
    (source / "notes.md").write_text("unread-private-document", encoding="utf-8")
    root = tmp_path / "repairs"
    root.mkdir()
    execution = SimpleNamespace(root=root, directory=lambda _: source.parent)
    return RepairWorkspace(execution, "repair_" + "a" * 16, {"id": "synthetic"}, lambda: None)


class RejectedReadModel(Model):
    def __init__(self, path):
        self.path, self.calls, self.outputs = path, 0, []

    async def get_response(self, system_instructions, input, **kwargs):
        self.calls += 1
        self.outputs.extend(ast.literal_eval(i["output"]) for i in input
                            if isinstance(i, dict) and i.get("type") == "function_call_output")
        return rejected_read(self.calls, self.path)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


@pytest.mark.parametrize("path,reason,calls,code", [
    ("notes.md", "unsupported_file_type", 3, "repair_source_path_rejected"),
    ("../outside.py", "invalid_path", 1, "repair_unsafe_path"),
    ("/outside.py", "invalid_path", 1, "repair_unsafe_path"),
    (".github/workflow.py", "invalid_path", 1, "repair_unsafe_path"),
    ("data/config.py", "protected_directory", 1, "repair_unsafe_path"),
    ("setup.py", "protected_file", 1, "repair_unsafe_path"),
    ("private_token.py", "sensitive_name", 1, "repair_unsafe_path"),
])
def test_rejected_reads_are_bounded_and_other_path_guards_remain_fatal(
    workspace, path, reason, calls, code,
):
    model, events = RejectedReadModel(path), []
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                               lambda *item: events.append(item), workspace=workspace))
    assert repair_failure_code(error.value) == code
    assert model.calls == calls and workspace.read_count == calls
    assert not workspace.reads and workspace.patch is None and workspace.verification is None
    assert not workspace.candidate.exists() and not workspace.baseline.exists()
    details = [json.loads(v) for k, v in events if k == "source_path_rejected"]
    assert len(details) == calls and all(d["reason"] == reason for d in details)
    assert details[-1]["recoverable"] is False
    assert details[-1]["corrections_remaining"] == 0
    assert all(not {"path", "text", "sha256"} & d.keys() for d in details)
    assert "unread-private-document" not in str(model.outputs)
    assert not any(k in {"patch", "testing", "handoff"} for k, _ in events)


def test_read_feedback_does_not_allow_documents_in_submitted_patch(workspace):
    with pytest.raises(RepairPathRejected) as error:
        workspace.prepare(Patch.model_validate({
            "summary": "Synthetic invalid document edit", "edits": [
                {"path": "notes.md", "original_sha256": "", "content": "replacement"},
                {"path": "tests/test_new.py", "original_sha256": "", "content": "assert True"},
            ], "regression_paths": ["tests/test_new.py"], "regression_command": "unused",
        }))
    assert error.value.details["reason"] == "unsupported_file_type"
    assert not error.value.recoverable and workspace.patch is None
