import asyncio
import copy

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGenerationChunk

from materialsagent.application.agent_process import AgentProcess, PublicText
from backend.tests.unit.test_agent_runtime import setup, tool_call


def test_private_identifier_is_hidden_at_every_chunk_boundary():
    secret = "private-resource-6e62dd76"
    for boundary in range(1, len(secret)):
        guard = PublicText({secret})
        guard.push("引用 " + secret[:boundary])
        assert guard.text == "引用 "
        guard.push(secret[boundary:] + " 的结果")
        assert secret not in guard.finish()
        assert "的结果" in guard.text
    guard = PublicText({secret})
    guard.push("引用 " + secret[:12])
    assert secret[:12] not in guard.finish(incomplete=True)


def test_process_coalesces_notifications_and_reloads_completed_content():
    async def scenario():
        run, store, _, _, _ = setup(AIMessage(content="answer"))
        process = AgentProcess(store)
        channel = await process.channel(run.agent_run_id, "actor")
        queue = asyncio.Queue(maxsize=1)
        channel.listeners.add(queue)
        for index in range(50):
            await process.upsert(run.agent_run_id, "actor", {"segment_id": "text", "kind": "text",
                "purpose": "pending", "status": "streaming", "text": str(index)})
        assert queue.qsize() == 1
        await process.finish(run.agent_run_id, "actor", "SUCCEEDED", final_message_id="answer")
        await process.upsert(run.agent_run_id, "actor", {"segment_id": "text", "kind": "text",
            "purpose": "pending", "status": "streaming", "text": "late duplicate"})
        restored = await AgentProcess(store).snapshot(run.agent_run_id, "actor")
        assert restored["segments"][0]["text"] == "49"
        assert restored["segments"][0]["purpose"] == "answer"
        assert restored["segments"][0]["status"] == "complete"
    asyncio.run(scenario())


class StreamingModel(BaseChatModel):
    release: asyncio.Event

    @property
    def _llm_type(self):
        return "process-stream-test"

    def bind_tools(self, *_args, **_kwargs):
        return self

    def _generate(self, *_args, **_kwargs):
        raise AssertionError("streaming required")

    async def _astream(self, *_args, **_kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先分析条件。"}))
        yield ChatGenerationChunk(message=AIMessageChunk(content="研究"))
        await self.release.wait()
        yield ChatGenerationChunk(message=AIMessageChunk(content="回答。", response_metadata={"finish_reason": "stop"}))


def test_native_sdk_stream_publishes_before_completion_without_duplicate_tail():
    async def scenario():
        run, store, _, factory, runtime = setup()
        release = asyncio.Event()
        factory.chat = StreamingModel(release=release)
        runtime.schedule(run)
        task = next(iter(runtime.running_tasks.values()))
        try:
            async with asyncio.timeout(5):
                while True:
                    state = await runtime.process.snapshot(run.agent_run_id, "actor")
                    if any(s["text"] == "研究" for s in state["segments"]):
                        break
                    await asyncio.sleep(.01)
            assert not task.done()
            assert store.run.final_message_id is None
            assert any(s["kind"] == "reasoning" and s["text"] == "先分析条件。" for s in state["segments"])
        finally:
            release.set()
            await task
        saved = copy.deepcopy(store.process["segments"])
        assert next(s for s in saved if s["kind"] == "text")["text"] == "研究回答。"
        assert next(s for s in saved if s["kind"] == "text")["purpose"] == "answer"
        assert store.run.status == "SUCCEEDED"
    asyncio.run(scenario())


def test_process_distinguishes_tool_narration_from_answer_and_marks_truncation():
    async def scenario():
        call = tool_call()
        call.content = "先换算单位。"
        call.additional_kwargs = {"reasoning_content": "检查单位是否一致。"}
        run, store, _, _, runtime = setup(call, AIMessage(content="结果为 4。"))
        await runtime.advance(run.agent_run_id, "actor")
        assert next(s for s in store.process["segments"] if s["text"] == "先换算单位。")["purpose"] == "process"
        assert any(s.get("tool_status") == "SUCCEEDED" and s.get("presentation") for s in store.process["segments"])
        run, store, _, _, runtime = setup(AIMessage(content="未完成的", response_metadata={"finish_reason": "length"}))
        await runtime.advance(run.agent_run_id, "actor")
        assert store.run.error_code == "FINAL_ANSWER_TRUNCATED"
        assert store.process["segments"][0]["status"] == "interrupted"
    asyncio.run(scenario())


def test_confirmation_accepted_during_last_owner_read_is_not_lost():
    from threading import Event
    async def scenario():
        run, store, _, _, runtime = setup()
        entered, release = Event(), Event()
        calls = []
        original_get = store.get
        def delayed_get(*args):
            value = original_get(*args)
            if len(calls) == 1 and value.status == "WAITING_FOR_CONFIRMATION":
                entered.set()
                assert release.wait(5)
            return value
        store.get = delayed_get
        async def advance(*_args, **_kwargs):
            calls.append(1)
            store.run.status = "WAITING_FOR_CONFIRMATION" if len(calls) == 1 else "SUCCEEDED"
        runtime.advance = advance
        runtime.schedule(run)
        task = next(iter(runtime.running_tasks.values()))
        assert await asyncio.to_thread(entered.wait, 5)
        store.run.status = "PENDING"
        runtime.schedule(original_get(run.agent_run_id, "actor"))
        release.set()
        await task
        assert len(calls) == 2 and store.run.status == "SUCCEEDED"
    asyncio.run(scenario())
