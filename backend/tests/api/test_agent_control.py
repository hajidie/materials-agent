from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
import pytest
from sqlalchemy import select, func, text
from backend.tests.api.test_message_orchestration import client_and_conversation
from backend.tests.agent_fakes import _Runtime, _MemoryStorage
from materialsagent.infrastructure.llm.agent_model import MockAgentModel
from materialsagent.infrastructure.db.agent import AgentRunRow, AgentObservationRow, FinalAnswerRow
from materialsagent.domain.ports.agent import AgentConflictError
from materialsagent.application.tools import build_tool_registry

ARGUMENTS = {"material":"ZTA35G","solution_temperature":{"value":1000,"unit":"°C"},"solution_time":{"value":2,"unit":"h"},"aging_temperature":{"value":730,"unit":"°C"},"aging_time":{"value":2,"unit":"h"},"requested_outputs":["sem_image","mechanical_properties"]}
def post(client, conversation, key="new", **body):
    return client.post(f"/api/v1/conversations/{conversation}/messages", json={"mode":"NEW_RUN","content_text":"1000 MPa 转 GPa",**body},headers={"Idempotency-Key":key})

def test_committed_answer_survives_lost_http_response(api_harness, monkeypatch):
    client, conversation = client_and_conversation(api_harness)
    with client:
        import materialsagent.api.routes.agent_runs as routes
        original = routes.public
        monkeypatch.setattr(routes,"public",lambda run: (_ for _ in ()).throw(ConnectionError()) if run.final_answer else original(run))
        assert post(client,conversation).status_code == 500
        saved = client.app.state.agent_runtime.store.list(conversation,"agent-test")[0]
        assert saved.status == "SUCCEEDED"
        monkeypatch.setattr(routes,"public",original)
        replay = post(client,conversation).json()["data"]
        assert replay["idempotency_replayed"] and replay["agent_run"]["final_answer"]["answer_id"] == saved.final_answer.answer_id
        assert len(replay["agent_run"]["calls"]) == len(saved.calls)

def test_cas_competition_and_long_tool_hold_no_transaction(api_harness, monkeypatch):
    client, conversation = client_and_conversation(api_harness)
    entered, release = Event(), Event(); executions=[]
    with client:
        gateway = client.app.state.agent_runtime.tools; original = gateway.execute
        def blocked(*args):
            executions.append(1); entered.set(); assert release.wait(15); return original(*args)
        monkeypatch.setattr(gateway,"execute",blocked)
        with ThreadPoolExecutor(1) as executor:
            future=executor.submit(post,client,conversation)
            try:
                assert entered.wait(10)
                replay=post(client,conversation).json()["data"]["agent_run"]
                assert replay["status"] == "RUNNING" and replay["pending_execution"]["dispatched"]
                with api_harness.engine.connect() as connection:
                    assert connection.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction'")) == 0
            finally: release.set()
            assert future.result().json()["data"]["agent_run"]["status"] == "SUCCEEDED"
        assert executions == [1]

def test_post_result_failure_repairs_observation_without_tool_reexecution(api_harness, monkeypatch):
    client, conversation = client_and_conversation(api_harness)
    with client:
        gateway=client.app.state.agent_runtime.tools; original=gateway.execute; executions=[]
        def lost_result(*args):
            executions.append(1); original(*args); raise ConnectionError("lost return")
        monkeypatch.setattr(gateway,"execute",lost_result)
        result=post(client,conversation).json()["data"]["agent_run"]
        assert result["status"] == "SUCCEEDED" and len(result["observations"]) == 1 and executions == [1]

def test_stale_run_owner_cannot_commit_tool_result(api_harness):
    client, conversation=client_and_conversation(api_harness)
    with client:
        runtime=client.app.state.agent_runtime; run,_=runtime.store.submit(conversation,"agent-test","goal","claim")
        one=runtime.store.get(run.agent_run_id,"agent-test"); two=one.model_copy(deep=True)
        one.status="RUNNING";one.claim="first";runtime.store.save(one)
        two.status="RUNNING";two.claim="second"
        with pytest.raises(AgentConflictError):runtime.store.save(two)
        runtime.store.recover_interrupted("replacement")
        one.status="TERMINATED"
        with pytest.raises(AgentConflictError):runtime.store.save(one)
        assert runtime.store.get(run.agent_run_id,"agent-test").error_code == "PROCESS_INTERRUPTED"

def test_standard_tool_argument_wait_resumes_with_only_one_delta_extraction(api_harness):
    roles=[]
    def respond(role,payload):
        roles.append(role)
        if role == "tool_arg_resolution":
            assert payload["user_input"] == "GPa";return {"to_unit":"GPa"}
        draft=payload.get("draft")
        if draft:
            if draft["issues"]:return {"type":"AskUser","reason":"TOOL_ARGUMENT_CLARIFICATION","tool_name":"materials_unit_conversion","fields":["to_unit"],"question":"目标单位？"}
            return {"type":"CallTool","tool_name":"materials_unit_conversion","arguments":{}}
        if payload["observations"]:return {"type":"Finish","answer":"1 GPa"}
        return {"type":"CallTool","tool_name":"materials_unit_conversion","arguments":{"value":1000,"from_unit":"MPa"}}
    client,conversation=client_and_conversation(api_harness,MockAgentModel(respond))
    with client:
        first=post(client,conversation).json()["data"]["agent_run"]
        assert first["status"] == "WAITING_FOR_USER" and first["tool_executions"] == 0 and "tool_arg_resolution" not in roles
        second=post(client,conversation,"resume",mode="RESUME_RUN",content_text="GPa",agent_run_id=first["agent_run_id"],waiting_version=first["waiting_version"]).json()["data"]["agent_run"]
        assert second["status"] == "SUCCEEDED" and second["llm_tokens"] > first["llm_tokens"] and roles.count("tool_arg_resolution") == 1

def test_final_answer_failure_leaves_managed_result_and_assets_intact(api_harness):
    api_harness.persist_actor("managed-control");conversation=api_harness.persist_conversation("managed-control")
    tool=_Runtime()
    def respond(role,payload):
        if role == "final_answer":raise RuntimeError("model unavailable")
        if payload["observations"]:return {"type":"Finish","needs_synthesis":True}
        return {"type":"CallTool","tool_name":"zta35g_sem_virtual_lab","arguments":ARGUMENTS}
    with api_harness.create_client("managed-control",agent_model=MockAgentModel(respond),tool_registry=build_tool_registry(tool),storage_service=_MemoryStorage()) as client:
        run=post(client,conversation.conversation_id).json()["data"]["agent_run"]
        assert run["status"] == "TERMINATED" and run["error_code"] == "LLM_CALL_FAILED"
        observation=run["observations"][0]
        assert observation["status"] == "SUCCEEDED" and observation["artifacts"]
        assert client.get(f"/api/v1/tool-results/{observation['result_id']}").status_code == 200
        assert client.get(observation["artifacts"][0]["content_url"]).status_code == 200
        assert tool.calls == 1

def test_regeneration_protocol_error_cannot_enter_executor(api_harness,monkeypatch):
    client,conversation=client_and_conversation(api_harness)
    with client:
        first=post(client,conversation).json()["data"]["agent_run"]
        runtime=client.app.state.agent_runtime
        def malicious(role,payload):
            assert payload["tools"] == [] and payload["tool_execution_disabled"]
            return {"type":"CallTool","tool_name":"materials_unit_conversion","arguments":{}}
        runtime.model=MockAgentModel(malicious)
        monkeypatch.setattr(runtime.tools,"execute",lambda *args:pytest.fail("Executor reached"))
        run=client.post(f"/api/v1/agent-runs/{first['agent_run_id']}/retry",json={"retry_type":"ANSWER_REGENERATION"},headers={"Idempotency-Key":"restricted"}).json()["data"]["agent_run"]
        assert run["status"] == "TERMINATED" and run["tool_executions"] == 0 and run["error_code"] == "TOOL_EXECUTION_DISABLED"


def test_failed_managed_retry_creates_new_run_invocation_attempt_and_seed(api_harness):
    from materialsagent.domain.ports.tool_execution import ToolClientRuntimeError
    from materialsagent.infrastructure.db.tool_run import ToolRunRow
    api_harness.persist_actor("retry-control"); conversation=api_harness.persist_conversation("retry-control")
    class BusyOnce(_Runtime):
        def execute(self, *args):
            if self.calls == 0:
                self.calls += 1
                raise ToolClientRuntimeError(code="RUNTIME_BUSY", safe_message="busy", retryable=True)
            return super().execute(*args)
    tool=BusyOnce()
    def respond(role,payload):
        if payload.get("observations"): return {"type":"Finish","answer":"完成"}
        return {"type":"CallTool","tool_name":"zta35g_sem_virtual_lab","arguments":ARGUMENTS}
    with api_harness.create_client("retry-control",agent_model=MockAgentModel(respond),tool_registry=build_tool_registry(tool),storage_service=_MemoryStorage()) as client:
        first=post(client,conversation.conversation_id).json()["data"]["agent_run"]
        assert first["status"] == "TERMINATED" and first["executions"][0]["retryable"], (first["observations"],first["pending_execution"])
        retry=client.post(f"/api/v1/agent-runs/{first['agent_run_id']}/retry",json={"retry_type":"TOOL_RETRY","invocation_run_id":first["executions"][0]["invocation_run_id"]},headers={"Idempotency-Key":"retry"})
        assert retry.status_code == 200, retry.text
        second=retry.json()["data"]["agent_run"]
        assert second["status"] == "SUCCEEDED",(second["error_code"],second["pending_execution"],second["observations"])
        assert second["source_agent_run_id"] == first["agent_run_id"] and tool.calls == 2
        assert second["executions"][0]["retry_of_invocation_run_id"] == first["executions"][0]["invocation_run_id"]
        with api_harness.engine.connect() as connection:
            runs=connection.execute(select(ToolRunRow.attempt_no,ToolRunRow.execution_input).order_by(ToolRunRow.attempt_no)).all()
        assert [r[0] for r in runs] == [1,2]
        assert runs[0][1]["runtime_parameters"]["seed"] != runs[1][1]["runtime_parameters"]["seed"]


@pytest.mark.parametrize("approved", [True, False])
def test_invocation_confirmation_api_reauthorizes_and_is_idempotent(api_harness, approved):
    from materialsagent.application.fake_side_effect_tool import FakeSideEffectSink
    sink = FakeSideEffectSink()
    api_harness.persist_actor("confirmation")
    conversation = api_harness.persist_conversation("confirmation")
    def respond(role, payload):
        return {"type":"Finish", "answer":"已处理"} if payload["observations"] else {"type":"CallTool", "tool_name":"dev_fake_side_effect", "arguments":{"message":"test-only receipt"}}
    with api_harness.create_client("confirmation", settings=api_harness.settings.model_copy(update={"enable_dev_fake_side_effect_tool":True}),
            agent_model=MockAgentModel(respond), tool_registry=build_tool_registry(enable_dev_fake_side_effect_tool=True, fake_side_effect_sink=sink)) as client:
        first = post(client, conversation.conversation_id).json()["data"]["agent_run"]
        assert first["status"] == "WAITING_FOR_CONFIRMATION", first["error_code"]
        pending = first["pending_execution"]
        url = f"/api/v1/agent-runs/{first['agent_run_id']}/invocations/{pending['invocation_run_id']}/" + ("confirm" if approved else "reject")
        body = {"waiting_version": first["waiting_version"], "confirmation_version":pending["confirmation_version"]}
        assert client.post(url, json={**body, "confirmation_version":"stale"}).status_code == 409
        result = client.post(url, json=body).json()["data"]
        assert result["status"] == ("SUCCEEDED" if approved else "TERMINATED"), result["error_code"]
        assert sink.write_count == int(approved)
        replay = client.post(url, json=body)
        assert replay.status_code == 200 and sink.write_count == int(approved)


def test_only_one_input_is_accepted_per_waiting_version(api_harness):
    model = MockAgentModel(lambda *_: {"type":"AskUser", "reason":"INTENT_CLARIFICATION", "question":"目标？"})
    client, conversation = client_and_conversation(api_harness, model)
    with client:
        waiting = post(client, conversation).json()["data"]["agent_run"]
        store = client.app.state.agent_runtime.store
        args = {"run_id": waiting["agent_run_id"], "waiting_version": waiting["waiting_version"]}
        accepted, replayed = store.submit(conversation, "agent-test", "one", "first-answer", **args)
        assert not replayed
        with pytest.raises(AgentConflictError):
            store.submit(conversation, "agent-test", "different", "second-answer", **args)
        replay, replayed = store.submit(conversation, "agent-test", "one", "first-answer", **args)
        assert replayed and replay.version == accepted.version
        from materialsagent.infrastructure.db.conversation_task import MessageRow
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(MessageRow)) == 2



def test_managed_proactive_wait_resumes_and_does_not_duplicate_ready_revision(api_harness):
    import json
    api_harness.persist_actor("managed-resume")
    conversation = api_harness.persist_conversation("managed-resume")
    runtime = _Runtime()
    with api_harness.create_client("managed-resume", agent_model=MockAgentModel(), tool_registry=build_tool_registry(runtime), storage_service=_MemoryStorage()) as client:
        waiting = post(client, conversation.conversation_id, content_text="预测 ZTA35G 性能").json()["data"]["agent_run"]
        assert waiting["status"] == "WAITING_FOR_USER" and waiting["steps"][0]["action"]["type"] == "AskUser"
        assert waiting["tool_executions"] == 0
        result = post(client, conversation.conversation_id, "managed-answer", mode="RESUME_RUN", content_text=json.dumps(ARGUMENTS), agent_run_id=waiting["agent_run_id"], waiting_version=waiting["waiting_version"]).json()["data"]["agent_run"]
        assert result["status"] == "SUCCEEDED", (result["error_code"], result["observations"])
        assert result["agent_run_id"] == waiting["agent_run_id"] and result["tool_executions"] == 1
        assert len([c for c in result["calls"] if c["role"] == "tool_arg_resolution"]) == 1
        from materialsagent.infrastructure.db.conversation_task import TaskInputRevisionRow
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(TaskInputRevisionRow)) == 2


def test_managed_resolver_question_can_restate_known_arguments_and_resume(api_harness):
    from materialsagent.infrastructure.db.conversation_task import TaskInputRevisionRow

    actor = "managed-known-restatement"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor)
    known = {key: value for key, value in ARGUMENTS.items() if key not in {"aging_temperature", "aging_time"}}
    known["requested_outputs"] = ["mechanical_properties"]
    missing = {"aging_temperature": None, "aging_time": None}
    delta = {key: ARGUMENTS[key] for key in missing}
    tool = _Runtime()

    def respond(role, payload):
        if role == "tool_arg_resolution":
            assert payload["user_input"] == "时效温度 730℃，时效时间 2 小时"
            return delta
        if any(o["kind"] == "TOOL_RESULT" for o in payload["observations"]):
            return {"type": "Finish", "answer": "预测完成"}
        if payload["draft"] and payload["draft"]["issues"]:
            return {"type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION",
                "tool_name": "zta35g_sem_virtual_lab", "fields": list(missing),
                "question": "请提供时效温度和时效时间，已收到固溶 1000℃ / 2 小时。",
                "known_arguments": known}
        return {"type": "CallTool", "tool_name": "zta35g_sem_virtual_lab",
            "arguments": {} if payload["draft"] else {**known, **missing}}

    # This test exercises four decisions and one incremental extraction; token limits
    # have separate boundary tests, so allow the offline byte-based estimator room.
    settings = api_harness.settings.model_copy(update={"agent_max_llm_tokens": 64000})
    with api_harness.create_client(actor, settings=settings, agent_model=MockAgentModel(respond),
            tool_registry=build_tool_registry(tool), storage_service=_MemoryStorage()) as client:
        goal = "对 ZTA35G 进行虚拟实验：固溶温度 1000℃，固溶时间 2 小时。预测力学性能。"
        waiting = post(client, conversation.conversation_id, content_text=goal).json()["data"]["agent_run"]
        assert waiting["status"] == "WAITING_FOR_USER", waiting["error_code"]
        assert [s["action"]["type"] for s in waiting["steps"]] == ["CallTool", "AskUser"]
        assert waiting["draft"]["issues"] == {key: "Missing" for key in missing}
        assert waiting["tool_executions"] == tool.calls == 0
        assert [c["role"] for c in waiting["calls"]] == ["agent_decision"] * 2
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(TaskInputRevisionRow)) == 1
        assert client.get(f"/api/v1/agent-runs/{waiting['agent_run_id']}").json()["data"]["waiting"] == waiting["waiting"]

        resumed = post(client, conversation.conversation_id, "supply-aging", mode="RESUME_RUN",
            content_text="时效温度 730℃，时效时间 2 小时", agent_run_id=waiting["agent_run_id"],
            waiting_version=waiting["waiting_version"]).json()["data"]["agent_run"]
        assert resumed["status"] == "SUCCEEDED", resumed["error_code"]
        assert resumed["agent_run_id"] == waiting["agent_run_id"]
        assert resumed["tool_executions"] == tool.calls == 1
        assert sum(c["role"] == "tool_arg_resolution" for c in resumed["calls"]) == 1
        assert resumed["executions"][0]["arguments"] == {**known, **delta}
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(TaskInputRevisionRow)) == 2


def test_restart_repairs_committed_result_without_redispatch_or_model(api_harness, monkeypatch):
    client, conversation = client_and_conversation(api_harness)
    with client:
        runtime = client.app.state.agent_runtime
        gateway = runtime.tools
        original = gateway.execute
        calls = []
        def process_exit(*args):
            calls.append(1)
            original(*args)
            raise SystemExit("simulated process exit before observation commit")
        monkeypatch.setattr(gateway, "execute", process_exit)
        run, _ = runtime.store.submit(conversation, "agent-test", "1000 MPa 转 GPa", "interrupted")
        with pytest.raises(SystemExit):
            runtime.advance(run.agent_run_id, "agent-test")
        assert runtime.store.get(run.agent_run_id, "agent-test").status == "RUNNING"
        runtime.store.recover_interrupted("replacement", repair=gateway.repair)
        recovered = runtime.store.get(run.agent_run_id, "agent-test")
        assert recovered.status == "TERMINATED" and recovered.error_code == "PROCESS_INTERRUPTED"
        assert recovered.observations[0].data["value"] == 1
        assert recovered.executions[0].status == "SUCCEEDED" and calls == [1]
        runtime.store.recover_interrupted("replacement", repair=gateway.repair)
        assert len(runtime.store.get(run.agent_run_id, "agent-test").observations) == 1
        response = client.post(f"/api/v1/agent-runs/{run.agent_run_id}/retry", json={"retry_type":"ANSWER_REGENERATION"}, headers={"Idempotency-Key":"answer-after-restart"})
        assert response.json()["data"]["agent_run"]["status"] == "SUCCEEDED" and calls == [1]


def test_invalid_managed_parameter_pauses_and_resumes_from_resolver_facts(api_harness):
    api_harness.persist_actor("invalid-managed")
    conversation = api_harness.persist_conversation("invalid-managed")
    bad = {**ARGUMENTS, "solution_temperature":{"value":1200,"unit":"°C"}}
    def respond(role, payload):
        if role == "tool_arg_resolution": return {"solution_temperature":{"value":1000,"unit":"°C"}}
        draft = payload.get("draft")
        if draft and draft["issues"]:
            return {"type":"AskUser", "reason":"TOOL_ARGUMENT_CLARIFICATION", "tool_name":"zta35g_sem_virtual_lab", "fields":["solution_temperature"], "question":"请修正温度"}
        if any(o["kind"] == "TOOL_RESULT" for o in payload["observations"]): return {"type":"Finish", "answer":"完成"}
        return {"type":"CallTool", "tool_name":"zta35g_sem_virtual_lab", "arguments":{} if draft else bad}
    with api_harness.create_client("invalid-managed", settings=api_harness.settings.model_copy(update={"agent_max_llm_tokens":64000}), agent_model=MockAgentModel(respond), tool_registry=build_tool_registry(_Runtime()), storage_service=_MemoryStorage()) as client:
        waiting = post(client, conversation.conversation_id).json()["data"]["agent_run"]
        assert waiting["status"] == "WAITING_FOR_USER", waiting["error_code"]
        assert waiting["draft"]["issues"] == {"solution_temperature":"Invalid"}
        resumed = post(client, conversation.conversation_id, "fixed", mode="RESUME_RUN", content_text="1000 摄氏度", agent_run_id=waiting["agent_run_id"], waiting_version=waiting["waiting_version"]).json()["data"]["agent_run"]
        assert resumed["status"] == "SUCCEEDED", resumed["error_code"]


def test_final_answer_transaction_failure_does_not_publish_success_or_message(api_harness):
    from sqlalchemy import event
    from materialsagent.infrastructure.db.conversation_task import MessageRow
    client, conversation = client_and_conversation(api_harness)
    def fail(*args): raise ValueError("simulated final answer write failure")
    with client:
        event.listen(FinalAnswerRow, "before_insert", fail)
        try:
            run = post(client, conversation).json()["data"]["agent_run"]
        finally:
            event.remove(FinalAnswerRow, "before_insert", fail)
        assert run["status"] == "TERMINATED" and run["final_answer"] is None
        assert run["observations"][0]["status"] == "SUCCEEDED"
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(FinalAnswerRow)) == 0
            assert connection.scalar(select(func.count()).select_from(MessageRow).where(MessageRow.role == "ASSISTANT")) == 0


def test_restart_between_managed_result_and_invocation_repairs_both(api_harness, monkeypatch):
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow
    api_harness.persist_actor("managed-gap")
    conversation = api_harness.persist_conversation("managed-gap")
    tool, storage = _Runtime(), _MemoryStorage()
    with api_harness.create_client("managed-gap", agent_model=MockAgentModel(lambda *_: {"type":"CallTool", "tool_name":"zta35g_sem_virtual_lab", "arguments":ARGUMENTS}), tool_registry=build_tool_registry(tool), storage_service=storage) as client:
        runtime = client.app.state.agent_runtime
        original = runtime.tools.workflow.execute
        def process_exit(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit("after committed managed result")
        monkeypatch.setattr(runtime.tools.workflow, "execute", process_exit)
        run, _ = runtime.store.submit(conversation.conversation_id, "managed-gap", "ZTA35G", "gap")
        with pytest.raises(SystemExit): runtime.advance(run.agent_run_id, "managed-gap")
        before = dict(storage.objects)
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(InvocationRunRow.status)) == "RUNNING"
        runtime.store.recover_interrupted("replacement", repair=runtime.tools.repair)
        recovered = runtime.store.get(run.agent_run_id, "managed-gap")
        assert recovered.observations[0].status == "SUCCEEDED" and recovered.status == "TERMINATED"
        assert tool.calls == 1 and storage.objects == before
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(InvocationRunRow.status)) == "SUCCEEDED"


def test_failed_managed_result_marks_invocation_failed(api_harness):
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow
    api_harness.persist_actor("failed-result")
    conversation = api_harness.persist_conversation("failed-result")
    arguments = {**ARGUMENTS, "requested_outputs":["mechanical_properties"]}
    with api_harness.create_client("failed-result", agent_model=MockAgentModel(lambda *_: {"type":"CallTool", "tool_name":"zta35g_sem_virtual_lab", "arguments":arguments}), tool_registry=build_tool_registry(_Runtime(failed=True)), storage_service=_MemoryStorage()) as client:
        run = post(client, conversation.conversation_id).json()["data"]["agent_run"]
        assert run["status"] == "TERMINATED" and run["observations"][0]["status"] == "FAILED"
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(InvocationRunRow.status)) == "FAILED"


def test_lost_managed_workflow_return_keeps_committed_invocation_success(api_harness, monkeypatch):
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow
    api_harness.persist_actor("managed-return")
    conversation = api_harness.persist_conversation("managed-return")
    model = MockAgentModel(lambda role, payload: {"type":"Finish", "answer":"完成"} if payload["observations"] else {"type":"CallTool", "tool_name":"zta35g_sem_virtual_lab", "arguments":ARGUMENTS})
    tool = _Runtime()
    with api_harness.create_client("managed-return", agent_model=model, tool_registry=build_tool_registry(tool), storage_service=_MemoryStorage()) as client:
        original = client.app.state.agent_runtime.tools.workflow.execute
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise ConnectionError("lost workflow return")
        monkeypatch.setattr(client.app.state.agent_runtime.tools.workflow, "execute", lost)
        run = post(client, conversation.conversation_id).json()["data"]["agent_run"]
        assert run["status"] == "SUCCEEDED" and tool.calls == 1
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(InvocationRunRow.status)) == "SUCCEEDED"
