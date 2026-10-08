"""Durable business boundary around the SDK model and tool continuation loop."""
from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Any, Callable

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from materialsagent.domain.models.agent import (
    AgentRun, ExecutionRecord, ModelCall, Observation, TokenUsage, canonical,
    fingerprint, identifier, now,
)
from materialsagent.domain.models.ml_resource_context import model_draft, model_observation
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure, AgentStore, AgentToolGateway

from .context_framework import ContextFramework, internal_values, protect_text
from .native_tool_protocol import decode_tool_arguments, validate_model_message
from .sdk_agent_loop import NATIVE_AGENT_INSTRUCTIONS, SdkAgentLoop
from .unit_resolution import project_units
from .agent_process import AgentProcess, PublicText


def trim_public_context(frame, messages, maximum: int) -> int:
    """Fit the model-visible projection without changing authoritative Run state."""
    from materialsagent.infrastructure.llm.agent_model import _count

    def estimate() -> int:
        return _count([NATIVE_AGENT_INSTRUCTIONS, frame.payload,
                       [message.model_dump(mode="json") for message in messages]])

    size = estimate()
    resources = frame.payload.get("resource_context")
    if isinstance(resources, dict) and isinstance(resources.get("resources"), list):
        selected = resources["resources"] = list(resources["resources"])
        while size > maximum and selected:
            selected.pop()
            resources["complete"] = False
            resources["omitted_count"] = int(resources.get("omitted_count", 0)) + 1
            size = estimate()
        visible = {item.get("resource_ref") for item in selected if isinstance(item, dict)}
        frame.resource_map = {key: value for key, value in frame.resource_map.items() if key in visible}
    history = frame.payload.get("conversation_context")
    if isinstance(history, list):
        while size > maximum and len(history) > 1:
            history.pop(0)
            size = estimate()
    return size


class AgentRuntime:
    def __init__(self, store: AgentStore, model: Any, tools: AgentToolGateway,
                 *, checkpointer: Any = None, checkpoint_cleanup: Any = None,
                 process_id: str | None = None, monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] = now):
        self.store, self.model, self.tools = store, model, tools
        self.checkpointer = checkpointer
        self.checkpoint_cleanup = checkpoint_cleanup
        self.process_id = process_id or identifier()
        self.monotonic, self.clock = monotonic, clock
        self.context_framework = ContextFramework()
        self.model_tasks: dict[tuple[str, str | None], asyncio.Task] = {}
        self.stop_events: dict[tuple[str, str | None], asyncio.Event] = {}
        self.receipt_tasks: set[asyncio.Task] = set()
        self.running_tasks: dict[tuple[str, str | None], asyncio.Task] = {}
        self.scheduled_again: set[str] = set()
        self.process = AgentProcess(store)

    def schedule(self, run: AgentRun) -> None:
        """Accepted work is owned by the application, never an HTTP subscriber."""
        if run.status != "PENDING" or (run.accepted_submission_id and run.accepted_submission_id != run.submission_id):
            return
        key = (run.agent_run_id, run.submission_id)
        if any(k[0] == run.agent_run_id and not task.done() for k, task in self.running_tasks.items()):
            self.scheduled_again.add(run.agent_run_id)
            return
        async def work():
            try:
                current = run
                while True:
                    self.scheduled_again.discard(run.agent_run_id)
                    if current.status == "PENDING":
                        await self.advance(current.agent_run_id, current.actor_id, submission_id=current.submission_id)
                    # A confirmation/reply may have been accepted during closeout.
                    current = await asyncio.to_thread(self.store.get, run.agent_run_id, run.actor_id)
                    if current.status != "PENDING" and run.agent_run_id not in self.scheduled_again:
                        break
                    if run.agent_run_id in self.scheduled_again:
                        current = await asyncio.to_thread(self.store.get, run.agent_run_id, run.actor_id)
            except (Exception, asyncio.CancelledError):
                # Includes failures before advance has claimed the run.
                current = await asyncio.to_thread(self.store.get, run.agent_run_id, run.actor_id)
                if current.status in {"PENDING", "RUNNING"} and current.submission_id == run.submission_id:
                    current.status, current.error_code = "INTERRUPTED", "PROCESS_INTERRUPTED"
                    try:
                        await asyncio.to_thread(self.store.save, current)
                        await self.process.finish(current.agent_run_id, current.actor_id, current.status)
                    except AgentConflictError:
                        pass
                raise
            finally:
                self.scheduled_again.discard(run.agent_run_id)
                self.process.changed(run.agent_run_id)
                self.process.evict()
        task = asyncio.create_task(work(), name=f"agent:{run.agent_run_id}")
        self.running_tasks[key] = task
        def done(finished):
            if self.running_tasks.get(key) is finished:
                self.running_tasks.pop(key, None)
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    import logging
                    logging.getLogger(__name__).error("agent_background_failed", exc_info=error)
        task.add_done_callback(done)

    async def resume(self, run_id: str, actor_id: str, submission_id: str, version: int):
        requested_version = version
        run = await asyncio.to_thread(self.store.get, run_id, actor_id)
        if run.submission_id != submission_id:
            raise AgentConflictError("Submission is stale.")
        if run.resumed_version == version:
            self.schedule(run)
            return run
        if run.status != "INTERRUPTED" or run.version != version:
            raise AgentConflictError("Recovery version is stale.")
        checkpoint = await self.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
        if checkpoint is None and (run.calls or run.pending_tool_call_id):
            raise AgentFailure("CHECKPOINT_MISSING")
        record = run.pending_execution
        if record is not None and record.dispatched:
            found = next((o for o in run.observations if o.invocation_run_id == record.invocation_run_id), None)
            if found is None:
                found = await asyncio.to_thread(self.tools.repair, run, record)
                if found is None:
                    raise AgentFailure("TOOL_OUTCOME_UNKNOWN")
                run = await asyncio.to_thread(self.store.receipt, run_id, actor_id, record, found)
                version = run.version
        run = await asyncio.to_thread(self.store.resume, run_id, actor_id, submission_id, version, requested_version=requested_version)
        self.schedule(run)
        return run

    async def close(self) -> None:
        jobs = list(self.running_tasks.values())
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        for task in list(self.model_tasks.values()):
            task.cancel()
        if self.model_tasks:
            await asyncio.gather(*self.model_tasks.values(), return_exceptions=True)
        if self.receipt_tasks:
            await asyncio.wait(list(self.receipt_tasks), timeout=30)

    async def cleanup_checkpoints(self, *, limit: int = 100) -> None:
        if self.checkpoint_cleanup is None:
            return
        ids = await asyncio.to_thread(self.store.checkpoint_cleanup_ids, limit=limit)
        for run_id in ids:
            try:
                await self.checkpoint_cleanup.delete(run_id)
                await asyncio.to_thread(self.store.complete_checkpoint_cleanup, run_id)
            except Exception:
                await asyncio.to_thread(self.store.checkpoint_cleanup_failed, run_id)

    async def recover(self) -> None:
        candidates = await asyncio.to_thread(
            self.store.recover_interrupted, self.process_id, repair=self.tools.repair,
        )
        for run_id, actor_id, status in candidates:
            checkpoint = await self.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
            run = await asyncio.to_thread(self.store.get, run_id, actor_id)
            if run.terminal:
                await self.process.tools(run)
                await self.process.finish(run_id, actor_id, run.status, final_message_id=run.final_message_id)
                continue
            if checkpoint is None and (run.calls or run.pending_execution):
                run.status = "TERMINATED"
                run.error_code = "CHECKPOINT_MISSING"
                await asyncio.to_thread(self.store.save, run)
                await self.process.finish(run_id, actor_id, run.status)
                continue
            if status in {"WAITING_FOR_USER", "WAITING_FOR_CONFIRMATION"}:
                await self.process.tools(run)
                await self.process.finish(run_id, actor_id, status)
                continue
            await self.process.tools(run)
            await self.process.finish(run_id, actor_id, run.status)

    async def stop(self, run_id: str, actor_id: str, submission_id: str):
        run, stopped = await asyncio.to_thread(self.store.stop, run_id, actor_id, submission_id)
        key = (run_id, submission_id)
        event = self.stop_events.get(key)
        if stopped and event is not None:
            event.set()
        task = self.model_tasks.get(key)
        if stopped and task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass  # The persisted stop owns the result.
        await self.process.finish(run_id, actor_id, run.status, final_message_id=run.final_message_id)
        return run, stopped

    async def advance(self, run_id: str, actor_id: str, *, submission_id: str | None = None,
                      waiting_version: int | None = None, confirmation: bool | None = None,
                      reconcile_waiting: bool = False) -> AgentRun:
        if self.checkpointer is None:
            raise AgentFailure("AGENT_CHECKPOINT_UNAVAILABLE")
        run = await asyncio.to_thread(self.store.get, run_id, actor_id)
        if submission_id is not None and run.submission_id != submission_id:
            raise AgentConflictError("Submission is stale.")
        if run.terminal or run.status in {"RUNNING", "INTERRUPTED"} or (run.status == "WAITING_FOR_USER" and not reconcile_waiting):
            return run
        if run.status == "WAITING_FOR_CONFIRMATION" and not reconcile_waiting and (
            waiting_version != run.waiting_version or confirmation is None
        ):
            raise AgentConflictError("Confirmation version is stale.")
        resumed_confirmation = run.status == "WAITING_FOR_CONFIRMATION" and not reconcile_waiting
        if resumed_confirmation:
            run.confirmation_response = {
                "tool_call_id": run.pending_execution.tool_call_id,
                "waiting_version": waiting_version,
                "approved": confirmation,
            }
        elif run.status == "PENDING" and run.confirmation_response is not None:
            record = run.pending_execution
            response = run.confirmation_response
            if (record is None or record.confirmed or
                    response.get("tool_call_id") != record.tool_call_id or
                    response.get("waiting_version") != run.waiting_version or
                    type(response.get("approved")) is not bool):
                raise AgentFailure("CONFIRMATION_SOURCE_MISMATCH")
            if ((waiting_version is not None and waiting_version != response["waiting_version"])
                    or (confirmation is not None and confirmation != response["approved"])):
                raise AgentConflictError("Confirmation decision is already recorded.")
            resumed_confirmation = True
        resumed_question = run.status == "PENDING" and run.question_message_id is not None
        replay_checkpoint = (run.recovery_replay or reconcile_waiting) and not (resumed_confirmation or resumed_question)
        resume = None
        if resumed_confirmation:
            resume = {"kind": "confirmation", **run.confirmation_response}
        elif resumed_question:
            resume = {"kind": "respond", "message": run.user_inputs[-1], "waiting_version": run.waiting_version}

        key = (run_id, run.submission_id)
        stopped_event = self.stop_events.setdefault(key, asyncio.Event())
        run.status, run.claim, run.process_id = "RUNNING", identifier(), self.process_id
        run.recovery_replay = False
        try:
            await asyncio.to_thread(self.store.save, run)
        except AgentConflictError:
            return await asyncio.to_thread(self.store.get, run_id, actor_id)

        last = self.monotonic()
        current_call: ModelCall | None = None
        request_limit = 0
        request_estimate = 0
        resources = getattr(self.tools, "resource_context", None)
        streams: dict[str, dict] = {}

        async def tool_progress(source=None):
            await self.process.tools(source or run)

        async def publish_text(call_id, kind, *, status="streaming", purpose="process", text=None):
            state = streams[call_id]
            guard = state[kind]
            await self.process.upsert(run_id, actor_id, {
                "segment_id": f"{call_id}:{kind}", "kind": kind, "purpose": purpose,
                "status": status, "text": guard.text if text is None else text,
            }, flush=status != "streaming")

        async def on_delta(call_id, message):
            state = streams.get(call_id)
            if state is None or state["closed"] or stopped_event.is_set():
                return
            reasoning = message.additional_kwargs.get("reasoning_content", "")
            text = message.content if isinstance(message.content, str) else "".join(
                b.get("text", "") for b in message.content if isinstance(b, dict) and b.get("type") == "text")
            for kind, value in (("reasoning", reasoning), ("text", text)):
                if not isinstance(value, str) or not value:
                    continue
                if len(state[kind].text) + len(state[kind].pending) + len(value) > 65536:
                    raise AgentFailure("MODEL_PROCESS_TOO_LARGE")
                if state[kind].push(value):
                    await publish_text(call_id, kind, purpose="pending" if kind == "text" else "process")
            for chunk in message.tool_call_chunks:
                name = chunk.get("name")
                if name and name != "ask_user" and name in {t["tool_name"] for t in self.tools.catalog()}:
                    await self.process.upsert(run_id, actor_id, {
                        "segment_id": f"{call_id}:preparing", "kind": "activity", "purpose": "process",
                        "status": "streaming", "text": "", "tool_name": name, "tool_status": "PREPARING",
                    })

        async def persist() -> None:
            nonlocal last
            instant = self.monotonic()
            run.active_seconds += max(0, instant - last)
            last = instant
            run.updated_at = now()
            await asyncio.to_thread(self.store.save, run)
            await tool_progress()
            self.process.changed(run_id)

        def remaining_time() -> float:
            return run.budget.max_active_seconds - run.active_seconds - max(0, self.monotonic() - last)

        def check_budget(*, new_model: bool = False, new_tool: bool = False) -> None:
            if remaining_time() <= 0:
                raise AgentFailure("AGENT_ACTIVE_TIME_EXCEEDED")
            if run.llm_tokens >= run.budget.max_llm_tokens:
                raise AgentFailure("LLM_TOKEN_BUDGET_EXCEEDED")
            if new_model and len(run.calls) >= run.budget.max_model_calls:
                raise AgentFailure("AGENT_MODEL_CALL_BUDGET_EXCEEDED")
            if new_tool and run.tool_executions >= run.budget.max_tool_executions:
                raise AgentFailure("TOOL_EXECUTION_BUDGET_EXCEEDED")
            if stopped_event.is_set():
                raise AgentConflictError("Submission was stopped.")

        async def context():
            payload = self._context(run)
            if resources is not None and not run.tool_execution_disabled:
                payload["resource_context"] = await asyncio.to_thread(resources.context, run)
            profile = "answer_regeneration" if run.tool_execution_disabled else "agent_decision"
            return self.context_framework.build(profile, payload, run)

        async def before_model(messages, frame):
            nonlocal current_call, request_limit, request_estimate
            check_budget(new_model=True)
            config = getattr(self.model, "configurations", {}).get("agent_decision")
            max_output = (config.max_tokens or 1024) if config is not None else 1024
            ceiling = min(run.budget.max_llm_tokens - run.llm_tokens,
                          (config.context_window_tokens - config.safety_margin_tokens) if config else 1_000_000,
                          (config.prompt_limit_tokens - config.safety_margin_tokens) if config else 1_000_000)
            minimum = 512 if frame.payload.get("resource_context") else 64
            request_estimate = trim_public_context(frame, messages, ceiling - minimum)
            request_limit = min(max_output, ceiling - request_estimate)
            if request_limit < minimum:
                raise AgentFailure("LLM_TOKEN_BUDGET_EXCEEDED" if ceiling == run.budget.max_llm_tokens - run.llm_tokens
                                   else "CONTEXT_BUDGET_EXCEEDED")
            current_call = ModelCall(role="agent_decision", prompt_digest=fingerprint(
                [frame.payload, [message.model_dump(mode="json") for message in messages]]),
                output_limit=request_limit, input_reserved=request_estimate)
            run.calls.append(current_call)
            private = set(frame.private) | internal_values(run.model_dump(mode="json"))
            streams[current_call.call_id] = {"reasoning": PublicText(private), "text": PublicText(private), "closed": False}
            await persist()  # Reserve before the external model call.
            check_budget()
            selected = self.model.native_model(timeout=remaining_time(), output_limit=request_limit, streaming=True)
            return selected.model_copy(update={"metadata": {**(selected.metadata or {}), "agent_call_id": current_call.call_id}})

        async def model_failure():
            if current_call is None or current_call.status != "RUNNING":
                return
            current_call.status, current_call.error_code = "FAILED", "LLM_CALL_FAILED"
            current_call.usage = TokenUsage(input_tokens=request_estimate, output_tokens=request_limit,
                total_tokens=request_estimate + request_limit, source="estimated",
                estimator_version="cl100k-x2-or-utf8-framing-v1")
            run.llm_tokens += current_call.usage.total_tokens
            try:
                await persist()
            except AgentConflictError:
                pass

        async def on_model(message: AIMessage, _messages, frame):
            if current_call is None:
                raise AgentFailure("LLM_CALL_SOURCE_MISMATCH")
            state = streams[current_call.call_id]
            state["closed"] = True
            finish_reason = message.response_metadata.get("finish_reason") or message.response_metadata.get("stop_reason")
            content_status = "interrupted" if finish_reason in {"length", "max_tokens", "content_filter"} else "complete"
            for kind, raw in (("reasoning", message.additional_kwargs.get("reasoning_content", "")),
                              ("text", message.content)):
                if not isinstance(raw, str):
                    continue
                safe = protect_text(raw, state[kind].private)
                if safe or state[kind].text:
                    await publish_text(current_call.call_id, kind, status=content_status, text=safe,
                        purpose="pending" if kind == "text" and not message.tool_calls else "process")
            if message.tool_calls:
                name = message.tool_calls[0]["name"]
                if name != "ask_user" and name in {t["tool_name"] for t in self.tools.catalog()}:
                    await self.process.upsert(run_id, actor_id, {
                        "segment_id": f"{current_call.call_id}:preparing", "kind": "activity", "purpose": "process",
                        "status": "complete", "text": "", "tool_name": name, "tool_status": "PREPARED",
                    }, flush=True)
            from materialsagent.domain.ports.agent import PreparedAgentCall
            from materialsagent.infrastructure.llm.agent_model import normalize_usage
            request = PreparedAgentCall("agent_decision", [], request_limit, request_estimate, remaining_time())
            current_call.usage = normalize_usage(message, request)
            current_call.status = "SUCCEEDED"
            run.llm_tokens += current_call.usage.total_tokens
            if (replay_checkpoint and run.pending_tool_call_id is not None
                    and run.pending_execution is None and run.question_message_id is None
                    and not any(record.tool_call_id == run.pending_tool_call_id for record in run.executions)
                    and not any(observation.tool_call_id == run.pending_tool_call_id for observation in run.observations)
                    and run.pending_tool_call_id not in run.answered_questions):
                # The business marker committed, but the SDK model checkpoint did
                # not. This is a fresh model response to that unfinished turn.
                run.pending_tool_call_id = None
                run.pending_resource_map = {}
            if message.tool_calls:
                call_id = message.tool_calls[0]["id"]
                if (run.pending_tool_call_id is not None or call_id in run.answered_questions
                        or any(record.tool_call_id == call_id for record in run.executions)
                        or any(observation.tool_call_id == call_id for observation in run.observations)):
                    raise AgentFailure("DUPLICATE_TOOL_CALL_ID")
                run.pending_tool_call_id = call_id
                run.pending_resource_map = frame.resource_map
            await persist()
            check_budget()

        async def observation_message(call_id: str, observation: Observation) -> ToolMessage:
            if observation.kind != "TOOL_RESULT":
                raise AgentFailure("OBSERVATION_INCONSISTENT")
            if observation.status != "SUCCEEDED":
                code = (observation.error or {}).get("code")
                raise AgentFailure("MCP_OUTCOME_UNKNOWN" if code == "MCP_OUTCOME_UNKNOWN"
                                   else "TOOL_EXECUTION_FAILED")
            return ToolMessage(content=canonical(model_observation(observation)), tool_call_id=call_id)

        async def execute(record: ExecutionRecord) -> ToolMessage:
            nonlocal run
            check_budget(new_tool=not record.dispatched)
            if record.dispatched:
                # A checkpoint replay may revisit the tool node. Never dispatch a
                # committed call twice, even if the external outcome is unknown.
                found = next((o for o in run.observations if o.invocation_run_id == record.invocation_run_id), None)
                if found is None:
                    found = await asyncio.to_thread(self.tools.repair, run, record)
                    if found is None:
                        raise AgentFailure("TOOL_OUTCOME_UNKNOWN")
                    run = await asyncio.to_thread(self.store.receipt, run_id, actor_id, record, found)
                if run.pending_tool_call_id == record.tool_call_id:
                    run.pending_tool_call_id = None
                    run.pending_resource_map = {}
                    await persist()
                return await observation_message(record.tool_call_id, found)

            record.dispatched, record.status = True, "RUNNING"
            run.tool_executions += 1
            await persist()  # No external call precedes this dispatch fence.
            profile = next((item.get("execution_profile") for item in self.tools.catalog()
                            if item["tool_name"] == record.tool_name), None)
            timeout = run.budget.managed_timeout_seconds if profile == "MANAGED" else run.budget.standard_timeout_seconds

            async def complete():
                try:
                    observation = await asyncio.to_thread(self.tools.execute, run, record, min(timeout, remaining_time()))
                except Exception:
                    observation = await asyncio.to_thread(self.tools.repair, run, record)
                    if observation is None:
                        updated = await asyncio.to_thread(self.store.fail_execution, run_id, actor_id, record)
                        await self.process.tools(updated)
                        return updated
                if observation.invocation_run_id != record.invocation_run_id:
                    raise AgentFailure("OBSERVATION_SOURCE_MISMATCH")
                updated = await asyncio.to_thread(self.store.receipt, run_id, actor_id, record, observation)
                await self.process.tools(updated)
                return updated

            task = asyncio.create_task(complete())
            self.receipt_tasks.add(task)
            task.add_done_callback(lambda finished: (self.receipt_tasks.discard(finished),
                finished.exception() if not finished.cancelled() else None))
            run = await asyncio.shield(task)
            if run.status == "TERMINATED":
                raise AgentConflictError("Run stopped after tool dispatch.")
            stored = next((o for o in run.observations if o.invocation_run_id == record.invocation_run_id), None)
            if stored is None:
                raise AgentFailure("OBSERVATION_INCONSISTENT")
            run.pending_tool_call_id = None
            run.pending_resource_map = {}
            await persist()
            return await observation_message(record.tool_call_id, stored)

        async def on_tool(call, frame):
            nonlocal run
            call_id, name, arguments = call.get("id"), call.get("name"), call.get("args")
            if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                raise AgentFailure("INVALID_TOOL_CALL")
            check_budget()
            if run.tool_execution_disabled:
                raise AgentFailure("TOOL_EXECUTION_DISABLED")
            completed = next((record for record in run.executions if record.tool_call_id == call_id), None)
            if completed is not None:
                if completed.tool_name != name:
                    raise AgentFailure("TOOL_CALL_SOURCE_MISMATCH")
                observation = next((o for o in run.observations if o.observation_id == completed.observation_id), None)
                if observation is None:
                    raise AgentFailure("OBSERVATION_INCONSISTENT")
                if run.pending_tool_call_id == call_id:
                    run.pending_tool_call_id = None
                    run.pending_resource_map = {}
                    await persist()
                return await observation_message(call_id, observation)
            if name == "ask_user" and call_id in run.answered_questions:
                reply_id = run.answered_questions[call_id]
                reply = next((item for item in run.user_messages if item["message_id"] == reply_id), None)
                if reply is None:
                    raise AgentFailure("USER_REPLY_INVALID")
                return ToolMessage(content=reply["text"], tool_call_id=call_id)
            if frame is None:
                frame = await context()
            if run.pending_tool_call_id != call_id:
                raise AgentFailure("TOOL_CALL_SOURCE_MISMATCH")
            if run.pending_resource_map:
                frame.resource_map = run.pending_resource_map
            if name == "ask_user":
                question = arguments.get("question")
                if not isinstance(question, str) or not question.strip() or len(question) > 2048:
                    raise AgentFailure("QUESTION_INVALID")
                if protect_text(question, frame.private) != question:
                    raise AgentFailure("MODEL_PRIVATE_VALUE_REJECTED")
                if run.question_message_id is None:
                    run.question_message_id = identifier()
                    run.waiting_version += 1
                    run.pending_message = {"message_id": run.question_message_id, "phase": "question",
                                           "text": question.strip(), "sources": []}
                    run.status = "WAITING_FOR_USER"
                    await persist()
                decision = interrupt({"kind": "ask_user", "question": question.strip(),
                                      "waiting_version": run.waiting_version, "tool_call_id": call_id})
                if (not isinstance(decision, dict) or decision.get("kind") != "respond"
                        or decision.get("waiting_version") != run.waiting_version
                        or not isinstance(decision.get("message"), str)):
                    raise AgentFailure("USER_REPLY_INVALID")
                reply_id = run.user_message_ids[-1]
                reply = next((item for item in run.user_messages if item["message_id"] == reply_id), None)
                if reply is None or reply["text"] != decision["message"]:
                    raise AgentFailure("USER_REPLY_INVALID")
                run.answered_questions[call_id] = reply_id
                run.question_message_id = None
                run.pending_tool_call_id = None
                run.pending_resource_map = {}
                await persist()
                return ToolMessage(content=decision["message"], tool_call_id=call_id)

            record = run.pending_execution
            if record is not None and record.tool_call_id != call_id:
                raise AgentFailure("TOOL_CALL_SOURCE_MISMATCH")
            if record is None:
                decoded, assertions = decode_tool_arguments(frame, run, name, arguments)
                run.current_unit_assertions = assertions
                run.draft = await asyncio.to_thread(self.tools.resolve, run, name, decoded)
                if run.draft.issues:
                    observation = Observation(tool_call_id=call_id, kind="ARGUMENT_RESOLUTION",
                        status="NEEDS_INPUT", tool_name=name, task_id=run.draft.task_id,
                        data={"normalized": run.draft.normalized, "issues": run.draft.issues})
                    run.observations.append(observation)
                    run.pending_tool_call_id = None
                    run.pending_resource_map = {}
                    await persist()
                    return ToolMessage(content=canonical({"status": "NEEDS_INPUT", "draft": model_draft(run.draft)}),
                                       tool_call_id=call_id)
                signature = fingerprint([run.draft.tool_name, run.draft.version,
                                         run.draft.schema_hash, run.draft.normalized])
                if any(e.execution_fingerprint == signature and e.status == "SUCCEEDED" for e in run.executions):
                    raise AgentFailure("DUPLICATE_TOOL_CALL")
                check_budget(new_tool=True)
                record = ExecutionRecord(tool_call_id=call_id, tool_name=run.draft.tool_name,
                    version=run.draft.version, schema_hash=run.draft.schema_hash,
                    arguments=run.draft.normalized, execution_fingerprint=signature,
                    resource_bindings=run.draft.resource_bindings, unit_annotations=run.draft.unit_annotations)
                if run.retry_execution:
                    if signature != run.retry_execution.execution_fingerprint:
                        raise AgentFailure("TOOL_RETRY_ARGUMENT_MISMATCH")
                    record.retry_of_invocation_run_id = run.retry_execution.invocation_run_id
                run.pending_execution = record
                await persist()

            if record.invocation_run_id is None:
                # Preparation uses the immutable tool-call key. A crash may
                # occur before or after the Invocation commits, but before its
                # ID is copied into the AgentRun.
                record = await asyncio.to_thread(self.tools.prepare, run, record)
                run.pending_execution = record
                await persist()

            if record.status == "PENDING_CONFIRMATION" and not record.confirmed:
                signature = fingerprint([record.tool_name, record.version, record.schema_hash, record.arguments])
                expected = fingerprint([record.invocation_run_id, signature])
                if signature != record.execution_fingerprint:
                    raise AgentFailure("CONFIRMATION_INVALIDATED")
                if record.confirmation_version is None:
                    record.confirmation_version = expected
                    run.waiting_version += 1
                    run.status = "WAITING_FOR_CONFIRMATION"
                    await persist()
                if record.confirmation_version != expected:
                    raise AgentFailure("CONFIRMATION_INVALIDATED")
                decision = interrupt({"kind": "confirmation", "tool_call_id": call_id,
                    "waiting_version": run.waiting_version, "confirmation_version": expected})
                if (not isinstance(decision, dict) or decision.get("kind") != "confirmation"
                        or decision.get("waiting_version") != run.waiting_version
                        or type(decision.get("approved")) is not bool):
                    raise AgentFailure("CONFIRMATION_SOURCE_MISMATCH")
                if record.confirmation_expires_at and self.clock() >= record.confirmation_expires_at:
                    raise AgentFailure("CONFIRMATION_EXPIRED")
                await asyncio.to_thread(self.tools.confirm, run, record, decision["approved"])
                if not decision["approved"]:
                    raise AgentFailure("CONFIRMATION_REJECTED")
                record.confirmed = True
                run.confirmation_response = None
                await persist()
            return await execute(record)

        try:
            import httpx
            async with httpx.AsyncClient() as async_http:
                with httpx.Client() as sync_http:
                    initial_model = self.model.native_model(
                        timeout=remaining_time(), http_client=sync_http, http_async_client=async_http, streaming=True)
                    graph = SdkAgentLoop(checkpointer=self.checkpointer)
                    def model_task_changed(task: asyncio.Task | None) -> None:
                        if task is None:
                            self.model_tasks.pop(key, None)
                        else:
                            self.model_tasks[key] = task

                    result = await graph.ainvoke(
                        thread_id=run_id, model=initial_model,
                        catalog=[] if run.tool_execution_disabled else self.tools.catalog(),
                        context=context, before_model=before_model, on_model=on_model,
                        on_model_failure=model_failure, model_task_changed=model_task_changed,
                        on_tool=on_tool, on_delta=on_delta, resume=resume,
                        resumed=resumed_question or resumed_confirmation,
                        replay=replay_checkpoint,
                        tools_allowed=not run.tool_execution_disabled,
                    )
            if "__interrupt__" in result:
                if run.question_message_id is not None:
                    waiting_status = "WAITING_FOR_USER"
                elif run.pending_execution is not None and run.pending_execution.status == "PENDING_CONFIRMATION":
                    waiting_status = "WAITING_FOR_CONFIRMATION"
                else:
                    raise AgentFailure("AGENT_INTERRUPT_SOURCE_MISMATCH")
                if run.status != waiting_status:
                    run.status = waiting_status
                    await persist()
            else:
                message = result["messages"][-1]
                if not isinstance(message, AIMessage):
                    raise AgentFailure("FINAL_ANSWER_INVALID")
                answer = validate_model_message(message, tools_allowed=not run.tool_execution_disabled)
                if answer is None or (run.draft is not None and run.draft.issues):
                    raise AgentFailure("UNRESOLVED_TOOL_ARGUMENTS")
                if len(answer) > 16384:
                    raise AgentFailure("FINAL_ANSWER_INVALID")
                if protect_text(answer, internal_values(run.model_dump(mode="json"))) != answer:
                    raise AgentFailure("MODEL_PRIVATE_VALUE_REJECTED")
                selected = self._verified_observations(run)
                for observation in selected:
                    for note in project_units({"notes": []}, observation.unit_annotations)["notes"]:
                        if note not in answer:
                            answer += "\n" + note
                check_budget()
                run.final_message_id = identifier()
                run.pending_message = {"message_id": run.final_message_id, "phase": "answer",
                                       "text": answer, "sources": [o.observation_id for o in selected]}
                run.question_message_id = None
                run.status = "SUCCEEDED"
                await persist()  # Message and success commit in one short transaction.
        except asyncio.CancelledError:
            current = await asyncio.to_thread(self.store.get, run_id, actor_id)
            if current.error_code == "USER_STOPPED":
                return current
            if current.status == "RUNNING":
                current.status, current.error_code = "INTERRUPTED", "PROCESS_INTERRUPTED"
                try:
                    await asyncio.to_thread(self.store.save, current)
                except AgentConflictError:
                    pass  # Concurrent receipt/stop owns the newer version.
            raise
        except AgentConflictError:
            return await asyncio.to_thread(self.store.get, run_id, actor_id)
        except Exception as error:
            run.error_code = (error.code if isinstance(error, AgentFailure) else
                              "LLM_CALL_FAILED" if current_call and current_call.error_code == "LLM_CALL_FAILED"
                              else "AGENT_INTERNAL_ERROR")
            run.status = "TERMINATED"
            run.final_message_id = None
            run.pending_message = None
            try:
                await persist()
            except AgentConflictError:
                return await asyncio.to_thread(self.store.get, run_id, actor_id)
        finally:
            self.stop_events.pop(key, None)
            for call_id, state in streams.items():
                if not state["closed"]:
                    for kind in ("reasoning", "text"):
                        text = state[kind].finish(incomplete=True)
                        if text:
                            await publish_text(call_id, kind, status="interrupted", text=text)
            actual = await asyncio.to_thread(self.store.get, run_id, actor_id)
            await tool_progress(actual)
            await self.process.finish(run_id, actor_id, actual.status, final_message_id=actual.final_message_id)
        if run.terminal and self.checkpoint_cleanup is not None:
            try:
                await self.checkpoint_cleanup.delete(run_id)
                await asyncio.to_thread(self.store.complete_checkpoint_cleanup, run_id)
            except Exception:
                await asyncio.to_thread(self.store.checkpoint_cleanup_failed, run_id)
        return await asyncio.to_thread(self.store.get, run_id, actor_id)

    def _context(self, run: AgentRun) -> dict[str, Any]:
        return {
            "goal": run.goal, "conversation_context": run.context,
            "user_inputs": run.user_inputs, "user_messages": run.user_messages,
            "question": run.waiting.model_dump(mode="json") if run.waiting else None,
            "execution_facts": [{"tool_name": e.tool_name, "status": e.status,
                "observation_id": e.observation_id} for e in run.executions],
            "tools": [] if run.tool_execution_disabled else self.tools.catalog(),
            "tool_execution_disabled": run.tool_execution_disabled,
            "retry_target": None if run.retry_execution is None else run.retry_execution.model_dump(mode="json"),
            "draft": model_draft(run.draft),
            "observations": [model_observation(o) for o in run.observations if o.kind == "TOOL_RESULT"],
        }

    @staticmethod
    def _verified_observations(run: AgentRun) -> list[Observation]:
        owned = {record.observation_id: record for record in run.executions if record.observation_id}
        selected = []
        for observation in run.observations:
            if observation.kind != "TOOL_RESULT":
                continue
            record = owned.get(observation.observation_id)
            if observation.source_agent_run_id:
                if run.retry_type != "ANSWER_REGENERATION":
                    raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
            elif (record is None or record.invocation_run_id != observation.invocation_run_id
                  or record.tool_call_id != observation.tool_call_id):
                raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
            selected.append(observation)
        return selected
