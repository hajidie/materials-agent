"""Fault injection against real PostgreSQL transactions and public recovery."""
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, func
from sqlalchemy.exc import OperationalError

from materialsagent.application.agent_runtime import AgentRuntime
from materialsagent.application.context import ActorContext
from materialsagent.domain.models.agent import ModelCall, RunBudget, identifier
from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.llm.agent_model import MockAgentModel
from backend.tests.agent_state import wait_run


def new_run(harness, actor):
    harness.persist_actor(actor)
    conversation = harness.persist_conversation(actor).conversation_id
    store = SQLAlchemyAgentStore(create_session_factory(harness.engine))
    run, _ = store.submit(conversation, actor, "解释材料", "fault-injection")
    run.status, run.claim, run.process_id = "RUNNING", identifier(), "original-process"
    store.save(run)
    return store, run


def test_lost_commit_ack_reuses_atomic_answer_and_original_operation(api_harness, monkeypatch):
    store, run = new_run(api_harness, "lost-answer-commit")
    original, attempts = store._save_once, []
    def lost(value):
        attempts.append(value.persistence_operation_id)
        original(value)
        raise OperationalError("COMMIT", None, OSError("lost ack"))
    monkeypatch.setattr(store, "_save_once", lost)
    run.status, run.final_message_id = "SUCCEEDED", identifier()
    run.pending_message = {"message_id": run.final_message_id, "phase": "answer", "text": "有效答案", "sources": []}
    store.save(run)
    assert len(attempts) == 1 and run.pending_message is None
    assert store.get(run.agent_run_id, run.actor_id).status == "SUCCEEDED"
    with store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MessageRow).where(MessageRow.message_id == run.final_message_id)) == 1


def test_confirmed_uncommitted_write_retries_only_persistence(api_harness, monkeypatch):
    store, run = new_run(api_harness, "absent-write-retry")
    original, attempts, waits = store._save_once, [], []
    def unavailable(value):
        attempts.append(value.persistence_operation_id)
        if len(attempts) < 3:
            raise OperationalError("COMMIT", None, OSError("down"))
        original(value)
    monkeypatch.setattr(store, "_save_once", unavailable)
    monkeypatch.setattr("materialsagent.infrastructure.db.agent.time.sleep", waits.append)
    run.error_code = "diagnostic"
    store.save(run)
    assert len(attempts) == 3 and len(set(attempts)) == 1 and waits == [0.5, 1]


def test_recovery_scan_releases_same_process_orphan_but_keeps_live_owner(api_harness):
    store, run = new_run(api_harness, "orphan-scan")
    run.calls.append(ModelCall(role="agent_decision", prompt_digest="digest", output_limit=20, input_reserved=10))
    store.save(run)
    assert store.recover_interrupted("original-process", include_current_orphans=True,
        protected_run_ids={run.agent_run_id}) == []
    assert store.get(run.agent_run_id, run.actor_id).status == "RUNNING"
    store.recover_interrupted("original-process", include_current_orphans=True, protected_run_ids=set())
    recovered = store.get(run.agent_run_id, run.actor_id)
    assert recovered.status == "INTERRUPTED" and recovered.llm_tokens == 30
    assert recovered.calls[0].usage.source == "estimated"
    store.recover_interrupted("original-process", include_current_orphans=True)
    assert store.get(run.agent_run_id, run.actor_id).llm_tokens == 30


def test_public_resume_keeps_budget_and_rejects_old_version(api_harness, monkeypatch):
    actor = "public-recovery"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor).conversation_id
    requests = []
    def answer(*_):
        requests.append(True)
        if len(requests) <= 3:
            raise TimeoutError()
        return {"type": "Finish", "answer": "恢复完成。"}
    monkeypatch.setattr("langchain.agents.middleware.model_retry.calculate_delay", lambda *args, **kwargs: 0)
    with api_harness.create_client(actor, agent_model=MockAgentModel(answer)) as client:
        submitted = client.post(f"/api/v1/conversations/{conversation}/messages", json={"content_text": "解释材料"},
            headers={"Idempotency-Key": "public-recover"}).json()["data"]
        run_id = submitted["agent_run"]["agent_run_id"]
        paused = wait_run(client, run_id).json()["data"]
        assert paused["status"] == "INTERRUPTED" and paused["can_resume"] and paused["recovery_action"] == "CONTINUE"
        runtime = client.app.state.agent_runtime
        before = runtime.store.get(run_id, actor)
        response = client.post(f"/api/v1/agent-runs/{run_id}/resume", json={"submission_id": before.submission_id, "version": before.version})
        assert response.status_code == 202
        completed = wait_run(client, run_id).json()["data"]
        after = runtime.store.get(run_id, actor)
        assert completed["status"] == "SUCCEEDED" and after.llm_tokens > before.llm_tokens and after.budget == before.budget
        assert len(requests) == 4
        repeated = client.post(f"/api/v1/agent-runs/{run_id}/resume", json={"submission_id": before.submission_id, "version": before.version})
        assert repeated.status_code == 202 and len(requests) == 4
        stale = client.post(f"/api/v1/agent-runs/{run_id}/resume", json={"submission_id": before.submission_id, "version": before.version - 1})
        assert stale.status_code == 409


@pytest.mark.parametrize("operation", ["submit", "resume"])
def test_recovery_scan_does_not_interrupt_newly_admitted_run(api_harness, monkeypatch, operation):
    from materialsagent.api.routes.agent_runs import Submission, submit

    actor = "scan-admission"
    api_harness.persist_actor(actor)
    conversation = api_harness.persist_conversation(actor).conversation_id
    store = SQLAlchemyAgentStore(create_session_factory(api_harness.engine))
    scan_entered, release_scan, model_entered, release_model = (Event() for _ in range(4))
    original = store.recover_interrupted

    def delayed_scan(*args, **kwargs):
        scan_entered.set()
        assert release_scan.wait(5)
        return original(*args, **kwargs)

    def answer(*_):
        model_entered.set()
        assert release_model.wait(5)
        return {"type": "Finish", "answer": "正常完成。"}

    monkeypatch.setattr(store, "recover_interrupted", delayed_scan)

    async def scenario():
        runtime = AgentRuntime(store, MockAgentModel(answer),
            SimpleNamespace(catalog=lambda: [], repair=lambda *_: None), checkpointer=InMemorySaver())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            agent_runtime=runtime, agent_budget=RunBudget())), state=SimpleNamespace(request_id="scan-request"))
        if operation == "resume":
            paused, _ = store.submit(conversation, actor, "解释材料", "scan-resume")
            paused.status, paused.error_code = "INTERRUPTED", "PROCESS_INTERRUPTED"
            store.save(paused)
        scan = asyncio.create_task(runtime.recover(include_current_orphans=True))
        admitted = None
        try:
            assert await asyncio.to_thread(scan_entered.wait, 5)
            admitted = asyncio.create_task(
                submit(conversation, Submission(content_text="解释材料"), request,
                    ActorContext(actor_id=actor, user_id=None), "scan-submit") if operation == "submit" else
                runtime.resume(paused.agent_run_id, actor, paused.submission_id, paused.version))
            # Give the request a chance to register while the scan is in its DB
            # thread. The old snapshot misses that owner and interrupts it.
            await asyncio.wait({admitted}, timeout=0.5)
            release_scan.set()
            await asyncio.wait_for(scan, 5)
            accepted = await asyncio.wait_for(admitted, 5)
            run_id = accepted["data"]["agent_run"]["agent_run_id"] if operation == "submit" else accepted.agent_run_id
            assert await asyncio.to_thread(model_entered.wait, 5)
            release_model.set()
            await asyncio.wait_for(asyncio.gather(*list(runtime.running_tasks.values())), 5)
            completed = store.get(run_id, actor)
            assert completed.status == "SUCCEEDED" and completed.final_message_id
            assert completed.calls[0].status == "SUCCEEDED"
        finally:
            release_scan.set()
            release_model.set()
            await asyncio.gather(scan, *([admitted] if admitted else []), return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())
