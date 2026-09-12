from types import SimpleNamespace

import pytest

from materialsagent.application.agent_runtime import AgentRuntime
from materialsagent.domain.models.agent import AgentRun, ArgumentDraft, Observation, RunBudget, identifier
from materialsagent.domain.ports.agent import AgentConflictError
from materialsagent.infrastructure.llm.agent_model import MockAgentModel, normalize_usage
from materialsagent.domain.ports.agent import PreparedAgentCall


class Store:
    def __init__(self, run):
        self.run = run.model_copy(deep=True)
        self.transactions = 0

    def get(self, run_id, actor_id):
        assert actor_id == self.run.actor_id
        return self.run.model_copy(deep=True)

    def save(self, run):
        if run.version != self.run.version:
            raise AgentConflictError()
        run.validate_transition(self.run)
        run.version += 1
        self.run = run.model_copy(deep=True)


class Tools:
    def __init__(self, store):
        self.store = store
        self.executed = []
        self.resolved = []
        self.require_confirmation = False

    def catalog(self):
        return [{"tool_name": name, "schema": {"properties": {"value": {"type": "number"}}}, "execution_profile": "STANDARD"} for name in ["predict", "convert"]]

    def resolve(self, run, name, arguments):
        self.resolved.append(arguments)
        values = {**(run.draft.normalized if run.draft else {}), **arguments}
        return ArgumentDraft(tool_name=name, version="1", schema_hash="a" * 64, arguments=values, normalized=values,
            issues={} if isinstance(values.get("value"), (int, float)) else {"value": "Missing"}, resolver_authoritative=True)

    def prepare(self, run, record):
        record.invocation_run_id = identifier()
        if self.require_confirmation:
            record.status = "PENDING_CONFIRMATION"
        return record

    def execute(self, run, record, timeout):
        assert self.store.transactions == 0
        assert self.store.run.pending_execution.dispatched
        self.executed.append(record)
        return Observation(step_id=record.action_id, kind="TOOL_RESULT", status="SUCCEEDED", tool_name=record.tool_name,
            invocation_run_id=record.invocation_run_id, data={"value": record.arguments["value"] * 2})

    def confirm(self, run, record, approved):
        pass

    def repair(self, run, record):
        return None


def setup(responder, **kwargs):
    run = AgentRun(conversation_id="conversation", actor_id="actor", source_message_id="message", goal="test", **kwargs)
    store = Store(run)
    tools = Tools(store)
    runtime = AgentRuntime(store, MockAgentModel(responder), tools)
    return run, store, tools, runtime


def test_multi_tool_observation_loop_and_final_commit():
    calls = []
    def respond(role, payload):
        calls.append(role)
        if not payload["observations"]:
            return {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 2}}
        if len(payload["observations"]) == 1:
            assert payload["observations"][0]["data"]["value"] == 4
            return {"type": "CallTool", "tool_name": "convert", "arguments": {"value": 4}}
        return {"type": "Finish", "answer": "8"}
    run, store, tools, runtime = setup(respond)
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED", result.error_code
    assert result.final_answer.text == "8"
    assert len(tools.executed) == 2
    assert calls == ["agent_decision"] * 3
    replay = runtime.advance(run.agent_run_id, "actor")
    assert replay.final_answer == result.final_answer
    assert len(tools.executed) == 2


def test_proactive_ask_then_incremental_resume_without_initial_extraction():
    roles = []
    def respond(role, payload):
        roles.append(role)
        if role == "tool_arg_resolution":
            return {"value": 3}
        if any(o["kind"] == "TOOL_RESULT" for o in payload["observations"]):
            return {"type": "Finish", "answer": "完成"}
        if payload["draft"] and not payload["draft"]["issues"]:
            return {"type": "CallTool", "tool_name": "predict", "arguments": {}}
        return {"type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION", "tool_name": "predict", "fields": ["value"], "question": "数值？"}
    run, store, tools, runtime = setup(respond)
    waiting = runtime.advance(run.agent_run_id, "actor")
    assert waiting.status == "WAITING_FOR_USER", waiting.error_code
    assert len(tools.executed) == 0
    assert roles == ["agent_decision"]
    result = runtime.advance(run.agent_run_id, "actor", waiting_version=waiting.waiting_version, user_input="3")
    assert result.status == "SUCCEEDED", result.error_code
    assert roles.count("tool_arg_resolution") == 1
    assert result.agent_run_id == waiting.agent_run_id
    assert result.llm_tokens > waiting.llm_tokens


@pytest.mark.parametrize("field", ["invented", "value"])
def test_invalid_or_known_proactive_field_rejected(field):
    run, _, tools, runtime = setup(lambda *_: {"type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION",
        "tool_name": "predict", "fields": [field], "question": "?", "known_arguments": {"value": 1}})
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.status == "TERMINATED"
    assert not tools.executed


@pytest.mark.parametrize("issue", ["Missing", "Invalid", "Conflict", "Ambiguous"])
@pytest.mark.parametrize(("known", "asked", "allowed"), [
    ({}, ["value"], True),
    ({"unit": "MPa"}, ["value"], True),
    ({"unit": "GPa"}, ["value"], False),
    ({"value": 5}, ["value"], False),
    ({"value": None}, ["value"], False),
    ({"invented": "MPa"}, ["value"], False),
    ({"unit": "MPa"}, ["unit"], False),
])
def test_resolver_clarification_accepts_only_unchanged_reliable_facts(issue, known, asked, allowed):
    draft = ArgumentDraft(tool_name="predict", version="1", schema_hash="a" * 64,
        arguments={"value": None, "unit": "MPa"}, normalized={"value": None, "unit": "MPa"},
        issues={"value": issue}, resolver_authoritative=True)
    run, _, tools, runtime = setup(lambda *_: {
        "type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION", "tool_name": "predict",
        "fields": asked, "question": "请提供数值", "known_arguments": known,
    }, draft=draft)
    tools.catalog = lambda: [{"tool_name": "predict", "schema": {"properties": {
        "value": {"type": "number"}, "unit": {"type": "string"},
    }}}]
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.status == ("WAITING_FOR_USER" if allowed else "TERMINATED")
    assert result.error_code == (None if allowed else "CLARIFICATION_CONTRADICTS_RESOLVER")
    assert result.draft == draft
    assert not tools.resolved and not tools.executed
    assert [call.role for call in result.calls] == ["agent_decision"]


def test_duplicate_success_terminates_without_second_dispatch():
    run, _, tools, runtime = setup(lambda *_: {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 1}})
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "DUPLICATE_TOOL_CALL"
    assert result.duplicate_of_invocation_run_id == result.observations[0].invocation_run_id
    assert len(tools.executed) == 1
    assert result.observations[0].status == "SUCCEEDED"


def test_regeneration_cannot_enter_resolver_or_executor():
    def respond(role, payload):
        assert payload["tools"] == []
        assert payload["tool_execution_disabled"]
        return {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 1}}
    run, _, tools, runtime = setup(respond, tool_execution_disabled=True, retry_type="ANSWER_REGENERATION")
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "TOOL_EXECUTION_DISABLED"
    assert tools.executed == tools.resolved == []


def test_confirmation_is_business_pause_and_duplicate_confirm_does_not_execute():
    def respond(role, payload):
        return {"type": "Finish", "answer": "done"} if payload["observations"] else {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 1}}
    run, _, tools, runtime = setup(respond)
    tools.require_confirmation = True
    waiting = runtime.advance(run.agent_run_id, "actor")
    assert waiting.status == "WAITING_FOR_CONFIRMATION"
    assert not tools.executed
    result = runtime.advance(run.agent_run_id, "actor", waiting_version=waiting.waiting_version, confirmation=True)
    assert result.status == "SUCCEEDED", result.error_code
    runtime.advance(run.agent_run_id, "actor", waiting_version=waiting.waiting_version, confirmation=True)
    assert len(tools.executed) == 1


def test_step_limit_does_not_call_provider_again():
    def respond(role, payload):
        return {"type": "CallTool", "tool_name": "predict", "arguments": {"value": len(payload["actions"])}}
    run, _, tools, runtime = setup(respond, budget=RunBudget(max_action_steps=2))
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "AGENT_STEP_BUDGET_EXCEEDED"
    assert len(result.calls) == len(tools.executed) == 2


def test_usage_reasoning_not_double_counted_and_missing_not_zero():
    request = PreparedAgentCall("agent_decision", [], 20, 30, 60)
    raw = SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
        "output_token_details": {"reasoning": 8}})
    actual = normalize_usage(raw, request)
    assert actual.total_tokens == 30 and actual.reasoning_tokens == 8 and actual.source == "actual"
    estimated = normalize_usage(SimpleNamespace(), request)
    assert estimated.total_tokens == 50 and estimated.source == "estimated"


@pytest.mark.parametrize("outcome", ["reject", "expire", "changed"])
def test_confirmation_rejection_expiry_and_changed_arguments_never_dispatch(outcome):
    from datetime import timedelta
    from materialsagent.domain.models.agent import now
    run, store, tools, runtime = setup(lambda *_: {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 1}})
    tools.require_confirmation = True
    waiting = runtime.advance(run.agent_run_id, "actor")
    if outcome == "expire":
        store.run.pending_execution.confirmation_expires_at = now() - timedelta(seconds=1)
    elif outcome == "changed":
        store.run.pending_execution.arguments["value"] = 2
    result = runtime.advance(run.agent_run_id, "actor", waiting_version=waiting.waiting_version, confirmation=outcome != "reject")
    assert result.error_code == {"reject": "CONFIRMATION_REJECTED", "expire": "CONFIRMATION_EXPIRED", "changed": "CONFIRMATION_INVALIDATED"}[outcome]
    assert not tools.executed


def test_tool_time_counts_towards_active_budget_without_losing_observation():
    ticks = [0.0]
    run, store, tools, runtime = setup(lambda *_: {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 1}}, budget=RunBudget(max_active_seconds=3))
    runtime.monotonic = lambda: ticks[0]
    execute = tools.execute
    def slow(*args):
        assert args[-1] == 3
        ticks[0] += 4
        return execute(*args)
    tools.execute = slow
    result = runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "AGENT_ACTIVE_TIME_EXCEEDED"
    assert result.observations[0].status == "SUCCEEDED"
    assert len(result.calls) == len(tools.executed) == 1


def test_waiting_time_does_not_consume_active_budget():
    ticks = [0.0]
    run, store, tools, runtime = setup(lambda role, payload: {"type": "Finish", "answer": "done"} if payload["user_inputs"] else {"type": "AskUser", "reason": "INTENT_CLARIFICATION", "question": "目标？"}, budget=RunBudget(max_active_seconds=3))
    runtime.monotonic = lambda: ticks[0]
    waiting = runtime.advance(run.agent_run_id, "actor")
    ticks[0] += 10000
    result = runtime.advance(run.agent_run_id, "actor", waiting_version=waiting.waiting_version, user_input="说明即可")
    assert result.status == "SUCCEEDED"
    assert result.active_seconds == 0
