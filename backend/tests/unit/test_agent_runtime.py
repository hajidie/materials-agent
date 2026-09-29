from __future__ import annotations

import copy

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from backend.tests.agent_state import agent_run
from backend.tests.unit.test_sdk_agent_loop import ScriptedChatModel
from materialsagent.application.agent_runtime import AgentRuntime
from materialsagent.domain.models.agent import ArgumentDraft, Observation, RunBudget
from materialsagent.domain.ports.agent import AgentConflictError


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Store:
    def __init__(self, run):
        self.run = run.model_copy(deep=True)
        self.cleanup = set()

    def get(self, _run_id, _actor_id):
        return self.run.model_copy(deep=True)

    def save(self, run):
        if run.version != self.run.version:
            raise AgentConflictError("version")
        run.validate_transition(self.run)
        if run.pending_message:
            value = run.pending_message
            run.messages.append({"message_id": value["message_id"], "text": value["text"],
                                 "role": "ASSISTANT", "attachments": []})
            run.pending_message = None
        run.version += 1
        self.run = run.model_copy(deep=True)
        if run.terminal:
            self.cleanup.add(run.agent_run_id)

    def receipt(self, run_id, actor_id, record, observation):
        run = self.get(run_id, actor_id)
        record.status, record.observation_id = observation.status, observation.observation_id
        run.observations.append(observation)
        run.executions.append(record.model_copy(deep=True))
        run.pending_execution, run.draft = None, None
        self.save(run)
        return self.get(run_id, actor_id)

    def complete_checkpoint_cleanup(self, run_id):
        self.cleanup.discard(run_id)


class Tools:
    def __init__(self, store):
        self.store = store
        self.executed = []
        self.require_confirmation = False

    def catalog(self):
        return [{
            "tool_name": "convert", "description": "Convert a number",
            "schema": {"type": "object", "properties": {"value": {"type": "number"}},
                       "required": ["value"], "additionalProperties": False},
            "resource_parameters": [], "execution_profile": "STANDARD",
        }]

    def resolve(self, run, name, arguments):
        return ArgumentDraft(tool_name=name, version="1", schema_hash="a" * 64,
            arguments=arguments, normalized=arguments,
            issues={} if isinstance(arguments.get("value"), (int, float)) else {"value": "Missing"},
            resolver_authoritative=True)

    def prepare(self, run, record):
        record.invocation_run_id = "invocation-" + record.tool_call_id
        record.status = "PENDING_CONFIRMATION" if self.require_confirmation else "PENDING"
        return record

    def execute(self, run, record, timeout):
        assert self.store.run.pending_execution.dispatched
        self.executed.append(record.tool_call_id)
        return Observation(tool_call_id=record.tool_call_id, kind="TOOL_RESULT",
            status="SUCCEEDED", tool_name=record.tool_name,
            invocation_run_id=record.invocation_run_id,
            data={"value": record.arguments["value"] * 2})

    def confirm(self, run, record, approved):
        assert approved

    def repair(self, run, record):
        return None


class ModelFactory:
    def __init__(self, *responses):
        self.chat = ScriptedChatModel(responses=list(responses))

    def native_model(self, **_kwargs):
        return self.chat


def setup(*responses, budget=None, tool_execution_disabled=False):
    run = agent_run(conversation_id="conversation", actor_id="actor", source_message_id="message",
                    goal="Convert", budget=budget or RunBudget(), tool_execution_disabled=tool_execution_disabled)
    store = Store(run)
    tools = Tools(store)
    model = ModelFactory(*responses)
    runtime = AgentRuntime(store, model, tools, checkpointer=InMemorySaver())
    return run, store, tools, model, runtime


def tool_call(name="convert", *, arguments=None, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{
        "id": call_id, "name": name, "args": arguments if arguments is not None else {"value": 2},
    }])


async def test_plain_assistant_message_publishes_final_answer_without_tool() -> None:
    run, store, tools, model, runtime = setup(AIMessage(content="完成。"))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED"
    assert next(m["text"] for m in result.messages if m["message_id"] == result.final_message_id) == "完成。"
    assert not result.executions and not tools.executed
    assert len(result.calls) == 1 and result.llm_tokens > 0


async def test_native_tool_call_uses_durable_dispatch_and_observation() -> None:
    run, store, tools, model, runtime = setup(tool_call(), AIMessage(content="结果为 4。"))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED"
    assert tools.executed == ["call-1"]
    assert result.executions[0].tool_call_id == "call-1"
    assert result.observations[0].tool_call_id == "call-1"
    assert len(result.calls) == 2


async def test_native_ask_user_pauses_and_resumes_same_run() -> None:
    run, store, tools, model, runtime = setup(
        tool_call("ask_user", arguments={"question": "数值是多少？"}),
        AIMessage(content="收到数值。"),
    )
    waiting = await runtime.advance(run.agent_run_id, "actor")
    assert waiting.status == "WAITING_FOR_USER"
    assert waiting.waiting.question == "数值是多少？"
    assert not tools.executed
    replied = store.get(run.agent_run_id, "actor")
    replied.status = "PENDING"
    replied.user_message_ids.append("reply")
    replied.messages.append({"message_id": "reply", "role": "USER", "text": "3"})
    store.save(replied)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and result.agent_run_id == run.agent_run_id
    assert len(result.calls) == 2


async def test_confirmation_prepares_once_and_resumes_without_re_dispatch() -> None:
    run, store, tools, model, runtime = setup(tool_call(), AIMessage(content="完成。"))
    tools.require_confirmation = True
    waiting = await runtime.advance(run.agent_run_id, "actor")
    assert waiting.status == "WAITING_FOR_CONFIRMATION"
    assert waiting.pending_execution.invocation_run_id == "invocation-call-1"
    assert not tools.executed
    result = await runtime.advance(run.agent_run_id, "actor",
        waiting_version=waiting.waiting_version, confirmation=True)
    assert result.status == "SUCCEEDED" and tools.executed == ["call-1"]


async def test_parallel_tool_calls_are_rejected_before_dispatch() -> None:
    parallel = AIMessage(content="", tool_calls=[
        {"id": "call-1", "name": "convert", "args": {"value": 1}},
        {"id": "call-2", "name": "convert", "args": {"value": 2}},
    ])
    run, store, tools, model, runtime = setup(parallel)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "PARALLEL_TOOL_CALLS_NOT_ALLOWED"
    assert not tools.executed


async def test_regeneration_rejects_tool_call_with_empty_catalog() -> None:
    run, store, tools, model, runtime = setup(tool_call(), tool_execution_disabled=True)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "TOOL_EXECUTION_DISABLED"
    assert not tools.executed


async def test_model_call_limit_prevents_second_dispatch() -> None:
    run, store, tools, model, runtime = setup(tool_call(), AIMessage(content="完成。"),
                                             budget=RunBudget(max_model_calls=1))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "AGENT_MODEL_CALL_BUDGET_EXCEEDED"
    assert tools.executed == ["call-1"] and len(result.calls) == 1
