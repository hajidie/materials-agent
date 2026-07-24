from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import func, select

from backend.tests.api.conftest import BASE_TIME
from backend.tests.api.test_assets import _MemoryStorage
from backend.tests.api.test_explanation_outcomes import (
    _CountingMemoryStorage,
    _Runtime,
    _UnavailableRuntime,
)
from materialsagent.application.chat_orchestration import (
    FOLLOW_UP_TEXT,
)
from materialsagent.application.explanation_service import ExplanationService
from materialsagent.application.result_service import ResultService
from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tools import build_tool_registry
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.idempotency_record import (
    IdempotencyRecordRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.application.messages import MessageSubmissionService
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.db.tool_result import ToolResultRow
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
    default_mock_responder,
)


class _CountingResponder:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    def __call__(self, orchestration_input):
        with self._lock:
            self.calls += 1
        return default_mock_responder(orchestration_input)


class _CommitThenReportFailureUnitOfWork(SQLAlchemyUnitOfWork):
    def commit(self) -> None:
        super().commit()
        raise PersistenceError("Commit result was uncertain.")


class _FirstCommitUncertainFactory:
    def __init__(self, engine) -> None:
        self._session_factory = create_session_factory(engine)
        self.calls = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        self.calls += 1
        if self.calls == 1:
            return _CommitThenReportFailureUnitOfWork(
                self._session_factory
            )
        return SQLAlchemyUnitOfWork(self._session_factory)


class _NthCommitUncertainUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, controller) -> None:
        super().__init__(session_factory)
        self._controller = controller

    def commit(self) -> None:
        self._controller.commits += 1
        super().commit()
        if self._controller.commits == self._controller.fail_on_commit:
            raise PersistenceError("Commit result was uncertain.")


class _NthCommitUncertainFactory:
    def __init__(self, engine, *, fail_on_commit: int) -> None:
        self._session_factory = create_session_factory(engine)
        self.fail_on_commit = fail_on_commit
        self.commits = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return _NthCommitUncertainUnitOfWork(
            self._session_factory,
            self,
        )


class _CorruptNeedsInputFinalizeUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, controller) -> None:
        super().__init__(session_factory)
        self._controller = controller

    def commit(self) -> None:
        self._controller.commits += 1
        super().commit()
        if self._controller.commits != 4:
            return
        with self._controller.session_factory() as session:
            assistant = session.scalar(
                select(MessageRow)
                .where(
                    MessageRow.actor_id == self._controller.actor_id,
                    MessageRow.role == "ASSISTANT",
                )
                .order_by(MessageRow.created_at.desc())
            )
            assert assistant is not None
            assistant.content_text = self._controller.assistant_text
            session.commit()
        raise PersistenceError("Commit result was uncertain.")


class _CorruptNeedsInputFinalizeFactory:
    def __init__(
        self,
        engine,
        *,
        actor_id: str,
        assistant_text: str,
    ) -> None:
        self.session_factory = create_session_factory(engine)
        self.actor_id = actor_id
        self.assistant_text = assistant_text
        self.commits = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return _CorruptNeedsInputFinalizeUnitOfWork(
            self.session_factory,
            self,
        )


class _RaiseAfterPrepareService:
    def __init__(self, delegate: MessageSubmissionService) -> None:
        self._delegate = delegate

    def prepare_submission(self, *args, **kwargs):
        self._delegate.prepare_submission(*args, **kwargs)
        raise RuntimeError("Injected boundary failure.")


class _BlockFirstAfterPrepareService:
    def __init__(self, delegate: MessageSubmissionService) -> None:
        self._delegate = delegate
        self._lock = Lock()
        self._prepared_calls = 0
        self.first_prepared = Event()
        self.release_first = Event()

    def prepare_submission(self, *args, **kwargs):
        prepared = self._delegate.prepare_submission(*args, **kwargs)
        with self._lock:
            self._prepared_calls += 1
            should_block = self._prepared_calls == 1
        if should_block:
            self.first_prepared.set()
            assert self.release_first.wait(timeout=10)
        return prepared


def _conversation(client) -> str:
    response = client.post("/api/v1/conversations", json={})
    assert response.status_code == 201
    return response.json()["data"]["conversation_id"]


def _submit(
    client,
    conversation_id: str,
    *,
    key: str | None,
    content_text: str = "知识问题",
):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={
            "submission_mode": "NEW_TASK",
            "content_text": content_text,
        },
    )


def _full_tool_options(
    api_harness,
    runtime,
    storage,
    explanation,
    *,
    tool_factory=None,
    result_factory=None,
    explanation_factory=None,
):
    normal_factory = api_harness.unit_of_work_factory
    options = {
        "tool_execution_service": ToolExecutionService(
            tool_factory or normal_factory,
            build_tool_registry(runtime),
            clock=lambda: BASE_TIME.replace(hour=1),
            seed_factory=lambda: 801,
        ),
        "storage_service": storage,
        "explanation_port": explanation,
        "m7_tool_chain_enabled": True,
    }
    if result_factory is not None:
        options["result_service"] = ResultService(
            result_factory,
            clock=lambda: BASE_TIME.replace(hour=1),
        )
    if explanation_factory is not None:
        options["explanation_service"] = ExplanationService(
            explanation_factory,
            explanation,
            clock=lambda: BASE_TIME.replace(hour=1),
        )
    return options


def test_task_create_replay_conflict_and_missing_key_are_exact(
    api_harness,
) -> None:
    actor_id = "actor_m8_idem"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    port = MockChatOrchestrationAdapter(responder)
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=port,
    ) as client:
        conversation_id = _conversation(client)
        missing = _submit(client, conversation_id, key=None)
        blank = _submit(client, conversation_id, key="   ")
        first = _submit(client, conversation_id, key="opaque-key")
        replay = _submit(client, conversation_id, key="opaque-key")
        conflict = _submit(
            client,
            conversation_id,
            key="opaque-key",
            content_text="different",
        )

    assert missing.status_code == blank.status_code == 422
    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["task"]["task_id"] == replay.json()["data"]["task"]["task_id"]
    assert (
        first.json()["data"]["user_message"]["message_id"]
        == replay.json()["data"]["user_message"]["message_id"]
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert responder.calls == 1

    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1


def test_twenty_concurrent_task_create_requests_have_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_concurrent"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        conversation_id = _conversation(client)

        def submit(_: int):
            return _submit(client, conversation_id, key="twenty-same-key")

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(submit, range(20)))

    assert {response.status_code for response in responses} == {200}, [
        response.json()
        for response in responses
        if response.status_code != 200
    ]
    payloads = [response.json()["data"] for response in responses]
    assert len({payload["task"]["task_id"] for payload in payloads}) == 1
    assert len(
        {payload["user_message"]["message_id"] for payload in payloads}
    ) == 1
    assert sum(not payload["idempotency_replayed"] for payload in payloads) == 1
    assert responder.calls == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1


def test_twenty_concurrent_full_tool_task_creates_execute_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_concurrent_full_tool"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    execution = ToolExecutionService(
        api_harness.unit_of_work_factory,
        build_tool_registry(runtime),
        clock=lambda: BASE_TIME.replace(hour=1),
        seed_factory=lambda: 812,
    )
    with api_harness.create_client(
        actor_id,
        tool_execution_service=execution,
        storage_service=storage,
        explanation_port=explanation,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
        m7_tool_chain_enabled=True,
    ) as client:
        conversation_id = _conversation(client)

        def submit(_: int):
            return _submit(
                client,
                conversation_id,
                key="twenty-same-full-tool-key",
                content_text="完整合法 Tool 请求",
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(submit, range(20)))

    assert {response.status_code for response in responses} == {200}, [
        response.json()
        for response in responses
        if response.status_code != 200
    ]
    payloads = [response.json()["data"] for response in responses]
    assert len({payload["task"]["task_id"] for payload in payloads}) == 1
    completed_payloads = [
        payload
        for payload in payloads
        if payload["task"]["selected_tool_run_id"] is not None
    ]
    assert completed_payloads
    assert len(
        {
            payload["task"]["selected_tool_run_id"]
            for payload in completed_payloads
        }
    ) == 1
    assert len(
        {
            payload["result_summary"]["result_id"]
            for payload in completed_payloads
        }
    ) == 1
    assert sum(not payload["idempotency_replayed"] for payload in payloads) == 1
    assert responder.calls == 1
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 1
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(AssetRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(ToolResultRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1


def test_supplement_replay_creates_one_message_and_next_full_revision(
    api_harness,
) -> None:
    actor_id = "actor_m8_supplement"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        conversation_id = _conversation(client)
        initial = _submit(
            client,
            conversation_id,
            key="initial-key",
            content_text="缺 aging_temperature",
        )
        task_id = initial.json()["data"]["task"]["task_id"]
        body = {
            "submission_mode": "SUPPLEMENT_TASK",
            "target_task_id": task_id,
            "content_text": "仍缺 aging_temperature",
        }
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "supplement-key"},
            json=body,
        )
        replay = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "supplement-key"},
            json=body,
        )
        conflict = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "supplement-key"},
            json={**body, "content_text": "different"},
        )

    assert initial.status_code == first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert conflict.status_code == 409
    assert responder.calls == 2
    with api_harness.engine.connect() as connection:
        revisions = list(
            connection.execute(
                select(
                    TaskInputRevisionRow.revision,
                    TaskInputRevisionRow.source_message_ids,
                ).order_by(TaskInputRevisionRow.revision)
            )
        )
        assert [row.revision for row in revisions] == [1, 2]
        assert len(revisions[0].source_message_ids) == 1
        assert len(revisions[1].source_message_ids) == 2
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(select(func.count()).select_from(MessageRow)) == 4
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 2


def test_twenty_concurrent_supplements_create_one_second_revision(
    api_harness,
) -> None:
    actor_id = "actor_m8_supplement_concurrent"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        conversation_id = _conversation(client)
        initial = _submit(
            client,
            conversation_id,
            key="initial-concurrent",
            content_text="缺 aging_temperature",
        )
        task_id = initial.json()["data"]["task"]["task_id"]
        body = {
            "submission_mode": "SUPPLEMENT_TASK",
            "target_task_id": task_id,
            "content_text": "仍缺 aging_temperature",
        }

        def supplement(_: int):
            return client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": "same-supplement"},
                json=body,
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(supplement, range(20)))

    assert {response.status_code for response in responses} == {200}, [
        response.json()
        for response in responses
        if response.status_code != 200
    ]
    assert sum(
        not response.json()["data"]["idempotency_replayed"]
        for response in responses
    ) == 1
    assert responder.calls == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 2


def test_different_supplement_keys_conflict_before_second_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_supplement_distinct_keys"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        conversation_id = _conversation(client)
        initial = _submit(
            client,
            conversation_id,
            key="distinct-supplement-initial",
            content_text="缺 aging_temperature",
        )
    assert initial.status_code == 200, initial.json()
    task_id = initial.json()["data"]["task"]["task_id"]
    with api_harness.engine.connect() as connection:
        user_messages_before = connection.scalar(
            select(func.count())
            .select_from(MessageRow)
            .where(MessageRow.role == "USER")
        )
        records_before = connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.operation == "TASK_INPUT_SUPPLEMENT"
            )
        )
        chat_calls_before = connection.scalar(
            select(func.count())
            .select_from(LLMCallRow)
            .where(
                LLMCallRow.task_id == task_id,
                LLMCallRow.purpose == "CHAT_ORCHESTRATION",
            )
        )
    service = _BlockFirstAfterPrepareService(
        MessageSubmissionService(api_harness.unit_of_work_factory)
    )
    with api_harness.create_client(
        actor_id,
        message_submission_service=service,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        def submit_first():
            return client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": "distinct-supplement-a"},
                json={
                    "submission_mode": "SUPPLEMENT_TASK",
                    "target_task_id": task_id,
                    "content_text": "仍缺 aging_temperature A",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(submit_first)
            assert service.first_prepared.wait(timeout=10)
            second = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": "distinct-supplement-b"},
                json={
                    "submission_mode": "SUPPLEMENT_TASK",
                    "target_task_id": task_id,
                    "content_text": "仍缺 aging_temperature B",
                },
            )
            service.release_first.set()
            first = first_future.result(timeout=10)

    assert first.status_code == 200, first.json()
    assert second.status_code == 409, second.json()
    assert second.json()["error"]["code"] == "TARGET_TASK_NOT_RECOVERABLE"
    assert responder.calls == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(MessageRow)
            .where(MessageRow.role == "USER")
        ) == user_messages_before + 1
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.operation == "TASK_INPUT_SUPPLEMENT"
            )
        ) == records_before + 1
        assert connection.scalar(
            select(func.count())
            .select_from(LLMCallRow)
            .where(
                LLMCallRow.task_id == task_id,
                LLMCallRow.purpose == "CHAT_ORCHESTRATION",
            )
        ) == chat_calls_before + 1
        assert connection.scalar(
            select(func.count())
            .select_from(LLMCallRow)
            .where(
                LLMCallRow.task_id == task_id,
                LLMCallRow.purpose == "CHAT_ORCHESTRATION",
                LLMCallRow.status.in_(("PENDING", "RUNNING")),
            )
        ) == 0


def test_task_create_own_reservation_commit_uncertainty_continues_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_message_commit"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    factory = _FirstCommitUncertainFactory(api_harness.engine)
    conversation_id = api_harness.persist_conversation(
        actor_id
    ).conversation_id
    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        first = _submit(
            client,
            conversation_id,
            key="uncertain-message-commit",
        )
        replay = _submit(
            client,
            conversation_id,
            key="uncertain-message-commit",
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert responder.calls == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert connection.scalar(
            select(func.count()).select_from(MessageRow)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1


def test_supplement_own_reservation_commit_uncertainty_continues_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_supplement_reservation"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        conversation_id = _conversation(client)
        initial = _submit(
            client,
            conversation_id,
            key="uncertain-supplement-initial",
            content_text="缺 aging_temperature",
        )
    assert initial.status_code == 200, initial.json()
    task_id = initial.json()["data"]["task"]["task_id"]
    factory = _FirstCommitUncertainFactory(api_harness.engine)
    body = {
        "submission_mode": "SUPPLEMENT_TASK",
        "target_task_id": task_id,
        "content_text": "仍缺 aging_temperature",
    }
    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "uncertain-supplement-reservation"},
            json=body,
        )
        replay = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "uncertain-supplement-reservation"},
            json=body,
        )

    assert first.status_code == replay.status_code == 200, (
        first.json(),
        replay.json(),
    )
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["task"]["status"] == "NEEDS_INPUT"
    assert responder.calls == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.operation == "TASK_INPUT_SUPPLEMENT"
            )
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 2


def test_chat_prepare_commit_uncertainty_requeries_and_calls_provider_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_chat_prepare"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    conversation_id = api_harness.persist_conversation(
        actor_id,
        title="existing title",
    ).conversation_id
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=2,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        response = _submit(
            client,
            conversation_id,
            key="uncertain-chat-prepare",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["idempotency_replayed"] is False
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert responder.calls == 1


def test_chat_start_commit_uncertainty_requeries_and_calls_provider_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_chat_start"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    conversation_id = api_harness.persist_conversation(
        actor_id,
        title="existing title",
    ).conversation_id
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        response = _submit(
            client,
            conversation_id,
            key="uncertain-chat-start",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["idempotency_replayed"] is False
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert responder.calls == 1


def test_chat_success_finalize_commit_uncertainty_returns_projection(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_chat_success_finalize"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    conversation_id = api_harness.persist_conversation(
        actor_id,
        title="existing title",
    ).conversation_id
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=4,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        response = _submit(
            client,
            conversation_id,
            key="uncertain-chat-success-finalize",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["assistant_message"]["content_text"]
    assert responder.calls == 1


@pytest.mark.parametrize(
    ("persisted_text", "expected_status"),
    [
        pytest.param(FOLLOW_UP_TEXT, 200, id="exact"),
        pytest.param(
            "错误的持久化追问正文。",
            409,
            id="mismatch",
        ),
    ],
)
def test_chat_needs_input_finalize_uncertainty_requires_exact_follow_up_text(
    api_harness,
    persisted_text: str,
    expected_status: int,
) -> None:
    actor_id = (
        "actor_m8_uncertain_chat_needs_input_text_"
        f"{expected_status}"
    )
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    conversation_id = api_harness.persist_conversation(
        actor_id,
        title="existing title",
    ).conversation_id
    factory = _CorruptNeedsInputFinalizeFactory(
        api_harness.engine,
        actor_id=actor_id,
        assistant_text=persisted_text,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        response = _submit(
            client,
            conversation_id,
            key=f"uncertain-chat-needs-input-text-{expected_status}",
            content_text="缺 aging_temperature",
        )

    assert response.status_code == expected_status, response.json()
    if expected_status == 409:
        assert response.json()["error"]["code"] == "RESOURCE_CONFLICT"
        task_id = response.json()["resource"]["task_id"]
    else:
        assert response.json()["data"]["task"]["status"] == "NEEDS_INPUT"
        task_id = response.json()["data"]["task"]["task_id"]
    assert responder.calls == 1
    with api_harness.engine.connect() as connection:
        task = connection.execute(
            select(TaskRow.current_status).where(
                TaskRow.task_id == task_id
            )
        ).one()
        assistant_text = connection.scalar(
            select(MessageRow.content_text).where(
                MessageRow.task_id == task_id,
                MessageRow.role == "ASSISTANT",
            )
        )
        assert task.current_status == "NEEDS_INPUT"
        assert assistant_text == persisted_text


def test_chat_failure_finalize_commit_uncertainty_returns_persisted_outcome(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_chat_failure_finalize"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    conversation_id = api_harness.persist_conversation(
        actor_id,
        title="existing title",
    ).conversation_id
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=4,
    )

    with api_harness.create_client(
        actor_id,
        unit_of_work_factory=factory,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        response = _submit(
            client,
            conversation_id,
            key="uncertain-chat-failure-finalize",
            content_text="mock timeout",
        )

    assert response.status_code == 504, response.json()
    assert response.json()["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert responder.calls == 1
    with api_harness.engine.connect() as connection:
        task = connection.execute(
            select(
                TaskRow.current_status,
                TaskRow.error_code,
            ).where(TaskRow.actor_id == actor_id)
        ).one()
        assert task.current_status == "FAILED"
        assert task.error_code == "UPSTREAM_TIMEOUT"


def test_initial_tool_run_prepare_commit_uncertainty_completes_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_initial_tool_prepare"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=1,
    )

    with api_harness.create_client(
        actor_id,
        **_full_tool_options(
            api_harness,
            runtime,
            storage,
            explanation,
            tool_factory=factory,
        ),
    ) as client:
        conversation_id = _conversation(client)
        response = _submit(
            client,
            conversation_id,
            key="uncertain-initial-tool-prepare",
            content_text="完整合法 Tool 请求",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 1


def test_initial_runtime_success_commit_uncertainty_completes_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_initial_runtime_success"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )

    with api_harness.create_client(
        actor_id,
        **_full_tool_options(
            api_harness,
            runtime,
            storage,
            explanation,
            tool_factory=factory,
        ),
    ) as client:
        conversation_id = _conversation(client)
        response = _submit(
            client,
            conversation_id,
            key="uncertain-initial-runtime-success",
            content_text="完整合法 Tool 请求",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1


def test_runtime_failure_commit_uncertainty_selects_failed_run(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_runtime_failure"
    api_harness.persist_actor(actor_id)
    runtime = _UnavailableRuntime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )

    with api_harness.create_client(
        actor_id,
        **_full_tool_options(
            api_harness,
            runtime,
            storage,
            explanation,
            tool_factory=factory,
        ),
    ) as client:
        conversation_id = _conversation(client)
        response = _submit(
            client,
            conversation_id,
            key="uncertain-runtime-failure",
            content_text="完整合法 Tool 请求",
        )

    assert response.status_code == 503, response.json()
    assert response.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"
    assert runtime.calls == 1
    assert storage.put_calls == 0
    assert explanation.call_count == 0
    with api_harness.engine.connect() as connection:
        task = connection.execute(
            select(
                TaskRow.task_id,
                TaskRow.current_status,
                TaskRow.selected_tool_run_id,
                TaskRow.selected_result_id,
            ).where(TaskRow.actor_id == actor_id)
        ).one()
        run = connection.execute(
            select(
                ToolRunRow.tool_run_id,
                ToolRunRow.current_status,
            ).where(ToolRunRow.task_id == task.task_id)
        ).one()
        assert task.current_status == "FAILED"
        assert task.selected_tool_run_id == run.tool_run_id
        assert task.selected_result_id is None
        assert run.current_status == "FAILED"


def test_initial_explanation_prepare_commit_uncertainty_completes_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_initial_explanation_prepare"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=1,
    )

    with api_harness.create_client(
        actor_id,
        **_full_tool_options(
            api_harness,
            runtime,
            storage,
            explanation,
            explanation_factory=factory,
        ),
    ) as client:
        conversation_id = _conversation(client)
        response = _submit(
            client,
            conversation_id,
            key="uncertain-initial-explanation-prepare",
            content_text="完整合法 Tool 请求",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1


def test_initial_explanation_finalize_commit_uncertainty_completes_one_chain(
    api_harness,
) -> None:
    actor_id = "actor_m8_uncertain_initial_explanation_finalize"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    explanation = MockExplanationAdapter()
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )

    with api_harness.create_client(
        actor_id,
        **_full_tool_options(
            api_harness,
            runtime,
            storage,
            explanation,
            explanation_factory=factory,
        ),
    ) as client:
        conversation_id = _conversation(client)
        response = _submit(
            client,
            conversation_id,
            key="uncertain-initial-explanation-finalize",
            content_text="完整合法 Tool 请求",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert explanation.call_count == 1


def test_failure_after_binding_before_provider_is_replayable_without_provider(
    api_harness,
) -> None:
    actor_id = "actor_m8_after_binding_failure"
    api_harness.persist_actor(actor_id)
    responder = _CountingResponder()
    service = _RaiseAfterPrepareService(
        MessageSubmissionService(api_harness.unit_of_work_factory)
    )
    with api_harness.create_client(
        actor_id,
        message_submission_service=service,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as failing_client:
        conversation_id = _conversation(failing_client)
        failed = _submit(
            failing_client,
            conversation_id,
            key="after-binding-failure",
        )
    with api_harness.create_client(
        actor_id,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as replay_client:
        replay = _submit(
            replay_client,
            conversation_id,
            key="after-binding-failure",
        )

    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "INTERNAL_ERROR"
    assert replay.status_code == 200
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert replay.json()["data"]["task"]["status"] == "PENDING"
    assert responder.calls == 0


def test_successful_supplement_revalidates_full_input_and_replay_skips_tool(
    api_harness,
) -> None:
    actor_id = "actor_m8_successful_supplement"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    explanation = MockExplanationAdapter()
    execution = ToolExecutionService(
        api_harness.unit_of_work_factory,
        build_tool_registry(runtime),
        clock=lambda: BASE_TIME.replace(hour=1),
        seed_factory=lambda: 811,
    )
    with api_harness.create_client(
        actor_id,
        tool_execution_service=execution,
        storage_service=_MemoryStorage(),
        explanation_port=explanation,
        chat_orchestration_port=MockChatOrchestrationAdapter(
            default_mock_responder
        ),
        m7_tool_chain_enabled=True,
    ) as client:
        conversation_id = _conversation(client)
        initial = _submit(
            client,
            conversation_id,
            key="successful-supplement-initial",
            content_text="缺 aging_temperature",
        )
        task_id = initial.json()["data"]["task"]["task_id"]
        body = {
            "submission_mode": "SUPPLEMENT_TASK",
            "target_task_id": task_id,
            "content_text": "完整合法 Tool 请求",
        }
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "successful-supplement"},
            json=body,
        )
        replay = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "successful-supplement"},
            json=body,
        )

    assert initial.status_code == 200
    assert initial.json()["data"]["task"]["status"] == "NEEDS_INPUT"
    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["task"]["status"] == "SUCCEEDED"
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert (
        first.json()["data"]["result_summary"]["result_id"]
        == replay.json()["data"]["result_summary"]["result_id"]
    )
    assert runtime.calls == 1
    assert explanation.call_count == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 2
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 1


def test_supplement_revalidates_conversation_and_actor_ownership(
    api_harness,
) -> None:
    owner_id = "actor_m8_supplement_owner"
    other_id = "actor_m8_supplement_other"
    api_harness.persist_actor(owner_id)
    api_harness.persist_actor(other_id)
    with api_harness.create_client(owner_id) as owner:
        source_conversation = _conversation(owner)
        wrong_conversation = _conversation(owner)
        initial = _submit(
            owner,
            source_conversation,
            key="supplement-ownership-source",
            content_text="缺 aging_temperature",
        )
        task_id = initial.json()["data"]["task"]["task_id"]
        body = {
            "submission_mode": "SUPPLEMENT_TASK",
            "target_task_id": task_id,
            "content_text": "完整合法 Tool 请求",
        }
        wrong_conversation_response = owner.post(
            f"/api/v1/conversations/{wrong_conversation}/messages",
            headers={"Idempotency-Key": "wrong-conversation-supplement"},
            json=body,
        )
    with api_harness.create_client(other_id) as other:
        foreign_response = other.post(
            f"/api/v1/conversations/{source_conversation}/messages",
            headers={"Idempotency-Key": "foreign-supplement"},
            json=body,
        )

    for response in (wrong_conversation_response, foreign_response):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(TaskInputRevisionRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(IdempotencyRecordRow)
        ) == 1
