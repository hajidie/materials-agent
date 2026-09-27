import pytest
from backend.tests.agent_state import agent_run
from materialsagent.application.agent_runtime import AgentRuntime
from materialsagent.domain.models.agent import ArgumentDraft, Observation, RunBudget, identifier
from materialsagent.domain.ports.agent import AgentConflictError
from materialsagent.infrastructure.llm.agent_model import MockAgentModel

pytestmark = pytest.mark.anyio

@pytest.fixture
def anyio_backend():
    return "asyncio"

class Store:
    def __init__(self, run): self.run = run.model_copy(deep=True)
    def get(self, run_id, actor_id): return self.run.model_copy(deep=True)
    def save(self, run):
        if run.version != self.run.version: raise AgentConflictError()
        run.validate_transition(self.run)
        if run.pending_message:
            value = run.pending_message
            run.messages.append({"message_id": value["message_id"], "text": value["text"], "role": "ASSISTANT", "attachments": []})
            run.pending_message = None
        run.version += 1
        self.run = run.model_copy(deep=True)
    def receipt(self, run_id, actor_id, record, observation):
        run = self.get(run_id, actor_id)
        record.status, record.observation_id = observation.status, observation.observation_id
        run.observations.append(observation)
        run.executions.append(record.model_copy(deep=True))
        run.pending_execution, run.draft = None, None
        next(s for s in run.steps if s.step_id == record.action_id).status = "COMPLETED"
        self.save(run)
        return self.get(run_id, actor_id)

class Tools:
    def __init__(self, store):
        self.store, self.executed, self.resolved = store, [], []
        self.require_confirmation = False
    def catalog(self):
        return [{"tool_name": name, "schema": {"properties": {"value": {"type": "number"}}}, "execution_profile": "STANDARD"} for name in ["predict", "convert"]]
    def resolve(self, run, name, arguments):
        self.resolved.append(arguments)
        return ArgumentDraft(tool_name=name, version="1", schema_hash="a"*64, arguments=arguments, normalized=arguments,
            issues={} if isinstance(arguments.get("value"), (int,float)) else {"value":"Missing"}, resolver_authoritative=True)
    def prepare(self, run, record):
        record.invocation_run_id = identifier()
        if self.require_confirmation: record.status = "PENDING_CONFIRMATION"
        return record
    def execute(self, run, record, timeout):
        assert self.store.run.pending_execution.dispatched
        self.executed.append(record)
        return Observation(step_id=record.action_id, kind="TOOL_RESULT", status="SUCCEEDED", tool_name=record.tool_name,
            invocation_run_id=record.invocation_run_id, data={"value":record.arguments["value"]*2})
    def confirm(self, run, record, approved): pass
    def repair(self, run, record): return None

def setup(responder, **kwargs):
    run = agent_run(conversation_id="conversation", actor_id="actor", source_message_id="message", goal="test", **kwargs)
    store = Store(run); tools = Tools(store)
    return run, store, tools, AgentRuntime(store, MockAgentModel(responder), tools)

def answer(run): return next(m["text"] for m in run.messages if m["message_id"] == run.final_message_id)

def resume(store, text):
    run = store.run
    run.status = "PENDING"; run.version += 1
    identity = identifier(); run.user_message_ids.append(identity)
    run.messages.append({"message_id":identity,"text":text,"role":"USER","attachments":[]})

async def test_multi_tool_observation_loop_publishes_without_finalizer():
    roles=[]
    def respond(role,payload):
        roles.append(role)
        results=payload["observations"]
        if not results: return {"type":"CallTool","tool_name":"predict","arguments":{"value":2}}
        if len(results)==1: return {"type":"CallTool","tool_name":"convert","arguments":{"value":4}}
        return {"type":"Finish","answer":"8"}
    run,store,tools,runtime=setup(respond)
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.status=="SUCCEEDED" and answer(result)=="8"
    assert roles==["agent_decision"]*3 and len(tools.executed)==2
    assert (await runtime.advance(run.agent_run_id,"actor")).final_message_id==result.final_message_id

async def test_question_is_an_ordinary_message_and_resume_uses_main_model():
    roles=[]
    def respond(role,payload):
        roles.append(role)
        if payload["observations"]: return {"type":"Finish","answer":"完成"}
        if payload["user_inputs"]: return {"type":"CallTool","tool_name":"predict","arguments":{"value":3}}
        return {"type":"AskUser","question":"数值？"}
    run,store,tools,runtime=setup(respond)
    waiting=await runtime.advance(run.agent_run_id,"actor")
    assert waiting.status=="WAITING_FOR_USER" and waiting.waiting.question=="数值？"
    assert not tools.resolved
    resume(store,"3")
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.status=="SUCCEEDED" and result.agent_run_id==waiting.agent_run_id
    assert roles==["agent_decision"]*3 and result.llm_tokens>waiting.llm_tokens

async def test_validation_issues_return_to_loop_before_any_execution():
    def respond(role,payload):
        if payload["draft"]: return {"type":"AskUser","question":"数值？"}
        return {"type":"CallTool","tool_name":"predict","arguments":{}}
    run,store,tools,runtime=setup(respond)
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.status=="WAITING_FOR_USER" and result.draft.issues=={"value":"Missing"}
    assert len(result.calls)==2 and not tools.executed

@pytest.mark.parametrize("proposal", [
    {"type":"Finish"}, {"type":"Finish","answer":""},
    {"type":"Finish","answer":"ok","needs_synthesis":True},
    {"type":"AskUser","question":"?","reason":"INTENT_CLARIFICATION"},
])
async def test_retired_or_incomplete_proposals_are_rejected(proposal):
    run,store,tools,runtime=setup(lambda *_:proposal)
    assert (await runtime.advance(run.agent_run_id,"actor")).status=="TERMINATED"
    assert not tools.executed

async def test_duplicate_success_never_dispatches_again():
    run,store,tools,runtime=setup(lambda *_:{"type":"CallTool","tool_name":"predict","arguments":{"value":1}})
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.error_code=="DUPLICATE_TOOL_CALL" and len(tools.executed)==1

async def test_regeneration_has_empty_tools_and_an_execution_gate():
    def respond(role,payload):
        assert payload["tools"]==[]
        return {"type":"CallTool","tool_name":"predict","arguments":{"value":1}}
    run,store,tools,runtime=setup(respond,tool_execution_disabled=True)
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.error_code=="TOOL_EXECUTION_DISABLED" and not tools.resolved and not tools.executed

async def test_step_limit_does_not_call_model_again():
    run,store,tools,runtime=setup(lambda *_:{"type":"CallTool","tool_name":"predict","arguments":{"value":1}},budget=RunBudget(max_action_steps=1))
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.error_code=="AGENT_STEP_BUDGET_EXCEEDED" and len(result.calls)==1

async def test_tool_time_is_budgeted_and_committed_observation_is_retained():
    ticks=[0.0]
    run,store,tools,runtime=setup(lambda *_:{"type":"CallTool","tool_name":"predict","arguments":{"value":1}},budget=RunBudget(max_active_seconds=3))
    runtime.monotonic=lambda:ticks[0]
    original=tools.execute
    def slow(*args): ticks[0]+=4; return original(*args)
    tools.execute=slow
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.error_code=="AGENT_ACTIVE_TIME_EXCEEDED" and result.observations[0].status=="SUCCEEDED"

async def test_waiting_time_does_not_consume_active_budget():
    run,store,tools,runtime=setup(lambda role,p:{"type":"Finish","answer":"done"} if p["user_inputs"] else {"type":"AskUser","question":"目标？"})
    ticks=[0.0];runtime.monotonic=lambda:ticks[0]
    await runtime.advance(run.agent_run_id,"actor")
    ticks[0]=10000;resume(store,"说明即可")
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.status=="SUCCEEDED" and result.active_seconds==0

async def test_untrusted_source_cannot_be_published():
    run,store,tools,runtime=setup(lambda *_:{"type":"Finish","answer":"ok","sources":["not-a-result"]})
    result=await runtime.advance(run.agent_run_id,"actor")
    assert result.error_code=="FINAL_ANSWER_SOURCE_MISMATCH" and not result.final_message_id


async def test_malformed_proposal_returns_to_main_model_without_reexecuting_result():
    seen = []
    def respond(role, payload):
        seen.append(payload)
        if not payload["observations"]:
            return {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 2}}
        if not payload.get("proposal_error"):
            return {"type": "json_object"}
        return {"type": "Finish", "answer": "结果为4"}
    run, store, tools, runtime = setup(respond)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and answer(result) == "结果为4"
    assert len(tools.executed) == 1 and len(result.calls) == 3
    assert [step.status for step in result.steps] == ["COMPLETED", "FAILED", "COMPLETED"]
    assert result.llm_tokens == sum(call.usage.total_tokens for call in result.calls)
    assert seen[-1]["proposal_error"] and len(seen[-1]["observations"]) == 1


async def test_repeated_json_failure_is_bounded_and_metered():
    from materialsagent.domain.ports.agent import AgentModelResponse
    run, store, tools, runtime = setup(lambda *_: None)
    class InvalidModel(MockAgentModel):
        async def ainvoke(self, request):
            response = await super().ainvoke(request)
            return AgentModelResponse(None, response.usage, "LLM_INVALID_JSON")
    runtime.model = InvalidModel(lambda *_: None)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.error_code == "LLM_INVALID_JSON" and len(result.calls) == 2
    assert not tools.executed and result.llm_tokens == sum(call.usage.total_tokens for call in result.calls)


async def test_duplicate_proposal_uses_existing_observation_to_finish_without_dispatch():
    def respond(role, payload):
        if payload.get("proposal_error"):
            assert payload["observations"][0]["data"]["value"] == 4
            return {"type": "Finish", "answer": "已算得4，无需重复计算"}
        return {"type": "CallTool", "tool_name": "predict", "arguments": {"value": 2}}
    run, store, tools, runtime = setup(respond)
    result = await runtime.advance(run.agent_run_id, "actor")
    assert result.status == "SUCCEEDED" and len(tools.executed) == 1
    assert len(result.calls) == 3 and len(result.observations) == 1
    assert result.llm_tokens == sum(call.usage.total_tokens for call in result.calls)
