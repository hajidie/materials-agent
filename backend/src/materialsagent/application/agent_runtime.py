"""Request-hosted runtime. No HTTP, SQLAlchemy, Provider or worker dependencies."""
from __future__ import annotations

from datetime import datetime, timedelta
import time
import asyncio
from typing import Any, Callable

from pydantic import ValidationError

from materialsagent.domain.models.agent import (
    ACTION_ADAPTER, AgentRun, AgentStep, AskUser, CallTool, ExecutionRecord,
    Finish, ModelCall, Observation, canonical, fingerprint, identifier, now,
)
from materialsagent.domain.ports.agent import (
    AgentConflictError, AgentFailure, AgentModelPort, AgentStore, AgentToolGateway,
)
from materialsagent.domain.models.ml_resource_context import model_observation, model_draft, PROTOCOL_VERSION
from .context_framework import ContextFramework
from .result_projection import project_result, FIELD_LABELS


class DecisionEngine:
    @staticmethod
    def action(raw):
        try:
            return ACTION_ADAPTER.validate_python(raw)
        except (ValidationError, ValueError):
            raise AgentFailure("AGENT_ACTION_PROTOCOL_ERROR") from None


class AgentRuntime:
    def __init__(self, store: AgentStore, model: AgentModelPort, tools: AgentToolGateway,
                 *, process_id: str | None = None, monotonic: Callable[[], float] = time.monotonic, clock: Callable[[], datetime] = now):
        self.store, self.model, self.tools = store, model, tools
        self.process_id = process_id or identifier()
        self.monotonic = monotonic
        self.clock = clock
        self.context_framework = ContextFramework()
        self.model_tasks = {}
        self.stop_events = {}
        self.receipt_tasks = set()

    async def close(self):
        for task in list(self.model_tasks.values()):
            task.cancel()
        if self.model_tasks:
            await asyncio.gather(*self.model_tasks.values(), return_exceptions=True)
        if self.receipt_tasks:
            await asyncio.wait(list(self.receipt_tasks), timeout=30)

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
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # The persisted stop is authoritative even if the request also failed.
        return run, stopped

    async def advance(self, run_id: str, actor_id: str, *, submission_id: str | None = None,
                      waiting_version: int | None = None, confirmation: bool | None = None) -> AgentRun:
        run = await asyncio.to_thread(self.store.get, run_id, actor_id)
        if submission_id is not None and run.submission_id != submission_id:
            raise AgentConflictError("Submission is stale.")
        if run.terminal or run.status in {"RUNNING", "WAITING_FOR_USER"}:
            return run
        if run.status == "WAITING_FOR_CONFIRMATION":
            if waiting_version != run.waiting_version or confirmation is None:
                raise AgentConflictError("Confirmation version is stale.")
        turn_key = (run_id, run.submission_id)
        stopped_event = self.stop_events.setdefault(turn_key, asyncio.Event())
        run.status, run.claim, run.process_id = "RUNNING", identifier(), self.process_id
        try:
            await asyncio.to_thread(self.store.save, run)  # Exactly one request owns advancement.
        except AgentConflictError:
            current = await asyncio.to_thread(self.store.get, run_id, actor_id)
            if current.terminal:
                self.stop_events.pop(turn_key, None)
            return current
        last = self.monotonic()
        resources = getattr(self.tools, "resource_context", None)

        async def persist() -> None:
            nonlocal last
            current = self.monotonic()
            run.active_seconds += max(0, current - last)
            last = current
            run.updated_at = now()
            await asyncio.to_thread(self.store.save, run)

        def remaining_time() -> float:
            return run.budget.max_active_seconds - run.active_seconds - max(0, self.monotonic() - last)

        def check_budget(*, decision: bool = False) -> None:
            if remaining_time() <= 0:
                raise AgentFailure("AGENT_ACTIVE_TIME_EXCEEDED")
            if run.llm_tokens >= run.budget.max_llm_tokens:
                raise AgentFailure("LLM_TOKEN_BUDGET_EXCEEDED")
            if decision and len(run.steps) >= run.budget.max_action_steps:
                raise AgentFailure("AGENT_STEP_BUDGET_EXCEEDED")

        async def model_call(role: str, payload: dict[str, Any], step: AgentStep) -> Any:
            check_budget()
            if resources is not None and not run.tool_execution_disabled:
                payload = {**payload, "resource_context": await asyncio.to_thread(resources.context, run)}
            frame = self.context_framework.build("answer_regeneration" if run.tool_execution_disabled else role, payload, run)
            request = self.model.prepare(role, frame.payload, run.budget.max_llm_tokens - run.llm_tokens, remaining_time())
            call = ModelCall(step_id=step.step_id, role=role, prompt_digest=fingerprint(request.messages), output_limit=request.output_limit, input_reserved=request.input_estimate)
            run.calls.append(call)
            await persist()
            if stopped_event.is_set():
                raise AgentConflictError("Submission was stopped before model dispatch.")
            try:
                task = asyncio.create_task(self.model.ainvoke(request))
                self.model_tasks[(run_id, run.submission_id)] = task
                try:
                    response = await task
                finally:
                    self.model_tasks.pop((run_id, run.submission_id), None)
                call.usage = response.usage
                call.status = "FAILED" if response.error_code else "SUCCEEDED"
                call.error_code = response.error_code
                run.llm_tokens += response.usage.total_tokens
            except Exception:
                call.status, call.error_code = "FAILED", "LLM_CALL_FAILED"
                # A failed external call can still consume output. Reserve it conservatively.
                from materialsagent.domain.models.agent import TokenUsage
                call.usage = TokenUsage(input_tokens=request.input_estimate, output_tokens=request.output_limit,
                                        total_tokens=request.input_estimate + request.output_limit,
                                        source="estimated", estimator_version="cl100k-x2-or-utf8-framing-v1")
                run.llm_tokens += call.usage.total_tokens
                await persist()
                raise AgentFailure("LLM_CALL_FAILED") from None
            await persist()
            check_budget()
            if response.error_code:
                raise AgentFailure(response.error_code)
            return self.context_framework.output(frame, response.value, run)

        def argument_observation(step: AgentStep) -> None:
            draft = run.draft
            assert draft is not None
            run.observations.append(Observation(step_id=step.step_id, kind="ARGUMENT_RESOLUTION",
                status="NEEDS_INPUT" if draft.issues else "READY", tool_name=draft.tool_name,
                task_id=draft.task_id, data={"normalized": draft.normalized, "issues": draft.issues}))

        async def execute(record: ExecutionRecord) -> None:
            nonlocal run
            if run.tool_execution_disabled:
                raise AgentFailure("TOOL_EXECUTION_DISABLED")
            check_budget()
            if run.tool_executions >= run.budget.max_tool_executions:
                raise AgentFailure("TOOL_EXECUTION_BUDGET_EXCEEDED")
            duplicate = next((item for item in run.executions if item.execution_fingerprint == record.execution_fingerprint and item.status == "SUCCEEDED"), None)
            if duplicate:
                run.duplicate_of_invocation_run_id = duplicate.invocation_run_id
                raise AgentFailure("DUPLICATE_TOOL_CALL")
            # Persist the action dispatch before any external execution. No transaction remains open.
            record.dispatched = True
            record.status = "RUNNING"
            run.tool_executions += 1
            await persist()
            profile = next((t.get("execution_profile") for t in self.tools.catalog() if t["tool_name"] == record.tool_name), None)
            timeout = run.budget.managed_timeout_seconds if profile == "MANAGED" else run.budget.standard_timeout_seconds
            async def complete():
                try:
                    observation = await asyncio.to_thread(self.tools.execute, run, record, min(timeout, remaining_time()))
                except Exception:
                    observation = await asyncio.to_thread(self.tools.repair, run, record)
                    if observation is None:
                        return await asyncio.to_thread(self.store.fail_execution, run_id, actor_id, record)
                if observation.invocation_run_id != record.invocation_run_id:
                    raise AgentFailure("OBSERVATION_SOURCE_MISMATCH")
                return await asyncio.to_thread(self.store.receipt, run_id, actor_id, record, observation)
            receipt = asyncio.create_task(complete())
            self.receipt_tasks.add(receipt)
            def completed(task):
                self.receipt_tasks.discard(task)
                if not task.cancelled():
                    task.exception()  # Retrieve failures even when the HTTP caller disconnected.
            receipt.add_done_callback(completed)
            run = await asyncio.shield(receipt)
            if run.status == "TERMINATED":
                return
            if record.status == "FAILED":
                raise AgentFailure("TOOL_EXECUTION_FAILED")
            check_budget()

        try:
            if confirmation is not None:
                record = run.pending_execution
                if record is None:
                    raise AgentFailure("CONFIRMATION_SOURCE_MISMATCH")
                signature = fingerprint([record.tool_name, record.version, record.schema_hash, record.arguments])
                if signature != record.execution_fingerprint or record.confirmation_version != fingerprint([record.invocation_run_id, signature]):
                    raise AgentFailure("CONFIRMATION_INVALIDATED")
                if record.confirmation_expires_at and self.clock() >= record.confirmation_expires_at:
                    raise AgentFailure("CONFIRMATION_EXPIRED")
                await asyncio.to_thread(self.tools.confirm, run, record, confirmation)
                if not confirmation:
                    raise AgentFailure("CONFIRMATION_REJECTED")
                record.confirmed = True
                await persist()
                await execute(record)
            proposal_error = None
            duplicate_corrections = set()
            while run.status == "RUNNING":
                check_budget(decision=True)
                for completed in run.executions:
                    if not any(o.invocation_run_id == completed.invocation_run_id for o in run.observations):
                        repaired = await asyncio.to_thread(self.tools.repair, run, completed)
                        if repaired is None:
                            raise AgentFailure("OBSERVATION_INCONSISTENT")
                        run.observations.append(repaired)
                        completed.observation_id = repaired.observation_id
                        await persist()
                step = AgentStep(number=len(run.steps) + 1)
                run.steps.append(step)
                await persist()
                payload = self._context(run)
                if proposal_error:
                    payload["proposal_error"] = proposal_error
                try:
                    raw = await model_call("agent_decision", payload, step)
                    action = DecisionEngine.action(raw)
                except AgentFailure as failure:
                    if failure.code not in {"LLM_INVALID_JSON", "LLM_EMPTY_RESPONSE", "AGENT_ACTION_PROTOCOL_ERROR"} or proposal_error:
                        raise
                    # One correction for a delivered, malformed proposal. It uses
                    # the same decision model and cumulative budgets; no execution
                    # or successful Observation is repeated and no raw output leaks.
                    step.status = "FAILED"
                    proposal_error = "上一次响应不符合输出合同。严格返回单个 CallTool、AskUser 或 Finish JSON 动作，不返回 Schema、格式描述或代码围栏。"
                    await persist()
                    continue
                proposal_error = None
                # The decoded proposal may temporarily contain trusted handles.
                # Persist only business arguments; verified identities belong in
                # the typed draft/execution bindings created below.
                step.action_type = action.type
                step.action = self._audit_action(action) if isinstance(action, CallTool) else None
                await persist()
                if isinstance(action, CallTool):
                    if run.tool_execution_disabled:
                        raise AgentFailure("TOOL_EXECUTION_DISABLED")
                    run.current_unit_assertions = action.user_unit_assertions
                    run.draft = await asyncio.to_thread(self.tools.resolve, run, action.tool_name, action.arguments)
                    if run.draft.issues:
                        argument_observation(step)
                        step.status = "COMPLETED"
                        await persist()
                        continue
                    draft = run.draft
                    # A resolved intent question has been consumed. Keeping it in
                    # subsequent Decision context can replay the user's answer
                    # after the Tool result already satisfies the request.
                    run.question_message_id = None
                    signature = fingerprint([draft.tool_name, draft.version, draft.schema_hash, draft.normalized])
                    duplicate = next((e for e in run.executions if e.execution_fingerprint == signature and e.status == "SUCCEEDED"), None)
                    if duplicate:
                        run.duplicate_of_invocation_run_id = duplicate.invocation_run_id
                        if signature in duplicate_corrections:
                            raise AgentFailure("DUPLICATE_TOOL_CALL")
                        duplicate_corrections.add(signature)
                        step.status = "FAILED"
                        run.draft = None
                        proposal_error = "相同工具与完整参数已成功执行，禁止再次执行。所需结果已在 Observation 和 execution_facts 中；请使用已有结果继续，目标已满足时直接 Finish。"
                        await persist()
                        continue
                    if run.tool_executions >= run.budget.max_tool_executions:
                        raise AgentFailure("TOOL_EXECUTION_BUDGET_EXCEEDED")
                    record = ExecutionRecord(action_id=step.step_id, tool_name=draft.tool_name,
                        version=draft.version, schema_hash=draft.schema_hash, arguments=draft.normalized,
                        execution_fingerprint=signature, resource_bindings=draft.resource_bindings, unit_annotations=draft.unit_annotations)
                    if run.retry_execution:
                        if signature != run.retry_execution.execution_fingerprint:
                            raise AgentFailure("TOOL_RETRY_ARGUMENT_MISMATCH")
                        record.retry_of_invocation_run_id = run.retry_execution.invocation_run_id
                    run.pending_execution = record
                    await persist()
                    record = await asyncio.to_thread(self.tools.prepare, run, record)
                    run.pending_execution = record
                    await persist()
                    if record.status == "PENDING_CONFIRMATION":
                        run.status = "WAITING_FOR_CONFIRMATION"
                        run.waiting_version += 1
                        record.confirmation_version = fingerprint([record.invocation_run_id, signature])
                        await persist()
                        break
                    await execute(record)
                elif isinstance(action, AskUser):
                    run.question_message_id = identifier()
                    run.waiting_version += 1
                    run.pending_message = {"message_id": run.question_message_id, "phase": "question",
                                           "text": action.question, "sources": [], "step_id": step.step_id}
                    step.message_id = run.question_message_id
                    run.status = "WAITING_FOR_USER"
                    step.status = "COMPLETED"
                    await persist()
                else:
                    selected = self._finish_observations(run, action)
                    answer = action.answer.strip()
                    if not answer:
                        raise AgentFailure("FINAL_ANSWER_INVALID")
                    from .unit_resolution import project_units
                    for observation in selected:
                        for note in project_units({"notes": []}, observation.unit_annotations)["notes"]:
                            if note not in answer:
                                answer += "\n" + note
                    check_budget()
                    run.final_message_id = identifier()
                    run.pending_message = {"message_id": run.final_message_id, "phase": "answer", "text": answer,
                                           "sources": [o.observation_id for o in selected], "step_id": step.step_id}
                    step.message_id = run.final_message_id
                    run.question_message_id = None
                    step.status, run.status = "COMPLETED", "SUCCEEDED"
                    await persist()
        except asyncio.CancelledError:
            current = await asyncio.to_thread(self.store.get, run_id, actor_id)
            if current.error_code == "USER_STOPPED":
                return current
            raise
        except AgentConflictError:
            return await asyncio.to_thread(self.store.get, run_id, actor_id)
        except Exception as error:
            run.error_code = error.code if isinstance(error, AgentFailure) else "AGENT_INTERNAL_ERROR"
            run.status = "TERMINATED"
            run.final_message_id = None
            run.pending_message = None
            if run.steps and run.steps[-1].status == "RUNNING":
                run.steps[-1].status = "FAILED"
            try:
                await persist()
            except AgentConflictError:
                return await asyncio.to_thread(self.store.get, run_id, actor_id)
        finally:
            self.stop_events.pop(turn_key, None)
        return await asyncio.to_thread(self.store.get, run_id, actor_id)

    def _tool(self, name: str) -> dict[str, Any]:
        for tool in self.tools.catalog():
            if tool["tool_name"] == name:
                return tool
        raise AgentFailure("UNKNOWN_TOOL")

    def _audit_action(self, action):
        name = getattr(action, "tool_name", None)
        if not name or not isinstance(action, (CallTool, AskUser)):
            return action
        tool = self._tool(name)
        fields = {item["execution_argument"] for item in tool.get("resource_parameters", ())}
        if not fields:
            return action
        value = action.model_dump(mode="json")
        key = "arguments"
        value["user_unit_assertions"] = []
        value[key] = {field: item for field, item in value[key].items() if field not in fields}
        return DecisionEngine.action(value)

    def _context(self, run: AgentRun) -> dict[str, Any]:
        return {
            "goal": run.goal, "conversation_context": run.context, "user_inputs": run.user_inputs,
            "user_messages": run.user_messages,
            "question": run.waiting.model_dump(mode="json") if run.waiting else None,
            "waiting_version": run.waiting_version,
            "execution_facts": [{"tool_name": e.tool_name, "arguments": e.arguments, "status": e.status,
                "observation_id": e.observation_id} for e in run.executions],
            "run_status": run.status,
            "remaining_budget": {"action_steps": run.budget.max_action_steps - len(run.steps),
                "tool_executions": run.budget.max_tool_executions - run.tool_executions,
                "llm_tokens": run.budget.max_llm_tokens - run.llm_tokens,
                "active_seconds": max(0, run.budget.max_active_seconds - run.active_seconds)},
            "tools": [] if run.tool_execution_disabled else self.tools.catalog(),
            "tool_execution_disabled": run.tool_execution_disabled,
            "retry_target": None if run.retry_execution is None else run.retry_execution.model_dump(mode="json"),
            "draft": model_draft(run.draft),
            "actions": [s.action.model_dump(mode="json") for s in run.steps if s.action],
            # The authoritative current draft replaces superseded clarification diagnostics.
            # Full questions and argument Observations remain persisted in the trace.
            "observations": [model_observation(o) for o in run.observations if o.kind == "TOOL_RESULT"],
        }

    @staticmethod
    def _finish_observations(run: AgentRun, action: Finish) -> list[Observation]:
        available = {o.observation_id: o for o in run.observations if o.kind == "TOOL_RESULT"}
        if not set(action.observation_ids) <= available.keys():
            raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
        if run.draft and run.draft.issues:
            raise AgentFailure("UNRESOLVED_TOOL_ARGUMENTS")
        return [available[i] for i in action.observation_ids] if action.observation_ids else list(available.values())
