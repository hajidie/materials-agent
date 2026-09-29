from __future__ import annotations

import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from materialsagent.application.context_framework import ContextFramework
from materialsagent.application.sdk_agent_loop import SdkAgentLoop
from materialsagent.domain.models.agent import AgentRun


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage]
    tool_settings: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-native-tool-model"

    def bind_tools(self, tools, **kwargs):
        self.tool_settings.append(kwargs)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def test_sdk_agent_continues_tool_then_accepts_plain_answer() -> None:
    run = AgentRun(conversation_id="conversation", actor_id="actor", source_message_id="message")
    run.user_message_ids = ["message"]
    run.messages = [{"message_id": "message", "role": "USER", "text": "换算"}]
    catalog = [{
        "tool_name": "convert", "description": "Convert units",
        "schema": {"type": "object", "properties": {"value": {"type": "number"}},
                   "required": ["value"], "additionalProperties": False},
        "resource_parameters": [], "execution_profile": "STANDARD",
    }]
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "convert", "args": {"value": 1}}]),
        AIMessage(content="换算完成。", response_metadata={"finish_reason": "stop"}),
    ])
    events = []

    async def context():
        return ContextFramework().build("agent_decision", {"goal": run.goal, "tools": catalog}, run)

    async def on_model(message, messages, frame):
        events.append(("model", bool(message.tool_calls)))

    async def on_tool(call, frame):
        events.append(("tool", call["id"]))
        return ToolMessage(content='{"converted": 2}', tool_call_id=call["id"])

    result = asyncio.run(SdkAgentLoop(checkpointer=InMemorySaver()).ainvoke(
        thread_id=run.agent_run_id, model=model, catalog=catalog, context=context,
        on_model=on_model, on_tool=on_tool,
    ))
    assert result["messages"][-1].content == "换算完成。"
    assert events == [("model", True), ("tool", "call-1"), ("model", False)]
    assert model.tool_settings == [{"tool_choice": None, "parallel_tool_calls": False}] * 2
