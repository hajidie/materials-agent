from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import AuthenticationError, RateLimitError
from pydantic import Field

from test_agent_runtime import setup, tool_call
from test_sdk_agent_loop import ScriptedChatModel
from materialsagent.application.reliability import model_fault, retry_after, validate_resume
from materialsagent.domain.models.agent import RunBudget, now
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FaultModel(ScriptedChatModel):
    responses: list[Any]
    request_log: list[bool] = Field(default_factory=list)

    @property
    def requests(self):
        return len(self.request_log)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.request_log.append(True)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


def fault_setup(*responses, budget=None):
    run, store, tools, factory, runtime = setup(budget=budget)
    factory.chat = FaultModel(responses=list(responses))
    return run, store, tools, factory, runtime


def no_backoff(monkeypatch):
    monkeypatch.setattr("langchain.agents.middleware.model_retry.calculate_delay", lambda *args, **kwargs: 0)


def allow_resume(store):
    def resume(run_id, actor_id, submission_id, version, *, requested_version=None):
        run = store.get(run_id, actor_id)
        if run.version != version or run.status != "INTERRUPTED":
            raise AgentConflictError("stale")
        validate_resume(run, now())
        run.status, run.error_code = "PENDING", None
        run.recovery_replay = bool(run.calls or run.pending_tool_call_id)
        run.resumed_version = requested_version
        store.save(run)
        return store.get(run_id, actor_id)
    store.resume = resume


async def test_two_transient_attempts_then_success_records_and_charges_each_request(monkeypatch):
    no_backoff(monkeypatch)
    run, store, _, factory, runtime = fault_setup(TimeoutError(), TimeoutError(), AIMessage(content="完成。"))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and factory.chat.requests == 3
    assert [call.attempt_no for call in result.calls] == [1, 2, 3]
    assert len({call.logical_call_id for call in result.calls}) == 1
    assert [call.status for call in result.calls] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert result.llm_tokens == sum(call.usage.total_tokens for call in result.calls)
    assert len([m for m in result.messages if m["role"] == "ASSISTANT"]) == 1
    assert result.pending_model_retry is None


async def test_auth_and_quota_pause_without_retry():
    request = httpx.Request("POST", "https://model.invalid")
    for error in (AuthenticationError("private", response=httpx.Response(401, request=request), body={}),
                  RateLimitError("private", response=httpx.Response(429, request=request), body={"code": "insufficient_quota"})):
        run, store, _, factory, runtime = fault_setup(error, AIMessage(content="不要执行"))
        result = await runtime.advance(run.agent_run_id, "actor")
        assert result.status == "INTERRUPTED" and result.error_code == "LLM_CONFIGURATION_REQUIRED"
        assert factory.chat.requests == 1 and result.calls[0].failure_category == "CONFIGURATION"
        assert await runtime.checkpointer.aget_tuple({"configurable": {"thread_id": run.agent_run_id}})


async def test_resume_keeps_successful_tool_and_cumulative_budget(monkeypatch):
    no_backoff(monkeypatch)
    run, store, tools, factory, runtime = fault_setup(tool_call(), TimeoutError(), TimeoutError(), TimeoutError(), AIMessage(content="结果为 4。"))
    failed = await runtime.advance(run.agent_run_id, "actor")
    assert failed.status == "INTERRUPTED" and tools.executed == ["call-1"]
    assert len(failed.calls) == 4 and failed.observations[0].status == "SUCCEEDED"
    allow_resume(store)
    accepted = await runtime.resume(run.agent_run_id, "actor", failed.submission_id, failed.version)
    await asyncio.gather(*list(runtime.running_tasks.values()))
    result = store.get(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and tools.executed == ["call-1"]
    assert result.budget == failed.budget and result.llm_tokens > failed.llm_tokens
    assert result.calls[-1].attempt_no == 1 and result.calls[-1].logical_call_id != failed.calls[-1].logical_call_id
    with pytest.raises(AgentConflictError):
        await runtime.resume(run.agent_run_id, "actor", failed.submission_id, failed.version - 1)


async def test_retry_after_persists_not_before_and_rejects_early_resume():
    error = RateLimitError("private", response=httpx.Response(429, request=httpx.Request("POST", "https://model.invalid"),
                          headers={"Retry-After": "60"}), body={})
    run, store, _, factory, runtime = fault_setup(error)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "INTERRUPTED" and result.retry_not_before > now()
    assert factory.chat.requests == 1
    with pytest.raises(AgentFailure, match="LLM_RETRY_NOT_READY"):
        await runtime.resume(run.agent_run_id, "actor", result.submission_id, result.version)


async def test_budget_exhaustion_cannot_be_reset_by_resume(monkeypatch):
    no_backoff(monkeypatch)
    run, store, _, factory, runtime = fault_setup(TimeoutError(), TimeoutError(), budget=RunBudget(max_model_calls=2))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "TERMINATED" and result.error_code == "AGENT_MODEL_CALL_BUDGET_EXCEEDED"
    assert factory.chat.requests == 2
    with pytest.raises(AgentConflictError):
        await runtime.resume(run.agent_run_id, "actor", result.submission_id, result.version)


async def test_unknown_internal_model_error_does_not_retry():
    run, store, _, factory, runtime = fault_setup(RuntimeError("secret"), AIMessage(content="不应到达"))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "TERMINATED" and factory.chat.requests == 1
    assert result.error_code == "LLM_CALL_FAILED"
    assert "secret" not in str(store.process)


async def test_invalid_model_response_records_protocol_failure_without_retry():
    response = AIMessage(content="不完整", response_metadata={"finish_reason": "length"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    run, store, _, factory, runtime = fault_setup(response)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "TERMINATED" and factory.chat.requests == 1
    assert result.calls[0].status == "FAILED" and result.calls[0].failure_category == "PROTOCOL"
    assert result.calls[0].usage.total_tokens == 15 and result.calls[0].usage.source == "actual"
    assert not any(m["role"] == "ASSISTANT" for m in result.messages)


async def test_stop_cancels_whole_retry_wait(monkeypatch):
    entered = asyncio.Event()
    def backoff(*args, **kwargs):
        entered.set()
        return 60
    monkeypatch.setattr("langchain.agents.middleware.model_retry.calculate_delay", backoff)
    run, store, _, factory, runtime = fault_setup(TimeoutError(), AIMessage(content="不应到达"))
    def stop(run_id, actor_id, submission_id):
        current = store.get(run_id, actor_id)
        current.status, current.error_code = "TERMINATED", "USER_STOPPED"
        store.save(current)
        return current, True
    store.stop = stop
    advancing = asyncio.create_task(runtime.advance(run.agent_run_id, "actor"))
    await asyncio.wait_for(entered.wait(), 5)
    await runtime.stop(run.agent_run_id, "actor", run.submission_id)
    result = await asyncio.wait_for(advancing, 5)
    assert result.error_code == "USER_STOPPED" and factory.chat.requests == 1


async def test_ask_user_interrupt_never_retries(monkeypatch):
    no_backoff(monkeypatch)
    run, store, _, factory, runtime = fault_setup(tool_call("ask_user", arguments={"question": "目标性能是多少？"}))
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "WAITING_FOR_USER" and factory.chat.requests == 1


async def test_retry_after_parser_rejects_non_finite_and_parses_http_date():
    instant = now().replace(microsecond=0)
    assert retry_after({"retry-after": "nan"}, instant) is None
    from email.utils import format_datetime
    assert retry_after({"retry-after": format_datetime(instant + timedelta(seconds=30))}, instant) == instant + timedelta(seconds=30)
    assert not model_fault(ValueError()).auto_retry


async def test_partial_disconnect_closes_failed_fragment_and_drops_late_chunk(monkeypatch):
    from langchain_core.outputs import ChatGenerationChunk
    from materialsagent.application.sdk_agent_loop import SdkAgentLoop
    no_backoff(monkeypatch)
    class StreamingFault(FaultModel):
        async def _astream(self, *args, **kwargs):
            self.request_log.append(True)
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                yield ChatGenerationChunk(message=AIMessageChunk(content="失败片段",
                    usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}))
                await asyncio.sleep(0.01)
                raise response
            yield ChatGenerationChunk(message=AIMessageChunk(content=response.content, response_metadata={"finish_reason": "stop"}))
    run, store, _, factory, runtime = fault_setup()
    factory.chat = StreamingFault(responses=[TimeoutError(), AIMessage(content="有效答案。")])
    original = SdkAgentLoop.ainvoke
    async def with_late_delta(self, **kwargs):
        ids = []
        delta, failure = kwargs["on_delta"], kwargs["on_model_failure"]
        async def capture(call_id, chunk):
            ids.append(call_id)
            await delta(call_id, chunk)
        async def failed(error, response=None):
            await failure(error, response)
            if ids:
                await delta(ids[0], AIMessageChunk(content="迟到污染"))
        return await original(self, **(kwargs | {"on_delta": capture, "on_model_failure": failed}))
    monkeypatch.setattr(SdkAgentLoop, "ainvoke", with_late_delta)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and factory.chat.requests == 2
    assert result.messages[-1]["text"] == "有效答案。"
    assert result.calls[0].usage.source == "actual" and result.calls[0].usage.total_tokens == 14
    segments = store.process["segments"]
    assert any(s["text"] == "失败片段" and s["status"] == "interrupted" and s["purpose"] == "process" for s in segments)
    assert "迟到污染" not in str(segments)


async def test_logical_window_bounds_request_and_keeps_checkpoint():
    class HangingModel(FaultModel):
        async def _agenerate(self, *args, **kwargs):
            self.request_log.append(True)
            await asyncio.sleep(60)
    run, store, _, factory, runtime = fault_setup()
    factory.chat = HangingModel(responses=[])
    runtime.model_retry_window_seconds = 0.05
    result = await asyncio.wait_for(runtime.advance(run.agent_run_id, "actor"), 5)
    assert result.status == "INTERRUPTED"
    assert result.error_code == "LLM_RECOVERY_WINDOW_EXCEEDED"
    assert factory.chat.requests == 1 and result.calls[0].status == "FAILED"


async def test_unknown_tool_keeps_pending_record_and_never_writes_failed_observation():
    from materialsagent.domain.models.agent import Observation
    run, store, tools, factory, runtime = fault_setup(tool_call())
    def execute(_run, record, _timeout):
        tools.executed.append(record.tool_call_id)
        return Observation(tool_call_id=record.tool_call_id, kind="TOOL_RESULT", status="OUTCOME_UNKNOWN",
            tool_name=record.tool_name, invocation_run_id=record.invocation_run_id)
    tools.execute = execute
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "INTERRUPTED" and result.pending_execution.status == "OUTCOME_UNKNOWN"
    assert not result.observations and tools.executed == ["call-1"]
    with pytest.raises(AgentFailure, match="TOOL_OUTCOME_UNKNOWN"):
        await runtime.resume(run.agent_run_id, "actor", result.submission_id, result.version)
    assert tools.executed == ["call-1"]
