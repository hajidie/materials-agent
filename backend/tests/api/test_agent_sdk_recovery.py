from __future__ import annotations

import asyncio

import pytest

from materialsagent.application.agent_runtime import AgentRuntime
from materialsagent.domain.ports.agent import AgentConflictError
from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore
from materialsagent.infrastructure.db.agent_checkpoint import AgentCheckpointStore
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.llm.agent_model import MockAgentModel


def test_ask_user_interrupt_survives_process_restart(api_harness):
    actor_id = "sdk-question-restart"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    model = MockAgentModel(lambda _role, payload: {"type": "Finish", "answer": "已收到补充。"}
        if payload["user_inputs"] else {"type": "AskUser", "question": "请说明目标性能。"})

    with api_harness.create_client(actor_id, agent_model=model) as client:
        submitted = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"content_text": "研究材料"}, headers={"Idempotency-Key": "sdk-question"}).json()["data"]
        run_id = submitted["agent_run"]["agent_run_id"]
        waiting = client.post(f"/api/v1/agent-runs/{run_id}/advance",
            json={"submission_id": submitted["submission_id"]}).json()["data"]
        assert waiting["status"] == "WAITING_FOR_USER"

    with api_harness.create_client(actor_id, agent_model=model) as client:
        current = client.get(f"/api/v1/agent-runs/{run_id}").json()["data"]
        assert current["status"] == "WAITING_FOR_USER"
        reply = {"question_message_id": waiting["question_message_id"],
                 "waiting_version": waiting["waiting_version"]}
        submitted = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"content_text": "屈服强度", "reply_to": reply},
            headers={"Idempotency-Key": "sdk-answer"}).json()["data"]
        result = client.post(f"/api/v1/agent-runs/{run_id}/advance",
            json={"submission_id": submitted["submission_id"]}).json()["data"]
        assert result["status"] == "SUCCEEDED"
        assert result["agent_run_id"] == run_id


def test_confirmation_interrupt_survives_process_restart(api_harness):
    from materialsagent.application.fake_side_effect_tool import FakeSideEffectSink
    from materialsagent.application.tools import build_tool_registry

    actor_id = "sdk-confirm-restart"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    sink = FakeSideEffectSink()
    registry = build_tool_registry(enable_dev_fake_side_effect_tool=True, fake_side_effect_sink=sink)
    settings = api_harness.settings.model_copy(update={"enable_dev_fake_side_effect_tool": True})
    model = MockAgentModel(lambda _role, payload: {"type": "Finish", "answer": "已处理。"}
        if payload["observations"] else {"type": "CallTool", "tool_name": "dev_fake_side_effect",
            "arguments": {"message": "test-only receipt"}})

    with api_harness.create_client(actor_id, settings=settings, agent_model=model, tool_registry=registry) as client:
        submitted = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"content_text": "执行测试操作"}, headers={"Idempotency-Key": "sdk-confirm"}).json()["data"]
        run_id = submitted["agent_run"]["agent_run_id"]
        waiting = client.post(f"/api/v1/agent-runs/{run_id}/advance",
            json={"submission_id": submitted["submission_id"]}).json()["data"]
        assert waiting["status"] == "WAITING_FOR_CONFIRMATION"
        assert sink.write_count == 0

    with api_harness.create_client(actor_id, settings=settings, agent_model=model, tool_registry=registry) as client:
        pending = waiting["pending_execution"]
        response = client.post(f"/api/v1/agent-runs/{run_id}/invocations/{pending['invocation_run_id']}/confirm",
            json={"waiting_version": waiting["waiting_version"],
                  "confirmation_version": pending["confirmation_version"]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "SUCCEEDED"
        assert sink.write_count == 1


def test_conversation_delete_drains_failed_checkpoint_cleanup(api_harness, monkeypatch):
    actor_id = "sdk-delete-cursor"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    with api_harness.create_client(actor_id, agent_model=MockAgentModel(
            lambda *_: {"type": "Finish", "answer": "已回答。"})) as client:
        runtime = client.app.state.agent_runtime
        original_delete = runtime.checkpoint_cleanup.delete

        async def unavailable(_run_id):
            raise RuntimeError("temporary checkpoint outage")

        monkeypatch.setattr(runtime.checkpoint_cleanup, "delete", unavailable)
        submitted = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"content_text": "解释材料"}, headers={"Idempotency-Key": "sdk-delete"}).json()["data"]
        run_id = submitted["agent_run"]["agent_run_id"]
        finished = client.post(f"/api/v1/agent-runs/{run_id}/advance",
            json={"submission_id": submitted["submission_id"]}).json()["data"]
        assert finished["status"] == "SUCCEEDED"
        assert runtime.store.checkpoint_cleanup_ids() == [run_id]
        monkeypatch.setattr(runtime.checkpoint_cleanup, "delete", original_delete)
        assert client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 200
        assert runtime.store.checkpoint_cleanup_ids() == []

    async def check_cursor_removed():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        try:
            assert await saver.aget_tuple({"configurable": {"thread_id": run_id}}) is None
        finally:
            await checkpoints.close()

    asyncio.run(check_cursor_removed())


class SimulatedCrash(BaseException):
    pass


class NoTools:
    def catalog(self):
        return []

    def repair(self, run, record):
        return None


def test_final_checkpoint_replays_publication_without_another_model_call(api_harness):
    actor_id = "sdk-recovery"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    store = SQLAlchemyAgentStore(create_session_factory(api_harness.engine))
    run, _ = store.submit(conversation_id, actor_id, "请说明结果", "sdk-crash-final")

    async def crash_after_final_checkpoint():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = AgentRuntime(store, MockAgentModel(
            lambda *_: {"type": "Finish", "answer": "已完成。"}), NoTools(),
            checkpointer=saver, checkpoint_cleanup=checkpoints)

        def crash_before_publication(_run):
            raise SimulatedCrash()

        runtime._verified_observations = crash_before_publication
        try:
            with pytest.raises(SimulatedCrash):
                await runtime.advance(run.agent_run_id, actor_id)
        finally:
            await runtime.close()
            await checkpoints.close()

    asyncio.run(crash_after_final_checkpoint())
    assert store.get(run.agent_run_id, actor_id).status == "RUNNING"

    async def restart_and_recover():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()

        def unexpected_model_call(*_args):
            raise AssertionError("checkpoint replay must not call the model")

        runtime = AgentRuntime(store, MockAgentModel(unexpected_model_call), NoTools(),
            checkpointer=saver, checkpoint_cleanup=checkpoints)
        try:
            await runtime.recover()
        finally:
            await runtime.close()
            await checkpoints.close()

    asyncio.run(restart_and_recover())
    recovered = store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED"
    assert recovered.final_message_id is not None
    assert [item["text"] for item in recovered.messages if item["message_id"] == recovered.final_message_id] == ["已完成。"]
    assert store.checkpoint_cleanup_ids() == []

    async def check_cursor_removed():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        try:
            assert await saver.aget_tuple({"configurable": {"thread_id": run.agent_run_id}}) is None
        finally:
            await checkpoints.close()

    asyncio.run(check_cursor_removed())


def test_replied_question_replays_without_asking_again(api_harness):
    actor_id = "sdk-replied-crash"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    store = SQLAlchemyAgentStore(create_session_factory(api_harness.engine))
    model = MockAgentModel(lambda _role, payload: {"type": "Finish", "answer": "已收到。"}
        if payload["user_inputs"] else {"type": "AskUser", "question": "请说明性能。"})
    run, _ = store.submit(conversation_id, actor_id, "研究材料", "sdk-replied-initial")

    async def ask_then_crash():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = AgentRuntime(store, model, NoTools(), checkpointer=saver, checkpoint_cleanup=checkpoints)
        original_save = store.save
        try:
            waiting = await runtime.advance(run.agent_run_id, actor_id)
            assert waiting.status == "WAITING_FOR_USER"
            store.submit(conversation_id, actor_id, "屈服强度", "sdk-replied-answer",
                run_id=run.agent_run_id, waiting_version=waiting.waiting_version,
                question_message_id=waiting.question_message_id)

            def crash_after_reply(current):
                original_save(current)
                if current.answered_questions and current.status == "RUNNING":
                    raise SimulatedCrash()

            store.save = crash_after_reply
            with pytest.raises(SimulatedCrash):
                await runtime.advance(run.agent_run_id, actor_id)
        finally:
            store.save = original_save
            await runtime.close()
            await checkpoints.close()

    asyncio.run(ask_then_crash())

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = AgentRuntime(store, model, NoTools(), checkpointer=saver, checkpoint_cleanup=checkpoints)
        try:
            await runtime.recover()
        finally:
            await runtime.close()
            await checkpoints.close()

    asyncio.run(restart())
    recovered = store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED", recovered.error_code
    assert len(recovered.answered_questions) == 1
    from sqlalchemy import func, select
    from materialsagent.infrastructure.db.conversation_task import MessageRow
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MessageRow).where(
            MessageRow.conversation_id == conversation_id, MessageRow.phase == "question")) == 1


def test_reply_claim_crash_resumes_the_existing_question(api_harness):
    actor_id = "sdk-reply-claim-crash"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    store = SQLAlchemyAgentStore(create_session_factory(api_harness.engine))
    model = MockAgentModel(lambda _role, payload: {"type": "Finish", "answer": "已收到。"}
        if payload["user_inputs"] else {"type": "AskUser", "question": "请说明性能。"})
    run, _ = store.submit(conversation_id, actor_id, "研究材料", "sdk-reply-claim")

    async def crash_after_claim():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = AgentRuntime(store, model, NoTools(), checkpointer=saver, checkpoint_cleanup=checkpoints)
        original_save = store.save
        try:
            waiting = await runtime.advance(run.agent_run_id, actor_id)
            store.submit(conversation_id, actor_id, "屈服强度", "sdk-reply-claim-answer",
                run_id=run.agent_run_id, waiting_version=waiting.waiting_version,
                question_message_id=waiting.question_message_id)

            def crash(current):
                original_save(current)
                if current.status == "RUNNING" and current.question_message_id and current.user_inputs:
                    raise SimulatedCrash()

            store.save = crash
            with pytest.raises(SimulatedCrash):
                await runtime.advance(run.agent_run_id, actor_id)
        finally:
            store.save = original_save
            await runtime.close()
            await checkpoints.close()

    asyncio.run(crash_after_claim())

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = AgentRuntime(store, model, NoTools(), checkpointer=saver, checkpoint_cleanup=checkpoints)
        try:
            await runtime.recover()
        finally:
            await runtime.close()
            await checkpoints.close()

    asyncio.run(restart())
    assert store.get(run.agent_run_id, actor_id).status == "SUCCEEDED"


def test_confirmation_claim_crash_preserves_approval(api_harness):
    from materialsagent.application.fake_side_effect_tool import FakeSideEffectSink
    from materialsagent.application.tools import build_tool_registry

    actor_id = "sdk-confirm-claim-crash"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    sink = FakeSideEffectSink()
    registry = build_tool_registry(enable_dev_fake_side_effect_tool=True, fake_side_effect_sink=sink)
    settings = api_harness.settings.model_copy(update={"enable_dev_fake_side_effect_tool": True})
    model = MockAgentModel(lambda _role, payload: {"type": "Finish", "answer": "已处理。"}
        if payload["observations"] else {"type": "CallTool", "tool_name": "dev_fake_side_effect",
            "arguments": {"message": "test-only receipt"}})
    client = api_harness.create_client(actor_id, settings=settings, agent_model=model, tool_registry=registry)
    store = client.app.state.agent_runtime.store
    run, _ = store.submit(conversation_id, actor_id, "执行测试操作", "sdk-confirm-claim")

    async def crash_after_claim():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = client.app.state.agent_runtime
        runtime.checkpointer, runtime.checkpoint_cleanup = saver, checkpoints
        original_save = store.save
        try:
            waiting = await runtime.advance(run.agent_run_id, actor_id)
            assert waiting.status == "WAITING_FOR_CONFIRMATION"

            def crash(current):
                original_save(current)
                if current.status == "RUNNING" and current.confirmation_response:
                    raise SimulatedCrash()

            store.save = crash
            with pytest.raises(SimulatedCrash):
                await runtime.advance(run.agent_run_id, actor_id,
                    waiting_version=waiting.waiting_version, confirmation=True)
        finally:
            store.save = original_save
            await runtime.close()
            await checkpoints.close()

    asyncio.run(crash_after_claim())
    assert sink.write_count == 0
    assert store.get(run.agent_run_id, actor_id).confirmation_response["approved"] is True

    second_client = api_harness.create_client(actor_id, settings=settings, agent_model=model, tool_registry=registry)

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        runtime = second_client.app.state.agent_runtime
        runtime.checkpointer, runtime.checkpoint_cleanup = saver, checkpoints
        try:
            await asyncio.to_thread(store.recover_interrupted, runtime.process_id, repair=runtime.tools.repair)
            current = store.get(run.agent_run_id, actor_id)
            with pytest.raises(AgentConflictError):
                await runtime.advance(run.agent_run_id, actor_id,
                    waiting_version=current.waiting_version, confirmation=False)
            assert sink.write_count == 0
            await runtime.recover()
        finally:
            await runtime.close()
            await checkpoints.close()

    asyncio.run(restart())
    assert store.get(run.agent_run_id, actor_id).status == "SUCCEEDED"
    assert sink.write_count == 1
    client.close()
    second_client.close()


def test_new_model_call_cannot_reuse_completed_tool_call_id(api_harness):
    actor_id = "sdk-reused-call-id"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    model = MockAgentModel(lambda _role, payload: {
        "type": "CallTool", "tool_call_id": "reused-call-id",
        "tool_name": "materials_unit_conversion",
        "arguments": {"value": 800 if payload["observations"] else 650,
                      "from_unit": "MPa", "to_unit": "GPa"},
    })
    with api_harness.create_client(actor_id, agent_model=model) as client:
        submitted = client.post(f"/api/v1/conversations/{conversation_id}/messages",
            json={"content_text": "把数值从 MPa 换算到 GPa"},
            headers={"Idempotency-Key": "sdk-reused-call"}).json()["data"]
        run_id = submitted["agent_run"]["agent_run_id"]
        result = client.post(f"/api/v1/agent-runs/{run_id}/advance",
            json={"submission_id": submitted["submission_id"]}).json()["data"]
        assert result["status"] == "TERMINATED"
        stored = client.app.state.agent_runtime.store.get(run_id, actor_id)
        assert stored.error_code == "DUPLICATE_TOOL_CALL_ID"
        assert stored.tool_executions == len(stored.executions) == 1


@pytest.mark.parametrize("crash_window", ["before_dispatch", "after_effect", "after_receipt", "after_clear"])
def test_replay_never_dispatches_a_tool_call_twice(api_harness, crash_window):
    actor_id = "sdk-fence-" + crash_window
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    first_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    first = first_client.app.state.agent_runtime
    run, _ = first.store.submit(conversation_id, actor_id, "650 MPa 转 GPa", "sdk-" + crash_window)
    effects = []

    async def crash():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        first.checkpointer = saver
        first.checkpoint_cleanup = checkpoints
        if crash_window == "before_dispatch":
            original_save = first.store.save

            def crash_at_fence(current):
                if current.pending_execution and current.pending_execution.dispatched:
                    raise SimulatedCrash()
                return original_save(current)

            first.store.save = crash_at_fence
        elif crash_window == "after_effect":
            original_execute = first.tools.execute

            def crash_after_effect(*args):
                original_execute(*args)
                effects.append("dispatched")
                raise SimulatedCrash()

            first.tools.execute = crash_after_effect
        elif crash_window == "after_receipt":
            original_execute = first.tools.execute
            original_receipt = first.store.receipt

            def counted_execute(*args):
                effects.append("dispatched")
                return original_execute(*args)

            def crash_after_receipt(*args):
                original_receipt(*args)
                raise SimulatedCrash()

            first.tools.execute = counted_execute
            first.store.receipt = crash_after_receipt
        else:
            original_execute = first.tools.execute
            original_save = first.store.save

            def counted_execute(*args):
                effects.append("dispatched")
                return original_execute(*args)

            def crash_after_clear(current):
                original_save(current)
                if current.executions and current.pending_tool_call_id is None and current.status == "RUNNING":
                    raise SimulatedCrash()

            first.tools.execute = counted_execute
            first.store.save = crash_after_clear
        try:
            with pytest.raises(SimulatedCrash):
                await first.advance(run.agent_run_id, actor_id)
        finally:
            await first.close()
            await checkpoints.close()

    asyncio.run(crash())
    assert first.store.get(run.agent_run_id, actor_id).status == "RUNNING"
    second_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    second = second_client.app.state.agent_runtime
    original_execute = second.tools.execute

    def count_execute(*args):
        effects.append("dispatched")
        return original_execute(*args)

    second.tools.execute = count_execute

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        second.checkpointer = saver
        second.checkpoint_cleanup = checkpoints
        try:
            await second.recover()
        finally:
            await second.close()
            await checkpoints.close()

    asyncio.run(restart())
    recovered = second.store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED"
    assert effects == ["dispatched"]
    assert len(recovered.executions) == len(recovered.observations) == 1
    first_client.close()
    second_client.close()


@pytest.mark.parametrize("crash_window", ["before_prepare", "after_prepare"])
def test_replay_prepares_an_unlinked_tool_call_once(api_harness, crash_window):
    from sqlalchemy import func, select
    from materialsagent.infrastructure.db.tool_invocation import InvocationRunRow

    actor_id = "sdk-prepare-" + crash_window
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    first_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    first = first_client.app.state.agent_runtime
    run, _ = first.store.submit(conversation_id, actor_id, "650 MPa 转 GPa", "sdk-" + crash_window)

    async def crash():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        first.checkpointer, first.checkpoint_cleanup = saver, checkpoints
        original_prepare = first.tools.prepare

        def interrupted_prepare(*args):
            if crash_window == "after_prepare":
                original_prepare(*args)
            raise SimulatedCrash()

        first.tools.prepare = interrupted_prepare
        try:
            with pytest.raises(SimulatedCrash):
                await first.advance(run.agent_run_id, actor_id)
        finally:
            await first.close()
            await checkpoints.close()

    asyncio.run(crash())
    pending = first.store.get(run.agent_run_id, actor_id).pending_execution
    assert pending is not None and pending.invocation_run_id is None

    second_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    second = second_client.app.state.agent_runtime

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        second.checkpointer, second.checkpoint_cleanup = saver, checkpoints
        try:
            await second.recover()
        finally:
            await second.close()
            await checkpoints.close()

    asyncio.run(restart())
    recovered = second.store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED", recovered.error_code
    assert len(recovered.executions) == len(recovered.observations) == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(InvocationRunRow).where(
            InvocationRunRow.request_id == run.agent_run_id)) == 1
    first_client.close()
    second_client.close()


@pytest.mark.parametrize("crash_window", ["after_effect", "after_receipt"])
def test_replay_after_tool_effect_allows_a_second_tool_call(api_harness, crash_window):
    actor_id = "sdk-two-tools-" + crash_window
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id

    def responder(_role, payload):
        count = len(payload["observations"])
        if count == 2:
            return {"type": "Finish", "answer": "两次换算完成。"}
        return {"type": "CallTool", "tool_name": "materials_unit_conversion",
                "arguments": {"value": 650 + count * 150, "from_unit": "MPa", "to_unit": "GPa"}}

    model = MockAgentModel(responder)
    first_client = api_harness.create_client(actor_id, agent_model=model)
    first = first_client.app.state.agent_runtime
    run, _ = first.store.submit(conversation_id, actor_id, "连续换算", "sdk-two-tools")

    async def crash():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        first.checkpointer, first.checkpoint_cleanup = saver, checkpoints
        if crash_window == "after_effect":
            original_execute = first.tools.execute

            def crash_after_first_effect(*args):
                original_execute(*args)
                raise SimulatedCrash()

            first.tools.execute = crash_after_first_effect
        else:
            original_receipt = first.store.receipt

            def crash_after_first_receipt(*args):
                original_receipt(*args)
                raise SimulatedCrash()

            first.store.receipt = crash_after_first_receipt
        try:
            with pytest.raises(SimulatedCrash):
                await first.advance(run.agent_run_id, actor_id)
        finally:
            await first.close()
            await checkpoints.close()

    asyncio.run(crash())
    second_client = api_harness.create_client(actor_id, agent_model=model)
    second = second_client.app.state.agent_runtime

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        second.checkpointer, second.checkpoint_cleanup = saver, checkpoints
        try:
            await second.recover()
        finally:
            await second.close()
            await checkpoints.close()

    asyncio.run(restart())
    recovered = second.store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED", recovered.error_code
    assert len(recovered.executions) == len(recovered.observations) == 2
    assert recovered.tool_executions == 2
    first_client.close()
    second_client.close()


def test_model_response_without_sdk_checkpoint_replays(api_harness):
    actor_id = "sdk-model-checkpoint-gap"
    api_harness.persist_actor(actor_id)
    conversation_id = api_harness.persist_conversation(actor_id).conversation_id
    first_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    first = first_client.app.state.agent_runtime
    run, _ = first.store.submit(conversation_id, actor_id, "650 MPa 转 GPa", "sdk-model-gap")

    async def crash():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        first.checkpointer, first.checkpoint_cleanup = saver, checkpoints
        original_save = first.store.save

        def crash_after_model_response(current):
            original_save(current)
            if (current.pending_tool_call_id is not None and current.pending_execution is None
                    and current.calls[-1].status == "SUCCEEDED"):
                raise SimulatedCrash()

        first.store.save = crash_after_model_response
        try:
            with pytest.raises(SimulatedCrash):
                await first.advance(run.agent_run_id, actor_id)
        finally:
            await first.close()
            await checkpoints.close()

    asyncio.run(crash())
    second_client = api_harness.create_client(actor_id, agent_model=MockAgentModel())
    second = second_client.app.state.agent_runtime

    async def restart():
        checkpoints = AgentCheckpointStore(api_harness.settings)
        saver = await checkpoints.open()
        second.checkpointer, second.checkpoint_cleanup = saver, checkpoints
        try:
            await second.recover()
        finally:
            await second.close()
            await checkpoints.close()

    asyncio.run(restart())
    recovered = second.store.get(run.agent_run_id, actor_id)
    assert recovered.status == "SUCCEEDED", recovered.error_code
    assert len(recovered.executions) == len(recovered.observations) == 1
    first_client.close()
    second_client.close()
