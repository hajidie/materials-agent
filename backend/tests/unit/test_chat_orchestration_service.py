from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.messages import PreparedSubmission
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceConflictError,
    PersistenceError,
)
from materialsagent.infrastructure.llm.mock import MockChatOrchestrationAdapter


BASE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class _Store:
    conversations: dict[str, Conversation]
    messages: dict[str, Message]
    tasks: dict[str, Task]
    revisions: dict[str, object]
    llm_calls: dict[str, object]


class _ConversationRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, conversation_id: str) -> Conversation | None:
        return self._store.conversations.get(conversation_id)

    def get_owned(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Conversation | None:
        conversation = self.get(conversation_id)
        if conversation is None or conversation.actor_id != actor_id:
            return None
        return conversation


class _MessageRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, message_id: str) -> Message | None:
        return self._store.messages.get(message_id)

    def get_by_llm_call_id(self, llm_call_id: str) -> Message | None:
        return next(
            (
                message
                for message in self._store.messages.values()
                if message.llm_call_id == llm_call_id
            ),
            None,
        )

    def list_for_task(self, task_id: str) -> list[Message]:
        return sorted(
            (
                message
                for message in self._store.messages.values()
                if message.task_id == task_id
            ),
            key=lambda message: (message.created_at, message.message_id),
        )

    def add(self, message: Message) -> None:
        if message.message_id in self._store.messages or (
            message.llm_call_id is not None
            and self.get_by_llm_call_id(message.llm_call_id) is not None
        ):
            raise PersistenceConflictError("Persistence conflict.")
        self._store.messages[message.message_id] = message


class _TaskRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, task_id: str) -> Task | None:
        return self._store.tasks.get(task_id)

    def get_owned(self, task_id: str, actor_id: str) -> Task | None:
        task = self.get(task_id)
        if task is None or task.actor_id != actor_id:
            return None
        return task

    def update(self, task: Task, *, expected_status: str) -> Task | None:
        current = self.get(task.task_id)
        if current is None or current.current_status != expected_status:
            return None
        if (
            current.conversation_id != task.conversation_id
            or current.actor_id != task.actor_id
            or current.created_at != task.created_at
        ):
            return None
        self._store.tasks[task.task_id] = task
        return task


class _RevisionRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, revision_id: str) -> object | None:
        return self._store.revisions.get(revision_id)

    def list_for_task(self, task_id: str) -> list[object]:
        return sorted(
            (
                revision
                for revision in self._store.revisions.values()
                if revision.task_id == task_id
            ),
            key=lambda revision: revision.revision,
        )

    def list_for_llm_call_id(self, llm_call_id: str) -> list[object]:
        return sorted(
            (
                revision
                for revision in self._store.revisions.values()
                if revision.source_llm_call_id == llm_call_id
            ),
            key=lambda revision: (
                revision.task_id,
                revision.revision,
                revision.task_input_revision_id,
            ),
        )

    def add(self, revision: object) -> None:
        if revision.task_input_revision_id in self._store.revisions or any(
            existing.task_id == revision.task_id
            and existing.revision == revision.revision
            for existing in self._store.revisions.values()
        ):
            raise PersistenceConflictError("Persistence conflict.")
        self._store.revisions[revision.task_input_revision_id] = revision


class _LLMCallRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, llm_call_id: str) -> object | None:
        return self._store.llm_calls.get(llm_call_id)

    def add(self, call: object) -> None:
        if call.llm_call_id in self._store.llm_calls:
            raise PersistenceConflictError("Persistence conflict.")
        self._store.llm_calls[call.llm_call_id] = call

    def list_for_task(
        self,
        task_id: str,
        *,
        request_id: str | None = None,
    ) -> list[object]:
        return [
            call
            for call in self._store.llm_calls.values()
            if call.task_id == task_id
            and (request_id is None or call.request_id == request_id)
        ]

    def update(self, call: object, *, expected_status: str) -> object | None:
        current = self.get(call.llm_call_id)
        if current is None or current.status != expected_status:
            return None
        self._store.llm_calls[call.llm_call_id] = call
        return call


class _UnitOfWork:
    def __init__(self, factory: _UnitOfWorkFactory) -> None:
        self._factory = factory
        self.conversations = _ConversationRepository(factory.store)
        self.messages = _MessageRepository(factory.store)
        self.tasks = _TaskRepository(factory.store)
        self.task_input_revisions = _RevisionRepository(factory.store)
        self.llm_calls = _LLMCallRepository(factory.store)
        self._snapshot: tuple[dict[str, object], ...] | None = None
        self._committed = False

    def __enter__(self) -> _UnitOfWork:
        assert self._factory.active == 0
        self._factory.active += 1
        self._snapshot = (
            dict(self._factory.store.conversations),
            dict(self._factory.store.messages),
            dict(self._factory.store.tasks),
            dict(self._factory.store.revisions),
            dict(self._factory.store.llm_calls),
        )
        return self

    def __exit__(self, *_args: object) -> None:
        if not self._committed:
            self.rollback()
        self._factory.active -= 1

    def commit(self) -> None:
        self._factory.commit_count += 1
        if self._factory.commit_count in self._factory.fail_commit_calls:
            raise PersistenceError("Persistence operation failed.")
        self._committed = True

    def rollback(self) -> None:
        if self._snapshot is None:
            return
        (
            conversations,
            messages,
            tasks,
            revisions,
            llm_calls,
        ) = self._snapshot
        self._factory.store.conversations = conversations
        self._factory.store.messages = messages
        self._factory.store.tasks = tasks
        self._factory.store.revisions = revisions
        self._factory.store.llm_calls = llm_calls


class _UnitOfWorkFactory:
    def __init__(
        self,
        store: _Store,
        *,
        fail_commit_calls: set[int] | None = None,
    ) -> None:
        self.store = store
        self.active = 0
        self.instances: list[_UnitOfWork] = []
        self.commit_count = 0
        self.fail_commit_calls = fail_commit_calls or set()

    def __call__(self) -> _UnitOfWork:
        unit_of_work = _UnitOfWork(self)
        self.instances.append(unit_of_work)
        return unit_of_work


class _SequenceClock:
    def __init__(self) -> None:
        self._index = 0

    def __call__(self) -> datetime:
        value = BASE_TIME + timedelta(seconds=self._index)
        self._index += 1
        return value


def _submission() -> tuple[ActorContext, PreparedSubmission, _Store]:
    actor = ActorContext(actor_id="actor_local", user_id=None)
    conversation = Conversation(
        conversation_id="conv_unit",
        actor_id=actor.actor_id,
        title=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    task = Task.pending(
        task_id="task_unit",
        conversation_id=conversation.conversation_id,
        actor_id=actor.actor_id,
        created_at=BASE_TIME,
    )
    message = Message.user(
        message_id="msg_user",
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
        actor_id=actor.actor_id,
        request_id="req_unit",
        content_text="什么是 ZTA35G？",
        created_at=BASE_TIME,
    )
    store = _Store(
        conversations={conversation.conversation_id: conversation},
        messages={message.message_id: message},
        tasks={task.task_id: task},
        revisions={},
        llm_calls={},
    )
    return actor, PreparedSubmission(
        conversation_id=conversation.conversation_id,
        user_message=message,
        task=task,
    ), store


def _id_factory(prefix: str) -> str:
    values = {
        "llm": "llm_unit",
        "msg": "msg_assistant",
        "revision": "revision_unit",
    }
    return values[prefix]


def _service_type() -> type[Any]:
    try:
        from materialsagent.application.chat_orchestration import (
            ChatOrchestrationService,
        )
    except ModuleNotFoundError:
        pytest.fail("ChatOrchestrationService is not implemented yet.")
    return ChatOrchestrationService


def _valid_tool_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "route": "TOOL_EXECUTION",
        "tool_id": "zta35g_sem_virtual_lab",
        "material": "ZTA35G",
        "candidate_parameters": {
            "solution_temperature": {"value": 1000, "unit": "°C"},
            "solution_time": {"value": 3, "unit": "h"},
            "aging_temperature": {"value": 730, "unit": "°C"},
            "aging_time": {"value": 3, "unit": "h"},
        },
        "requested_outputs": ["sem_image", "mechanical_properties"],
    }
    values.update(overrides)
    return values


def _service(
    factory: _UnitOfWorkFactory,
    responder: Callable[[object], dict[str, object]],
) -> object:
    return _service_type()(
        factory,
        MockChatOrchestrationAdapter(responder),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )


def test_knowledge_answer_calls_adapter_outside_uow_and_persists_once() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    calls: list[object] = []

    def responder(orchestration_input: object) -> dict[str, object]:
        assert factory.active == 0
        calls.append(orchestration_input)
        return {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "ZTA35G 是氧化锆增韧氧化铝复合材料。",
        }

    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(responder),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert len(calls) == 1
    assert calls[0].task_id == submission.task.task_id
    assert calls[0].conversation_id == submission.conversation_id
    assert calls[0].request_id == submission.user_message.request_id
    assert calls[0].content_text == submission.user_message.content_text
    assert factory.active == 0
    assert projection.task.task_type == "KNOWLEDGE_QA"
    assert projection.task.current_status == "SUCCEEDED"
    assert projection.task.completed_at is not None
    assert projection.task.selected_tool_run_id is None
    assert projection.task.selected_result_id is None
    assert projection.assistant_message is not None
    assert projection.assistant_message.role == "ASSISTANT"
    assert projection.assistant_message.generation_source == "LLM"
    assert projection.assistant_message.llm_call_id == "llm_unit"
    assert projection.assistant_message.structured_content is None
    assert projection.revision is None
    assert len(store.messages) == 2
    assert len(store.revisions) == 0
    assert len(store.llm_calls) == 1
    call = store.llm_calls["llm_unit"]
    assert call.status == "SUCCEEDED"
    assert call.structured_output_summary["route"] == "KNOWLEDGE_ANSWER"
    assert call.structured_output_summary["answer_length"] == len(
        projection.assistant_message.content_text
    )
    assert "answer_text" not in call.structured_output_summary
    assert call.prompt_digest is not None
    assert len(call.prompt_digest) == 64


def test_needs_input_recomputes_hints_and_persists_formal_partial_revision() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    parameters = _valid_tool_payload()["candidate_parameters"]
    parameters = dict(parameters)
    parameters["aging_temperature"] = None

    projection = _service(
        factory,
        lambda _: {
            "route": "NEEDS_INPUT",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": parameters,
            "missing_fields": ["solution_time"],
            "ambiguous_fields": ["material"],
            "follow_up_suggestion": "SECRET at C:\\private\\provider.txt",
            "requested_outputs": ["mechanical_properties", "sem_image"],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.task_type == "TOOL_EXECUTION"
    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.task.completed_at is None
    assert projection.task.error_code is None
    assert projection.assistant_message is not None
    assert "SECRET" not in projection.assistant_message.content_text
    assert projection.revision is not None
    assert projection.revision.revision == 1
    assert projection.revision.source_llm_call_id == "llm_unit"
    assert projection.revision.source_message_ids == ["msg_user"]
    assert projection.revision.missing_fields == ["aging_temperature"]
    assert projection.revision.ambiguous_fields == []
    assert projection.revision.validation_errors == []
    assert projection.revision.normalized_input["solution_time"] == {
        "value": 3,
        "unit": "h",
    }
    assert store.llm_calls["llm_unit"].structured_output_summary == {
        "route": "NEEDS_INPUT",
        "tool_id": "zta35g_sem_virtual_lab",
        "missing_fields": ("aging_temperature",),
        "ambiguous_fields": (),
    }


def test_ambiguity_is_formally_recomputed_without_becoming_missing_or_invalid() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    parameters = dict(_valid_tool_payload()["candidate_parameters"])
    parameters["solution_time"] = {
        "value": {"candidates": [2, 3]},
        "unit": "h",
    }

    projection = _service(
        factory,
        lambda _: {
            "route": "NEEDS_INPUT",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": parameters,
            "missing_fields": ["aging_time"],
            "ambiguous_fields": [],
            "follow_up_suggestion": "请确认时间。",
            "requested_outputs": ["sem_image"],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.revision is not None
    assert projection.revision.missing_fields == []
    assert projection.revision.ambiguous_fields == [
        {"field": "solution_time", "candidates": [2, 3]}
    ]
    assert projection.revision.validation_errors == []
    assert projection.task.current_status == "NEEDS_INPUT"


@pytest.mark.parametrize(
    ("payload_overrides", "expected_code"),
    [
        ({"material": "OTHER"}, "UNSUPPORTED_MATERIAL"),
        (
            {
                "candidate_parameters": {
                    **_valid_tool_payload()["candidate_parameters"],
                    "solution_time": {"value": 3, "unit": "day"},
                }
            },
            "INVALID_PROCESS_PARAMETER_UNIT",
        ),
        (
            {
                "candidate_parameters": {
                    **_valid_tool_payload()["candidate_parameters"],
                    "solution_temperature": {"value": 1000.5, "unit": "°C"},
                }
            },
            "INVALID_PROCESS_PARAMETER_PRECISION",
        ),
        (
            {
                "candidate_parameters": {
                    **_valid_tool_payload()["candidate_parameters"],
                    "solution_temperature": {"value": 1200, "unit": "°C"},
                }
            },
            "PROCESS_PARAMETERS_OUT_OF_RANGE",
        ),
        ({"requested_outputs": ["unknown"]}, "INVALID_REQUESTED_OUTPUT"),
        (
            {
                "candidate_parameters": {
                    **_valid_tool_payload()["candidate_parameters"],
                    "solution_time": {"value": True, "unit": "h"},
                }
            },
            "INVALID_PROCESS_PARAMETER_TYPE",
        ),
    ],
)
def test_complete_hard_invalid_candidate_succeeds_llm_but_fails_task(
    payload_overrides: dict[str, object],
    expected_code: str,
) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: _valid_tool_payload(**payload_overrides),
        ).orchestrate_submission(actor, submission)

    assert captured.value.status_code == 422
    assert captured.value.code == "VALIDATION_FAILED"
    assert captured.value.details[0]["code"] == expected_code
    revision = next(iter(store.revisions.values()))
    assert revision.validation_errors[0]["code"] == expected_code
    assert store.llm_calls["llm_unit"].status == "SUCCEEDED"
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert store.tasks[submission.task.task_id].error_code == "VALIDATION_FAILED"
    assert len(store.messages) == 1


@pytest.mark.parametrize(
    ("minutes", "expected_hours"),
    [(180, 3), (66, 1.1)],
)
def test_tool_revision_preserves_raw_minutes_and_json_ready_hours(
    minutes: int,
    expected_hours: int | float,
) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    parameters = dict(_valid_tool_payload()["candidate_parameters"])
    parameters["solution_time"] = {"value": minutes, "unit": "min"}

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: _valid_tool_payload(candidate_parameters=parameters),
        ).orchestrate_submission(actor, submission)

    assert captured.value.status_code == 503
    assert captured.value.code == "TOOL_UNAVAILABLE"
    revision = next(iter(store.revisions.values()))
    assert revision.raw_input["parameters"]["solution_time"] == {
        "value": minutes,
        "unit": "min",
    }
    assert revision.normalized_input["solution_time"] == {
        "value": expected_hours,
        "unit": "h",
    }


def test_complete_valid_tool_candidate_is_persisted_then_returns_unavailable() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    with pytest.raises(Exception) as captured:
        _service(factory, lambda _: _valid_tool_payload()).orchestrate_submission(
            actor,
            submission,
        )

    assert captured.value.status_code == 503
    assert captured.value.code == "TOOL_UNAVAILABLE"
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id
    revision = next(iter(store.revisions.values()))
    assert revision.revision == 1
    assert revision.validation_errors == []
    assert store.llm_calls["llm_unit"].status == "SUCCEEDED"
    task = store.tasks[submission.task.task_id]
    assert task.task_type == "TOOL_EXECUTION"
    assert task.current_status == "FAILED"
    assert task.error_code == "TOOL_UNAVAILABLE"
    assert task.completed_at is not None
    assert task.selected_tool_run_id is None
    assert task.selected_result_id is None
    assert len(store.messages) == 1


def test_complete_valid_tool_candidate_stays_running_when_m7_chain_is_enabled() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(lambda _: _valid_tool_payload()),
        clock=_SequenceClock(),
        id_factory=_id_factory,
        tool_chain_enabled=True,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.task_type == "TOOL_EXECUTION"
    assert projection.task.current_status == "RUNNING"
    assert projection.task.completed_at is None
    assert projection.task.error_code is None
    assert projection.task.safe_error_message is None
    assert projection.task.selected_tool_run_id is None
    assert projection.task.selected_result_id is None
    assert projection.assistant_message is None
    assert projection.revision is not None


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (ChatOrchestrationTimeoutError("PRIVATE timeout"), 504, "UPSTREAM_TIMEOUT"),
        (
            ChatOrchestrationProviderError("PRIVATE provider"),
            503,
            "CHAT_ORCHESTRATION_FAILED",
        ),
    ],
)
def test_safe_orchestration_failures_finalize_without_fake_facts(
    failure: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    def fail(_: object) -> dict[str, object]:
        raise failure

    with pytest.raises(Exception) as captured:
        _service(factory, fail).orchestrate_submission(actor, submission)

    assert captured.value.status_code == expected_status
    assert captured.value.code == expected_code
    assert "PRIVATE" not in str(captured.value)
    assert store.llm_calls["llm_unit"].status == "FAILED"
    assert store.llm_calls["llm_unit"].structured_output_summary is None
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert len(store.messages) == 1
    assert store.revisions == {}


def test_protocol_failure_is_safe_and_does_not_persist_provider_payload() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: {"route": "UNKNOWN", "payload": "SECRET"},
        ).orchestrate_submission(actor, submission)

    assert captured.value.status_code == 502
    assert captured.value.code == "CHAT_ORCHESTRATION_FAILED"
    assert "SECRET" not in str(captured.value)
    call = store.llm_calls["llm_unit"]
    assert call.status == "FAILED"
    assert call.structured_output_summary is None
    assert len(store.messages) == 1
    assert store.revisions == {}


def test_finalize_commit_failure_rolls_back_memory_success_and_returns_no_projection() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store, fail_commit_calls={3})

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: {
                "route": "KNOWLEDGE_ANSWER",
                "answer_text": "不能在提交失败时返回。",
            },
        ).orchestrate_submission(actor, submission)

    assert captured.value.code == "INTERNAL_ERROR"
    assert len(store.messages) == 1
    assert store.llm_calls["llm_unit"].status == "RUNNING"
    assert store.tasks[submission.task.task_id].current_status == "RUNNING"
    assert factory.active == 0


def test_repeated_finalize_returns_existing_facts_without_second_message() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(
        factory,
        lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "固定答案。",
        },
    )
    first = service.orchestrate_submission(actor, submission)

    second = service.finalize_result(
        actor,
        submission,
        llm_call_id="llm_unit",
        result=KnowledgeAnswer("不同的陈旧内存答案。"),
    )

    assert second == first
    assert len(store.messages) == 2
    assert len(store.llm_calls) == 1


def test_stale_result_does_not_overwrite_terminal_task() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    def concurrent_terminal(_: object) -> dict[str, object]:
        task = store.tasks[submission.task.task_id]
        store.tasks[task.task_id] = replace(
            task,
            task_type="TOOL_EXECUTION",
            current_status="FAILED",
            updated_at=BASE_TIME + timedelta(seconds=2),
            completed_at=BASE_TIME + timedelta(seconds=2),
            error_code="CONCURRENT_FAILURE",
            safe_error_message="并发终态。",
        )
        return {"route": "KNOWLEDGE_ANSWER", "answer_text": "陈旧答案。"}

    with pytest.raises(Exception) as captured:
        _service(factory, concurrent_terminal).orchestrate_submission(
            actor,
            submission,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id
    assert store.tasks[submission.task.task_id].error_code == "CONCURRENT_FAILURE"
    assert len(store.messages) == 1
    assert store.llm_calls["llm_unit"].status == "RUNNING"


class _UnnormalizedPort:
    provider = "replaceable"
    model_name = "replaceable-v1"

    def __init__(self, result_or_error: object) -> None:
        self._result_or_error = result_or_error

    def orchestrate(self, _orchestration_input: object) -> object:
        if isinstance(self._result_or_error, Exception):
            raise self._result_or_error
        return self._result_or_error


@pytest.mark.parametrize(
    ("result_or_error", "expected_status", "expected_code"),
    [
        (RuntimeError("PRIVATE replacement failure"), 503, "CHAT_ORCHESTRATION_FAILED"),
        (object(), 502, "CHAT_ORCHESTRATION_FAILED"),
    ],
)
def test_replaceable_port_failures_always_finalize_running_facts(
    result_or_error: object,
    expected_status: int,
    expected_code: str,
) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service_type()(
        factory,
        _UnnormalizedPort(result_or_error),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.status_code == expected_status
    assert captured.value.code == expected_code
    assert "PRIVATE" not in str(captured.value)
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id
    assert store.llm_calls["llm_unit"].status == "FAILED"
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert len(store.messages) == 1
    assert store.revisions == {}


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        (ChatOrchestrationTimeoutError("PRIVATE timeout"), 504, "UPSTREAM_TIMEOUT"),
        (
            ChatOrchestrationProviderError("PRIVATE provider"),
            503,
            "CHAT_ORCHESTRATION_FAILED",
        ),
        (
            ChatOrchestrationProtocolError("PRIVATE protocol"),
            502,
            "CHAT_ORCHESTRATION_FAILED",
        ),
    ],
)
def test_independent_port_safe_failures_use_the_formal_contract(
    failure: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service_type()(
        factory,
        _UnnormalizedPort(failure),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.status_code == expected_status
    assert captured.value.code == expected_code
    assert store.llm_calls["llm_unit"].status == "FAILED"
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert len(store.messages) == 1
    assert store.revisions == {}


def test_committed_projection_revalidates_assistant_source_chain() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(
        factory,
        lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "固定答案。",
        },
    )
    service.orchestrate_submission(actor, submission)
    assistant = store.messages["msg_assistant"]
    store.messages[assistant.message_id] = replace(
        assistant,
        actor_id="actor_foreign",
    )

    with pytest.raises(Exception) as captured:
        service.finalize_result(
            actor,
            submission,
            llm_call_id="llm_unit",
            result=KnowledgeAnswer("陈旧答案。"),
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id


def test_failure_finalize_rejects_non_chat_orchestration_call() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    def mutate_purpose_then_timeout(_: object) -> dict[str, object]:
        call = store.llm_calls["llm_unit"]
        object.__setattr__(call, "purpose", "EXPLANATION")
        raise ChatOrchestrationTimeoutError("PRIVATE timeout")

    with pytest.raises(Exception) as captured:
        _service(factory, mutate_purpose_then_timeout).orchestrate_submission(
            actor,
            submission,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id
    assert store.llm_calls["llm_unit"].status == "RUNNING"
    assert store.tasks[submission.task.task_id].current_status == "RUNNING"


def test_knowledge_summary_matches_trimmed_persisted_answer() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    projection = _service(
        factory,
        lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "  固定答案。  ",
        },
    ).orchestrate_submission(actor, submission)

    assert projection.assistant_message is not None
    assert projection.assistant_message.content_text == "固定答案。"
    summary = store.llm_calls["llm_unit"].structured_output_summary
    assert summary["answer_length"] == len("固定答案。")
    assert summary["answer_digest"] == sha256(
        "固定答案。".encode("utf-8")
    ).hexdigest()


def test_orchestration_log_contains_only_safe_correlation_ids(caplog) -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    with caplog.at_level("INFO", logger="materialsagent.chat_orchestration"):
        _service(
            factory,
            lambda _: {
                "route": "KNOWLEDGE_ANSWER",
                "answer_text": "日志安全答案。",
            },
        ).orchestrate_submission(actor, submission)

    records = [
        record
        for record in caplog.records
        if record.name == "materialsagent.chat_orchestration"
    ]
    assert records
    assert any(
        record.request_id == submission.user_message.request_id
        and record.task_id == submission.task.task_id
        and record.llm_call_id == "llm_unit"
        for record in records
    )
    rendered = " ".join(record.getMessage() for record in records)
    assert submission.user_message.content_text not in rendered
    assert "日志安全答案" not in rendered


def test_port_metadata_is_validated_before_any_submission_work() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    class ContractIncompletePort:
        def orchestrate(self, _orchestration_input: object) -> KnowledgeAnswer:
            return KnowledgeAnswer("不会调用。")

    with pytest.raises(ValueError):
        _service_type()(
            factory,
            ContractIncompletePort(),
            clock=_SequenceClock(),
            id_factory=_id_factory,
        )

    assert store.llm_calls == {}
    assert store.tasks[submission.task.task_id].current_status == "PENDING"
    assert len(store.messages) == 1
    assert actor.actor_id == submission.task.actor_id


def test_logging_failure_never_changes_committed_business_result(
    monkeypatch,
) -> None:
    from materialsagent.application import chat_orchestration

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    def fail_log(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(chat_orchestration.logger, "info", fail_log)
    projection = _service(
        factory,
        lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "日志失败不改变业务。",
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "SUCCEEDED"
    assert projection.llm_call.status == "SUCCEEDED"
    assert projection.assistant_message is not None
    assert len(store.messages) == 2


def _repeat_terminal(
    service: object,
    actor: ActorContext,
    submission: PreparedSubmission,
) -> object:
    return service.finalize_result(
        actor,
        submission,
        llm_call_id="llm_unit",
        result=KnowledgeAnswer("不得覆盖的陈旧结果。"),
    )


def _needs_input_payload() -> dict[str, object]:
    parameters = dict(_valid_tool_payload()["candidate_parameters"])
    parameters["aging_temperature"] = None
    return {
        "route": "NEEDS_INPUT",
        "tool_id": "zta35g_sem_virtual_lab",
        "material": "ZTA35G",
        "candidate_parameters": parameters,
        "missing_fields": ["aging_temperature"],
        "ambiguous_fields": [],
        "follow_up_suggestion": "请补充。",
        "requested_outputs": ["sem_image"],
    }


def _assert_repeat_conflict(
    service: object,
    actor: ActorContext,
    submission: PreparedSubmission,
) -> None:
    with pytest.raises(Exception) as captured:
        _repeat_terminal(service, actor, submission)
    assert captured.value.code == "RESOURCE_CONFLICT"
    assert captured.value.conversation_id == submission.conversation_id
    assert captured.value.task_id == submission.task.task_id


def test_failed_call_with_succeeded_task_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: (_ for _ in ()).throw(
        ChatOrchestrationTimeoutError("PRIVATE timeout")
    ))
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    failed_task = store.tasks[submission.task.task_id]
    store.tasks[failed_task.task_id] = replace(
        failed_task,
        task_type="KNOWLEDGE_QA",
        current_status="SUCCEEDED",
        error_code=None,
        safe_error_message=None,
    )

    _assert_repeat_conflict(service, actor, submission)


def test_knowledge_without_assistant_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(
        factory,
        lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "答案。"},
    )
    service.orchestrate_submission(actor, submission)
    del store.messages["msg_assistant"]

    _assert_repeat_conflict(service, actor, submission)


def test_knowledge_with_revision_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(
        factory,
        lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "答案。"},
    )
    projection = service.orchestrate_submission(actor, submission)
    store.revisions["revision_unexpected"] = TaskInputRevision(
        task_input_revision_id="revision_unexpected",
        task_id=submission.task.task_id,
        request_id=submission.user_message.request_id,
        source_llm_call_id="llm_unit",
        source_message_ids=[submission.user_message.message_id],
        revision=1,
        raw_input={"unexpected": True},
        normalized_input={},
        missing_fields=[],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=projection.task.completed_at,
    )

    _assert_repeat_conflict(service, actor, submission)


def test_needs_input_without_revision_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _needs_input_payload())
    service.orchestrate_submission(actor, submission)
    store.revisions.clear()

    _assert_repeat_conflict(service, actor, submission)


def test_tool_unavailable_without_revision_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    store.revisions.clear()

    _assert_repeat_conflict(service, actor, submission)


def test_tool_unavailable_with_assistant_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    task = store.tasks[submission.task.task_id]
    store.messages["msg_unexpected"] = Message(
        message_id="msg_unexpected",
        conversation_id=submission.conversation_id,
        task_id=submission.task.task_id,
        actor_id=actor.actor_id,
        request_id=submission.user_message.request_id,
        role="ASSISTANT",
        generation_source="LLM",
        content_text="不应存在。",
        structured_content=None,
        llm_call_id="llm_unit",
        created_at=task.completed_at,
    )

    _assert_repeat_conflict(service, actor, submission)


def test_validation_failed_without_validation_errors_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    parameters = dict(_valid_tool_payload()["candidate_parameters"])
    parameters["solution_temperature"] = {"value": 1200, "unit": "°C"}
    service = _service(
        factory,
        lambda _: _valid_tool_payload(candidate_parameters=parameters),
    )
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    revision = next(iter(store.revisions.values()))
    store.revisions[revision.task_input_revision_id] = replace(
        revision,
        validation_errors=[],
    )

    _assert_repeat_conflict(service, actor, submission)


def test_two_revisions_for_same_call_are_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    revision = next(iter(store.revisions.values()))
    duplicate = replace(
        revision,
        task_input_revision_id="revision_duplicate",
    )
    store.revisions[duplicate.task_input_revision_id] = duplicate

    _assert_repeat_conflict(service, actor, submission)


def test_failed_call_and_task_error_mismatch_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: (_ for _ in ()).throw(
        ChatOrchestrationTimeoutError("PRIVATE timeout")
    ))
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    failed_task = store.tasks[submission.task.task_id]
    store.tasks[failed_task.task_id] = replace(
        failed_task,
        error_code="DIFFERENT_FAILURE",
    )

    _assert_repeat_conflict(service, actor, submission)


def test_cross_task_revision_bound_to_same_call_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)
    revision = next(iter(store.revisions.values()))
    cross_task_revision = replace(
        revision,
        task_input_revision_id="revision_cross_task",
        task_id="task_foreign",
    )
    store.revisions[
        cross_task_revision.task_input_revision_id
    ] = cross_task_revision

    _assert_repeat_conflict(service, actor, submission)
