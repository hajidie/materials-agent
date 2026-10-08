import json

from backend.tests.agent_state import wait_run
from materialsagent.domain.models.agent import ExecutionRecord, Observation
from materialsagent.infrastructure.llm.agent_model import MockAgentModel


def decode_sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and line != "data: {}"]


def test_sse_snapshot_reconnect_history_and_owner_boundary(api_harness):
    actor = "stream-owner"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor).conversation_id
    model = MockAgentModel(lambda *_: {"type": "Finish", "answer": "## 研究结论\n\n保留正文。"})
    with api_harness.create_client(actor, agent_model=model) as client:
        accepted = client.post(f"/api/v1/conversations/{conversation}/messages",
            json={"content_text": "请解释"}, headers={"Idempotency-Key": "stream"})
        assert accepted.status_code == 202
        run_id = accepted.json()["data"]["agent_run"]["agent_run_id"]
        assert wait_run(client, run_id).json()["data"]["status"] == "SUCCEEDED"
        path = f"/api/v1/agent-runs/{run_id}"
        stream = client.get(path + "/events")
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: snapshot" in stream.text and "event: settled" in stream.text
        snapshot = decode_sse(stream.text)[0]
        assert snapshot["segments"][0]["purpose"] == "answer"
        assert snapshot["segments"][0]["text"].startswith("## 研究结论")
        replay = client.get(path + "/events", headers={"Last-Event-ID": "obsolete:99"})
        assert decode_sse(replay.text)[0]["segments"] == snapshot["segments"]
        assert client.post(path + "/advance", json={}).status_code == 404
    with api_harness.create_client(actor, agent_model=MockAgentModel(lambda *_: (_ for _ in ()).throw(AssertionError("history must not call model")))) as client:
        assert client.get(path + "/process").json()["data"]["segments"] == snapshot["segments"]
    api_harness.persist_actor("other-reader")
    with api_harness.create_client("other-reader") as client:
        assert client.get(path + "/process").status_code == 404
        assert client.get(path + "/events").status_code == 404
    with api_harness.create_client(actor) as client:
        assert client.delete(f"/api/v1/conversations/{conversation}").status_code == 200
    from sqlalchemy import select, func
    from materialsagent.infrastructure.db.agent import AgentProcessRow
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(AgentProcessRow)) == 0


def test_pending_restart_requires_explicit_idempotent_resume(api_harness, monkeypatch):
    actor = "pending-recovery"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor).conversation_id
    with api_harness.create_client(actor) as client:
        monkeypatch.setattr(client.app.state.agent_runtime, "schedule", lambda _: None)
        submitted = client.post(f"/api/v1/conversations/{conversation}/messages",
            json={"content_text": "1000 MPa 转 GPa"}, headers={"Idempotency-Key": "pending"}).json()["data"]
    run_id = submitted["agent_run"]["agent_run_id"]
    with api_harness.create_client(actor) as client:
        path = f"/api/v1/agent-runs/{run_id}"
        current = client.get(path).json()["data"]
        assert current["status"] == "INTERRUPTED"
        assert client.app.state.agent_runtime.store.get(run_id, actor).calls == []
        body = {"submission_id": current["submission_id"], "version": current["version"]}
        assert client.post(path + "/resume", json=body).status_code == 202
        assert wait_run(client, run_id).json()["data"]["status"] == "SUCCEEDED"
        count = len(client.app.state.agent_runtime.store.get(run_id, actor).calls)
        assert client.post(path + "/resume", json=body).status_code == 202
        assert len(client.app.state.agent_runtime.store.get(run_id, actor).calls) == count


def test_resume_receipt_keeps_original_request_version(api_harness, monkeypatch):
    actor = "receipt-resume"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor).conversation_id
    with api_harness.create_client(actor) as client:
        runtime = client.app.state.agent_runtime
        run, _ = runtime.store.submit(conversation, actor, "检查原计算", "receipt")
        run.status = "RUNNING"
        runtime.store.save(run)
        arguments = {"value": 1000, "from_unit": "MPa", "to_unit": "GPa"}
        run.draft = runtime.tools.resolve(run, "materials_unit_conversion", arguments)
        record = ExecutionRecord(tool_call_id="old-call", tool_name=run.draft.tool_name, version=run.draft.version,
            schema_hash=run.draft.schema_hash, arguments=run.draft.normalized, execution_fingerprint="b" * 64)
        record = runtime.tools.prepare(run, record)
        record.dispatched, record.status = True, "RUNNING"
        run.pending_execution = record
        runtime.store.save(run)
        run.status = "INTERRUPTED"
        runtime.store.save(run)
        version = run.version
        observation = Observation(tool_call_id=record.tool_call_id, kind="TOOL_RESULT", status="SUCCEEDED",
            tool_name=record.tool_name, invocation_run_id=record.invocation_run_id, data={"value": 1})
        monkeypatch.setattr(runtime.tools, "repair", lambda *_: observation)
        monkeypatch.setattr(runtime, "schedule", lambda _: None)
        body = {"submission_id": run.submission_id, "version": version}
        path = f"/api/v1/agent-runs/{run.agent_run_id}/resume"
        assert client.post(path, json=body).status_code == 202
        assert client.post(path, json=body).status_code == 202
        current = runtime.store.get(run.agent_run_id, actor)
        assert len(current.executions) == 1 and len(current.observations) == 1
        assert current.resumed_version == version
