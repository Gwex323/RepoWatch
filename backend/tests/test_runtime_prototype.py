import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import Field

from repowatch.domain.protocols import TaskEnvelope
from repowatch.runtime.prototype import LocalRuntimePrototype, SerialActivity


class ScriptedModel(FakeMessagesListChatModel):
    events: list[str] = Field(default_factory=list)
    received: list[list] = Field(default_factory=list)
    label: str = "model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.events.append(self.label)
        self.received.append(messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def call_message(name, arguments):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": name}])


def finish(envelope):
    return call_message(
        "StructuredResult",
        {
            "context": envelope.context.model_dump(mode="json"),
            "mode": envelope.mode,
            "status": "completed",
            "summary": "模拟模型已完成有界切片",
            "artifacts": [],
        },
    )


def readonly(envelope, run_id="review-1"):
    payload = envelope.model_dump()
    payload.update(
        role="reviewer",
        mode="review",
        tool_policy={
            "mode": "review",
            "allowed": ["read_source"],
        },
    )
    payload["context"]["run_id"] = run_id
    return TaskEnvelope.model_validate(payload)


def test_real_deepagents_loop_with_structured_result(envelope):
    reads = []

    @tool(description="读取夹具源码")
    def read_source(path: str) -> str:
        reads.append(path)
        return "fixture"

    model = ScriptedModel(
        responses=[call_message("read_source", {"path": "code.py"}), finish(envelope)]
    )
    result = LocalRuntimePrototype().run(envelope, model, [read_source])
    assert result.context == envelope.context
    assert reads == ["code.py"]
    assert len(model.received) == 2


@pytest.mark.parametrize("forbidden", ["write_file", "execute", "task", "write_source"])
def test_readonly_session_denies_ungranted_tools(envelope, forbidden):
    review = readonly(envelope)
    model = ScriptedModel(
        responses=[
            call_message(
                forbidden,
                {
                    "file_path": "/code.py",
                    "content": "overwrite",
                    "subagent_type": "general-purpose",
                },
            )
        ]
    )
    with pytest.raises(PermissionError):
        LocalRuntimePrototype().run(review, model, [])


def test_parent_child_are_synchronous_and_share_budget(envelope):
    events = []

    @tool(description="写入当前任务夹具")
    def write_source(content: str) -> str:
        events.append(content)
        return "written"

    child_payload = envelope.model_dump()
    child_payload["context"]["run_id"] = "child-1"
    child_payload.update(
        parent_run_id=envelope.context.run_id,
        mode="investigate",
        tool_policy={"mode": "investigate", "allowed": ["read_source"]},
    )
    child = TaskEnvelope.model_validate(child_payload)
    child_model = ScriptedModel(responses=[finish(child)], label="child")
    parent_model = ScriptedModel(
        responses=[
            call_message("write_source", {"content": "before"}),
            call_message("task", {"subagent_type": "bounded-child", "description": "调查夹具"}),
            call_message("write_source", {"content": "after"}),
            finish(envelope),
        ],
        label="parent",
    )
    parent_model.events = events
    child_model.events = events
    result = LocalRuntimePrototype().run(
        envelope, parent_model, [write_source], child=(child, child_model, [])
    )
    assert result.status == "completed"
    assert events == ["parent", "before", "parent", "child", "parent", "after", "parent"]


def test_review_has_fresh_context(envelope):
    runtime = LocalRuntimePrototype()
    processing = ScriptedModel(responses=[finish(envelope)])
    runtime.run(envelope, processing, [])
    review = readonly(envelope)
    reviewing = ScriptedModel(responses=[finish(review)])
    runtime.run(review, reviewing, [])
    human_messages = [
        message.content for message in reviewing.received[0] if message.type == "human"
    ]
    assert human_messages == [review.model_dump_json()]
    assert processing.received is not reviewing.received


def test_shared_activity_rejects_parallel_access_and_limits_steps():
    activity = SerialActivity(max_steps=1)
    with activity.enter(), pytest.raises(PermissionError, match="并行"):
        with activity.enter():
            pytest.fail("不应并行进入")
    with pytest.raises(PermissionError, match="步骤上限"), activity.enter():
        pytest.fail("不应超过上限")


def test_reject_wrong_child_budget(envelope):
    payload = envelope.model_dump()
    payload["context"]["run_id"] = "child-1"
    payload["parent_run_id"] = envelope.context.run_id
    payload["budget"]["owner_task_id"] = "other"
    child = TaskEnvelope.model_validate(payload)
    model = ScriptedModel(responses=[finish(envelope)])
    with pytest.raises(PermissionError, match="子任务归属"):
        LocalRuntimePrototype().run(envelope, model, [], child=(child, model, []))
