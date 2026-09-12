from sqlalchemy import func, select
from materialsagent.infrastructure.db.agent import AgentRunRow, AgentObservationRow, FinalAnswerRow
from materialsagent.infrastructure.llm.agent_model import MockAgentModel


def client_and_conversation(harness, model=None):
    harness.persist_actor("agent-test")
    conversation = harness.persist_conversation("agent-test")
    client = harness.create_client("agent-test", agent_model=model or MockAgentModel())
    return client, conversation.conversation_id


def test_new_message_executes_tool_then_decides_and_persists_answer(api_harness):
    client, conversation_id = client_and_conversation(api_harness)
    with client:
        response = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"mode": "NEW_RUN", "content_text": "1000 MPa 转 GPa"}, headers={"Idempotency-Key": "agent-api-convert"})
        assert response.status_code == 200, response.text
        run = response.json()["data"]["agent_run"]
        assert run["status"] == "SUCCEEDED", (run["error_code"], run["steps"], run["observations"], run["pending_execution"])
        assert len(run["executions"]) == 1
        assert run["observations"][0]["data"]["value"] == 1
        assert len(run["steps"]) == 2
        replay = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"mode": "NEW_RUN", "content_text": "1000 MPa 转 GPa"}, headers={"Idempotency-Key": "agent-api-convert"})
        assert replay.json()["data"]["agent_run"]["final_answer"] == run["final_answer"]
        assert replay.json()["data"]["idempotency_replayed"]
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(AgentRunRow)) == 1
        assert connection.scalar(select(func.count()).select_from(AgentObservationRow)) == 1
        assert connection.scalar(select(func.count()).select_from(FinalAnswerRow)) == 1


def test_regeneration_reuses_observation_without_executor(api_harness):
    client, conversation_id = client_and_conversation(api_harness)
    with client:
        first = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"mode": "NEW_RUN", "content_text": "1000 MPa 转 GPa"}, headers={"Idempotency-Key": "agent-first"})
        run = first.json()["data"]["agent_run"]
        assert run["status"] == "SUCCEEDED", (run["error_code"], run["steps"], run["observations"], run["pending_execution"])
        regenerated = client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/retry",
            json={"retry_type": "ANSWER_REGENERATION"}, headers={"Idempotency-Key": "agent-regenerate"})
        assert regenerated.status_code == 200, regenerated.text
        result = regenerated.json()["data"]["agent_run"]
        assert result["status"] == "SUCCEEDED", result
        assert result["tool_execution_disabled"]
        assert result["tool_executions"] == 0
        assert result["source_agent_run_id"] == run["agent_run_id"]


def test_removed_message_contract_rejected(api_harness):
    client, conversation_id = client_and_conversation(api_harness)
    with client:
        response = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"submission_mode": "NEW_TASK", "content_text": "hi"}, headers={"Idempotency-Key": "old-mode"})
        assert response.status_code == 422


def test_managed_result_is_observed_then_converted(api_harness, monkeypatch):
    from backend.tests.agent_fakes import _MemoryStorage
    from materialsagent.application.tools import build_tool_registry
    api_harness.persist_actor("agent-managed")
    conversation = api_harness.persist_conversation("agent-managed")
    from backend.tests.agent_fakes import _Runtime
    runtime_tool = _Runtime()
    def respond(role, payload):
        if role == "final_answer":
            return "两个工具结果已完成。"
        results = [o for o in payload["observations"] if o["kind"] == "TOOL_RESULT"]
        if len(results) == 0:
            return {"type": "CallTool", "tool_name": "zta35g_sem_virtual_lab", "arguments": {
                "material": "ZTA35G", "solution_temperature": {"value": 1000, "unit": "°C"},
                "solution_time": {"value": 2, "unit": "h"}, "aging_temperature": {"value": 730, "unit": "°C"},
                "aging_time": {"value": 2, "unit": "h"}, "requested_outputs": ["sem_image", "mechanical_properties"]}}
        if len(results) == 1:
            assert results[0]["result_id"] and results[0]["artifacts"]
            return {"type": "CallTool", "tool_name": "materials_unit_conversion", "arguments": {
                "value": results[0]["data"]["yield_strength"]["value"], "from_unit": "MPa", "to_unit": "GPa"}}
        return {"type": "Finish", "needs_synthesis": True}
    with api_harness.create_client("agent-managed", agent_model=MockAgentModel(respond),
            tool_registry=build_tool_registry(runtime_tool), storage_service=_MemoryStorage()) as client:
        execution_errors = []
        original_execute = client.app.state.agent_runtime.tools.execute
        def capture(*args):
            try:
                return original_execute(*args)
            except Exception:
                import traceback
                execution_errors.append(traceback.format_exc())
                raise
        monkeypatch.setattr(client.app.state.agent_runtime.tools, "execute", capture)
        response = client.post(f"/api/v1/conversations/{conversation.conversation_id}/messages",
            json={"mode": "NEW_RUN", "content_text": "预测并换算性能"}, headers={"Idempotency-Key": "managed-two-tools"})
        assert response.status_code == 200, response.text
        run = response.json()["data"]["agent_run"]
        assert run["status"] == "SUCCEEDED", run["error_code"]
        assert not execution_errors, "\n".join(execution_errors)
        assert run["tool_executions"] == 2
        assert runtime_tool.calls == 1
        assert len(run["observations"]) == 2
