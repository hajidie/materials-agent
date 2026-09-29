from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from materialsagent.application.context_framework import ContextFramework
from materialsagent.application.native_tool_protocol import decode_tool_arguments, sdk_tools, validate_model_message
from materialsagent.domain.models.agent import AgentRun, tool_call_key
from materialsagent.domain.ports.agent import AgentFailure


def _fixture():
    run = AgentRun(conversation_id="conversation", actor_id="actor", source_message_id="message")
    run.user_message_ids = ["message"]
    run.messages = [{"message_id": "message", "role": "USER", "text": "该列单位为 MPa"}]
    catalog = [{
        "tool_name": "read_resource",
        "description": "Read a resource",
        "schema": {
            "type": "object",
            "properties": {"training_run_id": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["training_run_id", "count"],
            "additionalProperties": False,
        },
        "resource_parameters": [{
            "model_argument": "training_run",
            "execution_argument": "training_run_id",
            "expected_resource_type": "training_run",
            "provider": "ml_resource",
            "required": True,
        }],
        "execution_profile": "STANDARD",
    }]
    frame = ContextFramework().build("agent_decision", {
        "goal": run.goal,
        "tools": catalog,
        "resource_context": {"mapping": {"r1": {"resource_id": "internal-id"}}, "view": {"resources": []}},
    }, run)
    return run, catalog, frame


def test_tool_call_idempotency_key_is_run_scoped_and_bounded() -> None:
    call_id = "tool-" + "x" * 256
    first = tool_call_key("run-one", call_id)
    assert first == tool_call_key("run-one", call_id)
    assert first != tool_call_key("run-two", call_id)
    assert len(first) <= 128


def test_sdk_tool_exposes_only_safe_resource_schema() -> None:
    _, catalog, _ = _fixture()
    tools = sdk_tools(catalog)
    schema = tools[0].tool_call_schema
    assert [tool.name for tool in tools] == ["read_resource", "ask_user"]
    assert "training_run_id" not in schema["properties"]
    assert "training_run" in schema["properties"]
    assert "user_unit_assertions" in schema["properties"]
    assert "execution_profile" not in str(schema)


def test_sdk_tool_decodes_current_handle_and_verified_user_unit() -> None:
    run, _, frame = _fixture()
    decoded, assertions = decode_tool_arguments(frame, run, "read_resource", {
        "training_run": {"resource_ref": "r1"},
        "count": 1,
        "user_unit_assertions": [{
            "resource_parameter": "training_run", "column": "strength", "unit": "MPa",
            "source_message": "用户输入 1", "evidence": "单位为 MPa",
        }],
    })
    assert decoded == {"training_run_id": {"resource_id": "internal-id"}, "count": 1}
    assert assertions[0]["source_message"] == "message"


@pytest.mark.parametrize("arguments", [
    {"training_run": {"resource_ref": "r999"}, "count": "1"},
    {"training_run": {"resource_ref": "r1"}, "count": 1, "training_run_id": "internal-id"},
    {"training_run": {"resource_ref": "r1"}, "count": 1,
     "user_unit_assertions": [{"resource_parameter": "training_run", "column": "strength",
                               "unit": "MPa", "source_message": "用户输入 1", "evidence": "not said"}]},
])
def test_sdk_tool_rejects_invalid_model_arguments(arguments) -> None:
    run, _, frame = _fixture()
    with pytest.raises(AgentFailure):
        decode_tool_arguments(frame, run, "read_resource", arguments)


def test_native_model_message_distinguishes_tool_call_and_complete_answer() -> None:
    call = AIMessage(content="", tool_calls=[{"id": "call-1", "name": "read_resource", "args": {}}])
    assert validate_model_message(call, tools_allowed=True) is None
    assert validate_model_message(AIMessage(content="回答已完成。"), tools_allowed=False) == "回答已完成。"
    with pytest.raises(AgentFailure, match="TOOL_EXECUTION_DISABLED"):
        validate_model_message(call, tools_allowed=False)
    with pytest.raises(AgentFailure, match="UNKNOWN_TOOL"):
        validate_model_message(call, tools_allowed=True, allowed_names={"ask_user"})
    with pytest.raises(AgentFailure, match="PARALLEL_TOOL_CALLS_NOT_ALLOWED"):
        validate_model_message(AIMessage(content="", tool_calls=[
            {"id": "call-1", "name": "a", "args": {}},
            {"id": "call-2", "name": "b", "args": {}},
        ]), tools_allowed=True)
    with pytest.raises(AgentFailure, match="FINAL_ANSWER_TRUNCATED"):
        validate_model_message(AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
                               tools_allowed=True)
