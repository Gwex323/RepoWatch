"""Text files are editable while source hashes and protected paths remain enforced."""

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
from repowatch.repair_workspace import Patch, RepairWorkspace, digest
from repowatch.store import Conflict


def rejected_read(number, path="ADAPTER_NOTES.md"):
    return ModelResponse(output=[ResponseFunctionToolCall(
        id=f"rejected_{number}", call_id=f"rejected_{number}", type="function_call",
        name="read_repair_source", arguments=json.dumps({"path": path, "candidate": False}),
    )], usage=Usage(requests=1), response_id=None)


@pytest.mark.parametrize("denied", [1, 2])
def test_document_reads_are_available_before_a_verified_repair(pending, denied):  # noqa: F811
    repairs, _, report, runtime, publisher = pending
    source = repairs.execution.directory(report["env_id"]) / "source"
    (source / "ADAPTER_NOTES.md").write_text("repository documentation", encoding="utf-8")
    task = confirm(pending)
    enable(repairs)

    class ReadingModel(RepairModel):
        reads = 0

        async def get_response(self, system_instructions, input, **kwargs):
            if self.reads < denied:
                self.reads += 1
                return rejected_read(self.reads)
            return await super().get_response(system_instructions, input, **kwargs)

    async def repair(context, event, *, workspace):
        return await run_repair(ReadingModel(), context, event, workspace=workspace)

    repairs.engine.repair = repair
    asyncio.run(repairs.tick())
    result = repairs.get(task["id"])
    assert result["status"] == "pr_created"
    assert len(runtime.test_calls) == 9 and len(publisher.calls) == 1
    assert not any(e["kind"] == "source_path_rejected" for e in result["events"])


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
def test_text_paths_are_available_and_unsafe_paths_remain_fatal(
    workspace, path, reason, calls, code,
):
    if path in {"notes.md", ".github/workflow.py", "setup.py"}:
        if path != "notes.md":
            original = workspace.original / path
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_text("fixture config", encoding="utf-8")
            workspace.files[path] = digest(original.read_bytes())
        observed = workspace.read(path)
        assert observed["sha256"] and path in workspace.reads
        return
    model, events = RejectedReadModel(path), []
    with pytest.raises(Exception) as error:
        asyncio.run(run_repair(model, {"finding": {}, "confirmation": {}},
                               lambda *item: events.append(item), workspace=workspace))
    assert repair_failure_code(error.value) == code
    assert model.calls == calls and workspace.read_count == calls
    assert not workspace.reads and workspace.patch is None and workspace.verification is None
    assert not workspace.candidate.exists() and not workspace.baseline.exists()
    assert "unread-private-document" not in str(model.outputs)
    assert not any(k in {"patch", "testing", "handoff"} for k, _ in events)


def test_text_patch_still_requires_the_existing_hash(workspace):
    patch = {"summary": "Update repository documentation", "edits": [
        {"path": "notes.md", "original_sha256": "", "content": "replacement"},
        {"path": "tests/test_new.py", "original_sha256": "", "content": "assert True"},
    ], "regression_paths": ["tests/test_new.py"], "regression_command": "unused"}
    with pytest.raises(Conflict, match="repair_source_changed"):
        workspace.prepare(Patch.model_validate(patch))
    patch["edits"][0]["original_sha256"] = workspace.read("notes.md")["sha256"]
    workspace.prepare(Patch.model_validate(patch))
    assert (workspace.candidate / "notes.md").read_text() == "replacement"
