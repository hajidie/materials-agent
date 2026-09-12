"""Request-hosted runtime. No HTTP, SQLAlchemy, Provider or worker dependencies."""
from __future__ import annotations

from datetime import datetime, timedelta
import time
from typing import Any, Callable

from pydantic import ValidationError

from materialsagent.domain.models.agent import (
    ACTION_ADAPTER, AgentRun, AgentStep, AskUser, CallTool, ExecutionRecord,
    FinalAnswer, Finish, ModelCall, Observation, canonical, fingerprint, identifier, now,
)
from materialsagent.domain.ports.agent import (
    AgentConflictError, AgentFailure, AgentModelPort, AgentStore, AgentToolGateway,
)


class FinalResponsePolicy:
    """Answer completeness is explicit; execution profile is irrelevant."""
    @staticmethod
    def direct(action: Finish, observations: list[Observation]) -> str | None:
        if action.needs_synthesis:
            return None
        if action.answer and action.answer.strip():
            return action.answer.strip()
        if len(observations) == 1:
            presentation = observations[0].presentation
            text = presentation.get("text") or presentation.get("summary")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return None


class DecisionEngine:
    @staticmethod
    def action(raw):
        try:
            return ACTION_ADAPTER.validate_python(raw)
        except (ValidationError, ValueError):
            raise AgentFailure("AGENT_ACTION_PROTOCOL_ERROR") from None


class FinalAnswerGenerator:
    @staticmethod
    def generate(run, observations, step, model_call):
        return model_call("final_answer", {"goal": run.goal, "user_inputs": run.user_inputs,
            "context": run.context, "observations": [o.agent_projection() for o in observations]}, step)


class AgentRuntime:
    def __init__(self, store: AgentStore, model: AgentModelPort, tools: AgentToolGateway,
                 *, process_id: str | None = None, monotonic: Callable[[], float] = time.monotonic, clock: Callable[[], datetime] = now):
        self.store, self.model, self.tools = store, model, tools
        self.process_id = process_id or identifier()
        self.monotonic = monotonic
        self.clock = clock

    def advance(self, run_id: str, actor_id: str, *, waiting_version: int | None = None,
                user_input: str | None = None, confirmation: bool | None = None) -> AgentRun:
        run = self.store.get(run_id, actor_id)
        if run.terminal or run.status == "RUNNING":
            return run
        resuming_arguments = False
        if run.status.startswith("WAITING_"):
            if waiting_version != run.waiting_version:
                raise AgentConflictError("Waiting version is stale.")
            if run.status == "WAITING_FOR_USER":
                if not user_input or not user_input.strip() or confirmation is not None:
                    raise AgentConflictError("A user response is required.")
                resuming_arguments = bool(run.waiting and run.waiting.reason == "TOOL_ARGUMENT_CLARIFICATION")
                run.user_inputs.append(user_input)
            elif confirmation is None or user_input is not None:
                raise AgentConflictError("An Invocation confirmation is required.")
        elif user_input is not None or confirmation is not None:
            raise AgentConflictError("Run is not waiting.")
        run.status, run.claim, run.process_id = "RUNNING", identifier(), self.process_id
        self.store.save(run)  # The only owner proceeds beyond this CAS.
        last = self.monotonic()

        def persist() -> None:
            nonlocal last
            current = self.monotonic()
            run.active_seconds += max(0, current - last)
            last = current
            run.updated_at = now()
            self.store.save(run)

        def remaining_time() -> float:
            return run.budget.max_active_seconds - run.active_seconds - max(0, self.monotonic() - last)

        def check_budget(*, decision: bool = False) -> None:
            if remaining_time() <= 0:
                raise AgentFailure("AGENT_ACTIVE_TIME_EXCEEDED")
            if run.llm_tokens >= run.budget.max_llm_tokens:
                raise AgentFailure("LLM_TOKEN_BUDGET_EXCEEDED")
            if decision and len(run.steps) >= run.budget.max_action_steps:
                raise AgentFailure("AGENT_STEP_BUDGET_EXCEEDED")

        def model_call(role: str, payload: dict[str, Any], step: AgentStep) -> Any:
            check_budget()
            request = self.model.prepare(role, payload, run.budget.max_llm_tokens - run.llm_tokens, remaining_time())
            call = ModelCall(step_id=step.step_id, role=role, prompt_digest=fingerprint(request.messages), output_limit=request.output_limit, input_reserved=request.input_estimate)
            run.calls.append(call)
            persist()
            try:
                response = self.model.invoke(request)
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
                persist()
                raise AgentFailure("LLM_CALL_FAILED") from None
            persist()
            check_budget()
            if response.error_code:
                raise AgentFailure(response.error_code)
            return response.value

        def argument_observation(step: AgentStep) -> None:
            draft = run.draft
            assert draft is not None
            run.observations.append(Observation(step_id=step.step_id, kind="ARGUMENT_RESOLUTION",
                status="NEEDS_INPUT" if draft.issues else "READY", tool_name=draft.tool_name,
                task_id=draft.task_id, data={"normalized": draft.normalized, "issues": draft.issues}))

        def execute(record: ExecutionRecord) -> None:
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
            persist()
            profile = next((t.get("execution_profile") for t in self.tools.catalog() if t["tool_name"] == record.tool_name), None)
            timeout = run.budget.managed_timeout_seconds if profile == "MANAGED" else run.budget.standard_timeout_seconds
            try:
                observation = self.tools.execute(run, record, min(timeout, remaining_time()))
            except Exception:
                # Repair only from committed facts; never execute again to fill an Observation gap.
                observation = self.tools.repair(run, record)
                if observation is None:
                    record.status = "FAILED"
                    persist()
                    raise AgentFailure("TOOL_EXECUTION_FAILED") from None
            observation.step_id = record.action_id
            if observation.invocation_run_id != record.invocation_run_id:
                raise AgentFailure("OBSERVATION_SOURCE_MISMATCH")
            record.status = observation.status
            record.task_id, record.tool_run_id = observation.task_id, observation.tool_run_id
            record.retryable = bool(observation.error and observation.error.get("retryable") is True)
            record.observation_id = observation.observation_id
            run.observations.append(observation)
            run.executions.append(record.model_copy(deep=True))
            run.pending_execution = None
            run.draft = None
            run.retry_execution = None
            step = next(s for s in run.steps if s.step_id == record.action_id)
            step.status = "COMPLETED"
            persist()  # Observation + completed Step + Run CAS form one store transaction.
            if observation.status == "FAILED":
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
                self.tools.confirm(run, record, confirmation)
                if not confirmation:
                    raise AgentFailure("CONFIRMATION_REJECTED")
                record.confirmed = True
                persist()
                execute(record)
            if resuming_arguments:
                if run.draft is None:
                    raise AgentFailure("ARGUMENT_STATE_MISSING")
                step = run.steps[-1]
                delta = model_call("tool_arg_resolution", {
                    "tool": self._tool(run.draft.tool_name), "draft": run.draft.model_dump(mode="json"),
                    "user_input": user_input,
                }, step)
                if not isinstance(delta, dict):
                    raise AgentFailure("ARGUMENT_PROTOCOL_ERROR")
                run.draft = self.tools.resolve(run, run.draft.tool_name, delta)
                argument_observation(step)
                run.waiting = None
                persist()
            while run.status == "RUNNING":
                check_budget(decision=True)
                for completed in run.executions:
                    if not any(o.invocation_run_id == completed.invocation_run_id for o in run.observations):
                        repaired = self.tools.repair(run, completed)
                        if repaired is None:
                            raise AgentFailure("OBSERVATION_INCONSISTENT")
                        run.observations.append(repaired)
                        completed.observation_id = repaired.observation_id
                        persist()
                step = AgentStep(number=len(run.steps) + 1)
                run.steps.append(step)
                persist()
                payload = self._context(run)
                raw = model_call("agent_decision", payload, step)
                action = DecisionEngine.action(raw)
                step.action = action
                persist()
                if isinstance(action, CallTool):
                    if run.tool_execution_disabled:
                        raise AgentFailure("TOOL_EXECUTION_DISABLED")
                    if run.draft and run.draft.tool_name != action.tool_name:
                        raise AgentFailure("BOUND_TOOL_MISMATCH")
                    if run.draft and run.draft.resolver_authoritative and run.draft.issues:
                        raise AgentFailure("ARGUMENT_STATE_REQUIRES_USER")
                    run.draft = self.tools.resolve(run, action.tool_name, action.arguments)
                    if run.draft.issues:
                        argument_observation(step)
                        step.status = "COMPLETED"
                        persist()
                        continue
                    draft = run.draft
                    signature = fingerprint([draft.tool_name, draft.version, draft.schema_hash, draft.normalized])
                    duplicate = next((e for e in run.executions if e.execution_fingerprint == signature and e.status == "SUCCEEDED"), None)
                    if duplicate:
                        run.duplicate_of_invocation_run_id = duplicate.invocation_run_id
                        raise AgentFailure("DUPLICATE_TOOL_CALL")
                    if run.tool_executions >= run.budget.max_tool_executions:
                        raise AgentFailure("TOOL_EXECUTION_BUDGET_EXCEEDED")
                    record = ExecutionRecord(action_id=step.step_id, tool_name=draft.tool_name,
                        version=draft.version, schema_hash=draft.schema_hash, arguments=draft.normalized,
                        execution_fingerprint=signature)
                    if run.retry_execution:
                        if signature != run.retry_execution.execution_fingerprint:
                            raise AgentFailure("TOOL_RETRY_ARGUMENT_MISMATCH")
                        record.retry_of_invocation_run_id = run.retry_execution.invocation_run_id
                    run.pending_execution = record
                    persist()
                    record = self.tools.prepare(run, record)
                    run.pending_execution = record
                    persist()
                    if record.status == "PENDING_CONFIRMATION":
                        run.status = "WAITING_FOR_CONFIRMATION"
                        run.waiting_version += 1
                        record.confirmation_version = fingerprint([record.invocation_run_id, signature])
                        persist()
                        break
                    execute(record)
                elif isinstance(action, AskUser):
                    if action.reason == "TOOL_ARGUMENT_CLARIFICATION":
                        if run.tool_execution_disabled:
                            raise AgentFailure("TOOL_EXECUTION_DISABLED")
                        self._validate_question(run, action)
                        # Public question wording cannot invent ranges/defaults not in the schema.
                        action.question = "请补充或明确以下参数：" + "、".join(action.fields) + "。"
                    run.waiting = action
                    run.waiting_version += 1
                    run.status = "WAITING_FOR_USER"
                    step.status = "COMPLETED"
                    persist()
                else:
                    selected = self._finish_observations(run, action)
                    answer = FinalResponsePolicy.direct(action, selected)
                    generated = answer is None
                    if answer is None:
                        answer = FinalAnswerGenerator.generate(run, selected, step, model_call)
                    if not isinstance(answer, str) or not answer.strip():
                        raise AgentFailure("FINAL_ANSWER_INVALID")
                    check_budget()
                    run.final_answer = FinalAnswer(step_id=step.step_id, text=answer.strip(),
                        observation_ids=[o.observation_id for o in selected], generated=generated)
                    step.status, run.status = "COMPLETED", "SUCCEEDED"
                    persist()
        except AgentConflictError:
            return self.store.get(run_id, actor_id)
        except Exception as error:
            run.error_code = error.code if isinstance(error, AgentFailure) else "AGENT_INTERNAL_ERROR"
            run.status = "TERMINATED"
            run.final_answer = None
            if run.steps and run.steps[-1].status == "RUNNING":
                run.steps[-1].status = "FAILED"
            try:
                persist()
            except AgentConflictError:
                return self.store.get(run_id, actor_id)
        return self.store.get(run_id, actor_id)

    def _tool(self, name: str) -> dict[str, Any]:
        for tool in self.tools.catalog():
            if tool["tool_name"] == name:
                return tool
        raise AgentFailure("UNKNOWN_TOOL")

    def _validate_question(self, run: AgentRun, action: AskUser) -> None:
        tool = self._tool(action.tool_name or "")
        fields = set(tool["schema"].get("properties", {}))
        if not set(action.fields) <= fields or len(set(action.fields)) != len(action.fields):
            raise AgentFailure("CLARIFICATION_FIELD_INVALID")
        if run.draft and run.draft.tool_name != action.tool_name:
            raise AgentFailure("BOUND_TOOL_MISMATCH")
        if run.draft and run.draft.resolver_authoritative:
            if not set(action.fields) <= set(run.draft.issues):
                raise AgentFailure("CLARIFICATION_CONTRADICTS_RESOLVER")
            # A model may restate resolved facts, but cannot add/change values or
            # promote an unresolved field to known. Never merge this echo into the draft.
            for field, value in action.known_arguments.items():
                if (field not in fields or field in run.draft.issues
                        or field not in run.draft.normalized
                        or canonical(value) != canonical(run.draft.normalized[field])):
                    raise AgentFailure("CLARIFICATION_CONTRADICTS_RESOLVER")
        else:
            # Deterministic validation only: proactive AskUser never manufactures a CallTool.
            run.draft = self.tools.resolve(run, action.tool_name or "", action.known_arguments)
            if not set(action.fields) <= set(run.draft.issues):
                raise AgentFailure("CLARIFICATION_FIELD_ALREADY_KNOWN")

    def _context(self, run: AgentRun) -> dict[str, Any]:
        return {
            "goal": run.goal, "conversation_context": run.context, "user_inputs": run.user_inputs,
            "run_status": run.status,
            "remaining_budget": {"action_steps": run.budget.max_action_steps - len(run.steps),
                "tool_executions": run.budget.max_tool_executions - run.tool_executions,
                "llm_tokens": run.budget.max_llm_tokens - run.llm_tokens,
                "active_seconds": max(0, run.budget.max_active_seconds - run.active_seconds)},
            "tools": [] if run.tool_execution_disabled else self.tools.catalog(),
            "tool_execution_disabled": run.tool_execution_disabled,
            "retry_target": None if run.retry_execution is None else run.retry_execution.model_dump(mode="json"),
            "draft": None if run.draft is None else run.draft.model_dump(mode="json"),
            "actions": [s.action.model_dump(mode="json") for s in run.steps if s.action],
            "observations": [o.agent_projection() for o in run.observations],
        }

    @staticmethod
    def _finish_observations(run: AgentRun, action: Finish) -> list[Observation]:
        available = {o.observation_id: o for o in run.observations if o.kind == "TOOL_RESULT"}
        if not set(action.observation_ids) <= available.keys():
            raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
        if run.draft and run.draft.issues:
            raise AgentFailure("UNRESOLVED_TOOL_ARGUMENTS")
        return [available[i] for i in action.observation_ids] if action.observation_ids else list(available.values())
