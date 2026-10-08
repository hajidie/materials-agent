import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from backend.tests.agent_state import wait_run

from sqlalchemy import select, func
from materialsagent.infrastructure.db.agent import AgentRunRow
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.llm.agent_model import MockAgentModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import AIMessage


class _AsyncWaitingProvider(BaseChatModel):
    entered: Event
    cancelled: Event

    @property
    def _llm_type(self):
        return "waiting-test-provider"

    def bind_tools(self, _tools, **_kwargs):
        return self

    def _generate(self, *_args, **_kwargs):
        raise AssertionError("Synchronous provider path used")

    async def _agenerate(self, *_args, **_kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="unreachable"))])


class _WaitingModel:
    def __init__(self, entered, cancelled):
        self.entered, self.cancelled = entered, cancelled

    def native_model(self, **_kwargs):
        return _AsyncWaitingProvider(entered=self.entered, cancelled=self.cancelled)


def client_for(harness, model=None):
    harness.persist_actor("turn-test")
    conversation = harness.persist_conversation("turn-test")
    return harness.create_client("turn-test", agent_model=model or MockAgentModel()), conversation.conversation_id


def accept(client, conversation, text="1000 MPa 转 GPa", key="first", **body):
    response = client.post(f"/api/v1/conversations/{conversation}/messages",
        json={"content_text": text, **body}, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["data"]


def advance(client, submitted):
    return wait_run(client, submitted['agent_run']['agent_run_id']).json()["data"]


def messages(client, conversation):
    response = client.get(f"/api/v1/conversations/{conversation}/messages")
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def test_acceptance_then_single_loop_publishes_canonical_messages(api_harness):
    client, conversation = client_for(api_harness)
    with client:
        submitted = accept(client, conversation)
        assert submitted["agent_run"]["status"] == "PENDING"
        result = advance(client, submitted)
        assert result["status"] == "SUCCEEDED", result
        history = messages(client, conversation)
        assert [m["phase"] for m in history] == ["user", "answer"]
        assert len(result["executions"]) == 1
        assert advance(client, submitted)["final_message_id"] == result["final_message_id"]
        assert accept(client, conversation)["idempotency_replayed"]
    with api_harness.engine.connect() as connection:
        document = connection.scalar(select(AgentRunRow.document))
        assert all(k not in document for k in ("goal", "user_inputs", "user_messages", "final_answer", "waiting"))
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 2


def test_question_reply_uses_same_loop_and_waiting_identity(api_harness):
    def respond(role, payload):
        assert role == "agent_decision"
        if payload["user_inputs"]:
            return {"type": "Finish", "answer": "你的研究目标是强度。"}
        return {"type": "AskUser", "question": "希望研究哪项性能？"}
    client, conversation = client_for(api_harness, MockAgentModel(respond))
    with client:
        waiting = advance(client, accept(client, conversation, "帮我研究材料"))
        assert waiting["status"] == "WAITING_FOR_USER"
        reply = {"question_message_id": waiting["question_message_id"], "waiting_version": waiting["waiting_version"]}
        submitted = accept(client, conversation, "强度", "reply", reply_to=reply)
        result = advance(client, submitted)
        assert result["agent_run_id"] == waiting["agent_run_id"]
        assert result["status"] == "SUCCEEDED"
        assert [m["phase"] for m in messages(client, conversation)] == ["user", "question", "user", "answer"]
        stale = client.post(f"/api/v1/conversations/{conversation}/messages",
            json={"content_text": "另一个回答", "reply_to": reply}, headers={"Idempotency-Key": "stale"})
        assert stale.status_code == 409


def test_knowledge_answer_regeneration_is_versioned_without_tools_or_fake_user(api_harness):
    client, conversation = client_for(api_harness, MockAgentModel(lambda *_: {"type": "Finish", "answer": "材料解释。"}))
    with client:
        first = advance(client, accept(client, conversation, "解释材料"))
        accepted = client.post(f"/api/v1/messages/{first['final_message_id']}/regenerate",
            headers={"Idempotency-Key": "regen"}, json={})
        assert accepted.status_code == 202, accepted.text
        result = advance(client, accepted.json()["data"])
        assert result["status"] == "SUCCEEDED" and result["executions"] == []
        history = messages(client, conversation)
        assert len(history) == 2
        assert history[-1]["version_count"] == 2
        versions = client.get(f"/api/v1/messages/{first['final_message_id']}/versions").json()["data"]["items"]
        assert [m["answer_version"] for m in versions] == [2, 1]


def test_stop_cancels_the_actual_model_await_and_preserves_user_message(api_harness):
    entered, cancelled = Event(), Event()
    client, conversation = client_for(api_harness, _WaitingModel(entered, cancelled))
    with client:
        submitted = accept(client, conversation, "解释材料")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(advance, client, submitted)
            assert entered.wait(10)
            endpoint = f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop"
            stopped = client.post(endpoint, json={"submission_id": submitted["submission_id"]})
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["outcome"] == "stopped"
            assert cancelled.wait(1)
            assert future.result(timeout=5)["status"] == "TERMINATED"
            assert client.post(endpoint, json={"submission_id": submitted["submission_id"]}).status_code == 200
        assert len(messages(client, conversation)) == 1


def test_stop_before_background_claim_prevents_any_model_or_tool_call(api_harness, monkeypatch):
    client, conversation = client_for(api_harness, MockAgentModel(lambda *_: (_ for _ in ()).throw(AssertionError("model called"))))
    with client:
        monkeypatch.setattr(client.app.state.agent_runtime, "schedule", lambda run: None)
        submitted = accept(client, conversation)
        response = client.post(f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop",
            json={"submission_id": submitted["submission_id"]})
        assert response.status_code == 200
        assert advance(client, submitted)["stopped"]


def test_stopped_dispatched_tool_publishes_receipt_without_resuming_loop(api_harness, monkeypatch):
    client, conversation = client_for(api_harness)
    entered, release = Event(), Event()
    with client:
        runtime = client.app.state.agent_runtime
        original = runtime.tools.execute
        def blocked(*args):
            entered.set()
            assert release.wait(15)
            return original(*args)
        monkeypatch.setattr(runtime.tools, "execute", blocked)
        submitted = accept(client, conversation)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(advance, client, submitted)
            try:
                assert entered.wait(10)
                stopped = client.post(f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop",
                    json={"submission_id": submitted["submission_id"]})
                assert stopped.status_code == 200
            finally:
                release.set()
            result = future.result(timeout=10)
        assert result["stopped"]
        assert len(result["executions"]) == 1
        assert result["executions"][0]["status"] == "SUCCEEDED"
        assert result["final_message_id"] is None
        assert messages(client, conversation)[-1]["phase"] == "notification"


def test_replayed_older_acceptance_cannot_stop_new_reply(api_harness):
    client,conversation=client_for(api_harness,MockAgentModel(lambda *_:{"type":"AskUser","question":"性能目标？"}))
    with client:
        original=accept(client,conversation,"研究材料")
        waiting=advance(client,original)
        reply=accept(client,conversation,"强度",key="reply",reply_to={"question_message_id":waiting["question_message_id"],"waiting_version":waiting["waiting_version"]})
        replay=accept(client,conversation,"研究材料")
        assert replay["submission_id"]==original["submission_id"]!=reply["submission_id"]
        url=f"/api/v1/agent-runs/{waiting['agent_run_id']}"
        assert client.post(url+"/stop",json={"submission_id":replay["submission_id"]}).status_code==409
        assert client.post(url+"/resume",json={"submission_id":replay["submission_id"],"version":0}).status_code==409
        current = wait_run(client, waiting['agent_run_id']).json()["data"]
        assert current["status"] == "WAITING_FOR_USER" and not current["stopped"]


def test_old_message_modes_and_blank_finish_do_not_publish(api_harness):
    client,conversation=client_for(api_harness,MockAgentModel(lambda *_:{"type":"Finish","answer":"  "}))
    with client:
        assert client.post(f"/api/v1/conversations/{conversation}/messages",json={"mode":"NEW_RUN","content_text":"材料"},headers={"Idempotency-Key":"old"}).status_code==422
        result=advance(client,accept(client,conversation,"材料"))
        assert result["status"]=="TERMINATED" and result["final_message_id"] is None
        assert [m["phase"] for m in messages(client,conversation)]==["user"]


def test_stopped_managed_compute_retains_result_and_assets(api_harness):
    from backend.tests.agent_fakes import _Runtime,_MemoryStorage
    from backend.tests.api.test_agent_control import ARGUMENTS
    from materialsagent.application.tools import build_tool_registry
    entered,release=Event(),Event()
    class Compute(_Runtime):
        def execute(self,*args):
            entered.set();assert release.wait(15)
            return super().execute(*args)
    tool=Compute();storage=_MemoryStorage()
    api_harness.persist_actor("managed-stop");conversation=api_harness.persist_conversation("managed-stop").conversation_id
    model=MockAgentModel(lambda *_:{"type":"CallTool","tool_name":"zta35g_sem_virtual_lab","arguments":ARGUMENTS})
    with api_harness.create_client("managed-stop",agent_model=model,tool_registry=build_tool_registry(tool),storage_service=storage) as client:
        submitted=accept(client,conversation,"ZTA35G")
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(advance,client,submitted)
            try:
                assert entered.wait(10)
                url=f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop"
                assert client.post(url,json={"submission_id":submitted["submission_id"]}).status_code==200
                assert client.delete(f"/api/v1/conversations/{conversation}").status_code==409
            finally:release.set()
            result=future.result(timeout=10)
        assert result["stopped"] and result["executions"][0]["status"]=="SUCCEEDED"
        notice=messages(client,conversation)[-1]
        assert notice["phase"]=="notification" and notice["artifacts"]
        assert "验证结果" not in notice["text"]
        assert tool.calls==1 and storage.objects
        assert client.get(f"/api/v1/assets/{notice['artifacts'][0]['attachment_id']}/content").status_code==200


def test_stop_between_preparation_and_dispatch_terminalizes_only_original_invocation(api_harness,monkeypatch):
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow
    client,conversation=client_for(api_harness)
    entered,release=Event(),Event()
    with client:
        runtime=client.app.state.agent_runtime;original=runtime.tools.prepare
        def prepared(*args):
            result=original(*args);entered.set();assert release.wait(15);return result
        monkeypatch.setattr(runtime.tools,"prepare",prepared)
        monkeypatch.setattr(runtime.tools,"execute",lambda *_: (_ for _ in ()).throw(AssertionError("dispatch after stop")))
        submitted=accept(client,conversation)
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(advance,client,submitted)
            try:
                assert entered.wait(10)
                response=client.post(f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop",json={"submission_id":submitted["submission_id"]})
                assert response.status_code==200,response.text
            finally:release.set()
            assert future.result(timeout=10)["stopped"]
        with api_harness.engine.connect() as connection:
            assert connection.scalar(select(InvocationRunRow.status))=="FAILED"
        assert client.delete(f"/api/v1/conversations/{conversation}").status_code==200


def test_stop_after_model_reservation_prevents_provider_dispatch(api_harness,monkeypatch):
    client,conversation=client_for(api_harness,MockAgentModel(lambda *_: (_ for _ in ()).throw(AssertionError("provider dispatched"))))
    entered,release=Event(),Event()
    with client:
        store=client.app.state.agent_runtime.store;original=store.save
        def reserved(run):
            original(run)
            if run.calls and run.calls[-1].status=="RUNNING":
                entered.set();assert release.wait(15)
        monkeypatch.setattr(store,"save",reserved)
        submitted=accept(client,conversation)
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(advance,client,submitted)
            try:
                assert entered.wait(10)
                response=client.post(f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop",json={"submission_id":submitted["submission_id"]})
                assert response.status_code==200,response.text
            finally:release.set()
            assert future.result(timeout=10)["stopped"]


def test_stopped_regeneration_does_not_publish_an_empty_answer_version(api_harness):
    entered,cancelled=Event(),Event()
    client,conversation=client_for(api_harness,MockAgentModel(lambda *_:{"type":"Finish","answer":"原回答"}))
    with client:
        original=advance(client,accept(client,conversation,"解释固溶"))
        snapshot=messages(client,conversation)
        client.app.state.agent_runtime.model=_WaitingModel(entered,cancelled)
        submitted=client.post(f"/api/v1/messages/{original['final_message_id']}/regenerate",json={},headers={"Idempotency-Key":"stopped-version"}).json()["data"]
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(advance,client,submitted)
            assert entered.wait(10)
            assert client.post(f"/api/v1/agent-runs/{submitted['agent_run']['agent_run_id']}/stop",json={"submission_id":submitted["submission_id"]}).status_code==200
            assert future.result(timeout=10)["stopped"] and cancelled.wait(1)
        assert messages(client,conversation)==snapshot
        assert len(client.get(f"/api/v1/messages/{original['final_message_id']}/versions").json()["data"]["items"])==1


def test_unrecoverable_execution_failure_uses_receipt_version_and_terminates(api_harness):
    client, conversation = client_for(api_harness)
    runtime = client.app.state.agent_runtime
    def broken_execute(*args):
        raise RuntimeError("execution interrupted")
    runtime.tools.execute = broken_execute
    runtime.tools.repair = lambda *args: None
    with client:
        result = advance(client, accept(client, conversation))
        assert result["status"] == "TERMINATED"
        assert result["final_message_id"] is None
        assert result["observations"][-1]["status"] == "FAILED"
        assert len(result["executions"]) == 1


def test_regenerated_history_keeps_original_order_and_freezes_later_context(api_harness):
    client, conversation = client_for(api_harness, MockAgentModel(lambda role, payload: {"type": "Finish", "answer": payload["goal"]}))
    with client:
        first = advance(client, accept(client, conversation, "第一问", "one"))
        second = advance(client, accept(client, conversation, "第二问", "two"))
        regenerated = client.post(f"/api/v1/messages/{first['final_message_id']}/regenerate",
            headers={"Idempotency-Key": "regenerate-old"}, json={}).json()["data"]
        new_first = advance(client, regenerated)
        store = client.app.state.agent_runtime.store
        frozen = store.get(new_first["agent_run_id"], "turn-test")
        assert frozen.context_message_ids == []
        third = accept(client, conversation, "第三问", "three")
        current = store.get(third["agent_run"]["agent_run_id"], "turn-test")
        assert current.context_message_ids == [first["source_message_id"], new_first["final_message_id"],
            second["source_message_id"], second["final_message_id"]]


def test_duplicate_managed_proposal_allocates_no_second_task_or_invocation(api_harness):
    from backend.tests.agent_fakes import _Runtime, _MemoryStorage
    from backend.tests.api.test_agent_control import ARGUMENTS
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.infrastructure.db.conversation_task import TaskRow
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow
    tool, storage = _Runtime(), _MemoryStorage()
    api_harness.persist_actor("managed-duplicate")
    conversation = api_harness.persist_conversation("managed-duplicate").conversation_id
    def respond(role, payload):
        if payload.get("observations"):
            return {"type": "Finish", "answer": "实验已完成，结果图片可查看。", "sources": ["结果 1"]}
        return {"type": "CallTool", "tool_name": "zta35g_sem_virtual_lab", "arguments": ARGUMENTS}
    with api_harness.create_client("managed-duplicate", agent_model=MockAgentModel(respond),
            tool_registry=build_tool_registry(tool), storage_service=storage) as client:
        result = advance(client, accept(client, conversation, "ZTA35G"))
        assert result["status"] == "SUCCEEDED" and len(result["executions"]) == 1
        assert tool.calls == 1 and messages(client, conversation)[-1]["artifacts"]
    with api_harness.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(TaskRow).where(TaskRow.conversation_id == conversation)) == 1
        assert c.scalar(select(func.count()).select_from(InvocationRunRow).where(InvocationRunRow.conversation_id == conversation)) == 1
