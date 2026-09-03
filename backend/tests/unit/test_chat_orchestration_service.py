from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
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
    ChatOrchestrationOutcome,
    ChatOrchestrationRequestMetadata,
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
    idempotency_records: dict[str, object]


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

    def list_for_conversation_before(
        self,
        conversation_id: str,
        actor_id: str,
        *,
        before_created_at: datetime,
        before_message_id: str,
    ) -> list[Message]:
        return sorted(
            (
                message
                for message in self._store.messages.values()
                if message.conversation_id == conversation_id
                and message.actor_id == actor_id
                and (message.created_at, message.message_id)
                < (before_created_at, before_message_id)
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

    def get_owned_for_update(self, task_id: str, actor_id: str) -> Task | None:
        return self.get_owned(task_id, actor_id)

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


class _ToolRunRepository:
    def list_for_task(self, _task_id: str) -> list[object]:
        return []


class _IdempotencyRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get_by_first_request_id(self, request_id: str) -> object | None:
        return next(
            (
                record
                for record in self._store.idempotency_records.values()
                if record.first_request_id == request_id
            ),
            None,
        )

    def bind_task_input_revision(self, record, revision_id: str):
        if record.task_input_revision_id is not None:
            return None
        bound = replace(record, task_input_revision_id=revision_id)
        self._store.idempotency_records[record.idempotency_record_id] = bound
        return bound


class _UnitOfWork:
    def __init__(self, factory: _UnitOfWorkFactory) -> None:
        self._factory = factory
        self.conversations = _ConversationRepository(factory.store)
        self.messages = _MessageRepository(factory.store)
        self.tasks = _TaskRepository(factory.store)
        self.task_input_revisions = _RevisionRepository(factory.store)
        self.llm_calls = _LLMCallRepository(factory.store)
        self.tool_runs = _ToolRunRepository()
        self.idempotency_records = _IdempotencyRepository(factory.store)
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
            dict(self._factory.store.idempotency_records),
        )
        return self

    def __exit__(self, *_args: object) -> None:
        if not self._committed:
            self.rollback()
        self._factory.active -= 1

    def commit(self) -> None:
        self._factory.commit_count += 1
        if self._factory.commit_count == self._factory.uncertain_commit_call:
            self._committed = True
            assert self._factory.uncertain_commit_mutator is not None
            self._factory.uncertain_commit_mutator(self._factory.store)
            raise PersistenceError("Persistence operation failed.")
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
            idempotency_records,
        ) = self._snapshot
        self._factory.store.conversations = conversations
        self._factory.store.messages = messages
        self._factory.store.tasks = tasks
        self._factory.store.revisions = revisions
        self._factory.store.llm_calls = llm_calls
        self._factory.store.idempotency_records = idempotency_records


class _UnitOfWorkFactory:
    def __init__(
        self,
        store: _Store,
        *,
        fail_commit_calls: set[int] | None = None,
        uncertain_commit_call: int | None = None,
        uncertain_commit_mutator: Callable[[_Store], None] | None = None,
    ) -> None:
        self.store = store
        self.active = 0
        self.instances: list[_UnitOfWork] = []
        self.commit_count = 0
        self.fail_commit_calls = fail_commit_calls or set()
        self.uncertain_commit_call = uncertain_commit_call
        self.uncertain_commit_mutator = uncertain_commit_mutator

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


class _SequenceIdFactory:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        next_value = self._counts.get(prefix, 0) + 1
        self._counts[prefix] = next_value
        return f"{prefix}_{next_value}"


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
        idempotency_records={},
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
    def generic_responder(value: object) -> dict[str, object]:
        payload = responder(value)
        if payload.get("route") not in {"TOOL_EXECUTION", "NEEDS_INPUT"}:
            return payload
        candidate_input = {
            "material": payload["material"],
            **payload["candidate_parameters"],
            "requested_outputs": payload["requested_outputs"],
        }
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [{
                "tool_id": payload["tool_id"],
                "candidate_input_delta": candidate_input,
            }],
        }

    return _service_type()(
        factory,
        MockChatOrchestrationAdapter(generic_responder),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )


def test_replay_resumes_same_task_and_pending_call_after_pre_start_commit_failure() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store, fail_commit_calls={2})
    provider_calls = 0

    def responder(_value: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        return {"route": "KNOWLEDGE_ANSWER", "answer_text": "恢复后的答案。"}

    service = _service(factory, responder)

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "INTERNAL_ERROR"
    assert provider_calls == 0
    assert list(store.tasks) == [submission.task.task_id]
    assert list(store.messages) == [submission.user_message.message_id]
    assert len(store.llm_calls) == 1
    pending_call = next(iter(store.llm_calls.values()))
    assert pending_call.status == "PENDING"

    factory.fail_commit_calls.clear()
    recovered = service.resume_or_load_submission(actor, submission)

    assert provider_calls == 1
    assert recovered.task.task_id == submission.task.task_id
    assert recovered.llm_call.llm_call_id == pending_call.llm_call_id
    assert recovered.llm_call.status == "SUCCEEDED"
    assert len(store.tasks) == 1
    assert len([message for message in store.messages.values() if message.role == "USER"]) == 1


def _history_reference_case(
    content_text: str,
) -> tuple[
    ActorContext,
    PreparedSubmission,
    _Store,
    _UnitOfWorkFactory,
    object,
    list[tuple[str, object]],
]:
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.application.zta35g_tool import build_zta35g_tool_definition

    actor, submission, store = _submission()
    current_message = replace(
        submission.user_message,
        content_text=content_text,
    )
    submission = replace(submission, user_message=current_message)
    store.messages[current_message.message_id] = current_message

    original_definition = build_zta35g_tool_definition()
    normalization_calls: list[tuple[str, object]] = []

    def recorded_normalizer(candidate_input, prior_normalized_input=None):
        normalization_calls.append(("normalize", candidate_input))
        assert prior_normalized_input is None
        return original_definition.normalize(
            candidate_input,
            prior_normalized_input=prior_normalized_input,
        )

    registry = ToolRegistry(
        (replace(original_definition, normalizer=recorded_normalizer),)
    )
    definition = registry.resolve("zta35g_sem_virtual_lab")
    prior_time = BASE_TIME - timedelta(minutes=1)
    prior_task = Task(
        task_id="task_prior",
        conversation_id=submission.conversation_id,
        actor_id=actor.actor_id,
        task_type="TOOL_EXECUTION",
        current_status="READY",
        selected_tool_run_id=None,
        selected_result_id=None,
        created_at=prior_time,
        started_at=prior_time,
        updated_at=prior_time,
        completed_at=None,
        error_code=None,
        safe_error_message=None,
        tool_id=definition.tool_id,
        bound_tool_version=definition.version,
        bound_schema_hash=definition.schema_hash,
    )
    prior_message = Message.user(
        message_id="msg_prior",
        conversation_id=submission.conversation_id,
        task_id=prior_task.task_id,
        actor_id=actor.actor_id,
        request_id="req_prior",
        content_text="1000℃ 固溶2h，750℃时效4h。",
        created_at=prior_time,
    )
    prior_input = {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {"value": 2, "unit": "h"},
        "aging_temperature": {"value": 750, "unit": "°C"},
        "aging_time": {"value": 4, "unit": "h"},
        "requested_outputs": ["sem_image", "mechanical_properties"],
    }
    prior_revision = TaskInputRevision(
        task_input_revision_id="revision_prior",
        task_id=prior_task.task_id,
        request_id=prior_message.request_id,
        source_llm_call_id=None,
        source_message_ids=[prior_message.message_id],
        revision=1,
        raw_input=prior_input,
        normalized_input=prior_input,
        missing_fields=[],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=prior_time,
    )
    store.tasks[prior_task.task_id] = prior_task
    store.messages[prior_message.message_id] = prior_message
    store.revisions[prior_revision.task_input_revision_id] = prior_revision
    return (
        actor,
        submission,
        store,
        _UnitOfWorkFactory(store),
        registry,
        normalization_calls,
    )


def test_new_task_without_explicit_history_reference_does_not_inherit_parameters() -> None:
    (
        actor,
        submission,
        _store,
        factory,
        registry,
        normalization_calls,
    ) = _history_reference_case("帮我再做一个实验，固溶温度1050℃")
    delta = {
        "solution_temperature": {"value": 1050, "unit": "°C"},
        "requested_outputs": ["sem_image", "mechanical_properties"],
    }

    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(
            lambda command: {
                "route": "TOOL_CANDIDATES",
                "candidates": [
                    {
                        "tool_id": "zta35g_sem_virtual_lab",
                        "candidate_input_delta": delta,
                    }
                ],
            }
        ),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.revision is not None
    assert projection.revision.normalized_input == {
        "material": None,
        "solution_temperature": {"value": 1050, "unit": "°C"},
        "solution_time": None,
        "aging_temperature": None,
        "aging_time": None,
        "requested_outputs": ["sem_image", "mechanical_properties"],
    }
    assert set(projection.revision.missing_fields) == {
        "material",
        "solution_time",
        "aging_temperature",
        "aging_time",
    }
    assert normalization_calls == [("normalize", delta)]


def test_new_task_explicit_history_reference_merges_then_validates_and_authorizes() -> None:
    text = "沿用上一组条件，只把固溶温度改成1050℃"
    (
        actor,
        submission,
        _store,
        factory,
        registry,
        normalization_calls,
    ) = _history_reference_case(text)
    authorization_calls: list[object] = []
    original_authorize = registry.authorize

    class _RecordingRegistry:
        def routing_snapshot(self):
            return registry.routing_snapshot()

        def resolve(self, *args, **kwargs):
            return registry.resolve(*args, **kwargs)

        def authorize(self, **kwargs):
            authorization_calls.append(kwargs["action"])
            return original_authorize(**kwargs)

    recording_registry = _RecordingRegistry()

    def responder(command):
        assert len(command.context_window.recent_turns) == 1
        history_payload = json.loads(
            command.context_window.recent_turns[0].assistant_content
        )
        context_ref = history_payload["task_fact"]["context_ref"]
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "zta35g_sem_virtual_lab",
                    "candidate_input_delta": {
                        "solution_temperature": {"value": 1050, "unit": "°C"}
                    },
                    "history_reference": {
                        "context_ref": context_ref,
                        "reference_text": "沿用上一组条件",
                    },
                }
            ],
        }

    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(responder),
        tool_registry=recording_registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.current_status == "READY"
    assert projection.revision is not None
    normalized = projection.revision.normalized_input
    assert normalized == {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1050, "unit": "°C"},
        "solution_time": {"value": 2, "unit": "h"},
        "aging_temperature": {"value": 750, "unit": "°C"},
        "aging_time": {"value": 4, "unit": "h"},
        "requested_outputs": ["sem_image", "mechanical_properties"],
    }
    assert normalization_calls == [("normalize", normalized)]
    from materialsagent.domain.ports.tool_registry import ToolAction

    assert authorization_calls == [ToolAction.NEW_BINDING]
    definition = registry.resolve("zta35g_sem_virtual_lab")
    assert projection.task.bound_tool_ref == definition.ref
    assert definition.tool.validate_input(dict(normalized), seed=0)


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


def test_adapter_metadata_usage_and_request_id_are_persisted() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    class MetadataPort:
        provider = "deepseek"
        model_name = "deepseek-v4-flash"

        def request_metadata(
            self,
            orchestration_input: object,
        ) -> ChatOrchestrationRequestMetadata:
            assert factory.active == 0
            content = getattr(orchestration_input, "content_text")
            return ChatOrchestrationRequestMetadata(
                provider=self.provider,
                model_name=self.model_name,
                prompt_template_id="chat-orchestration",
                prompt_template_version="2",
                prompt_digest=sha256(content.encode("utf-8")).hexdigest(),
                generation_parameters={
                    "temperature": 0,
                    "max_tokens": 1024,
                    "thinking_mode": "disabled",
                    "response_format": "json_object",
                    "streaming": False,
                },
            )

        def orchestrate(
            self,
            _orchestration_input: object,
        ) -> ChatOrchestrationOutcome:
            assert factory.active == 0
            return ChatOrchestrationOutcome(
                result=KnowledgeAnswer("受控答案。"),
                usage={"input_tokens": 11, "output_tokens": 3},
                provider_request_id="deepseek-request-1",
            )

    service = _service_type()(
        factory,
        MetadataPort(),
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    service.orchestrate_submission(actor, submission)

    call = store.llm_calls["llm_unit"]
    assert call.provider == "deepseek"
    assert call.model_name == "deepseek-v4-flash"
    assert call.prompt_template_version == "2"
    assert call.generation_parameters["max_tokens"] == 1024
    assert call.usage == {"input_tokens": 11, "output_tokens": 3}
    assert call.provider_request_id == "deepseek-request-1"


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
    assert store.llm_calls["llm_unit"].structured_output_summary["route"] == (
        "TOOL_CANDIDATES"
    )


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
def test_complete_hard_invalid_candidate_is_persisted_as_validation_failure(
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
    revision = next(iter(store.revisions.values()))
    assert [error["code"] for error in revision.validation_errors] == [expected_code]
    assert store.llm_calls["llm_unit"].status == "SUCCEEDED"
    assert store.llm_calls["llm_unit"].error_code is None
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert store.tasks[submission.task.task_id].error_code == "VALIDATION_FAILED"
    assert store.tasks[submission.task.task_id].tool_id == "zta35g_sem_virtual_lab"
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

    projection = _service(
        factory,
        lambda _: _valid_tool_payload(candidate_parameters=parameters),
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "READY"
    revision = next(iter(store.revisions.values()))
    assert revision.raw_input["solution_time"] == {
        "value": minutes,
        "unit": "min",
    }
    assert revision.normalized_input["solution_time"] == {
        "value": expected_hours,
        "unit": "h",
    }


def test_complete_valid_tool_candidate_is_bound_and_ready() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)

    projection = _service(factory, lambda _: _valid_tool_payload()).orchestrate_submission(
        actor,
        submission,
    )
    revision = next(iter(store.revisions.values()))
    assert revision.revision == 1
    assert revision.validation_errors == []
    assert store.llm_calls["llm_unit"].status == "SUCCEEDED"
    task = store.tasks[submission.task.task_id]
    assert task.task_type == "TOOL_EXECUTION"
    assert task.current_status == "READY"
    assert task.tool_id == "zta35g_sem_virtual_lab"
    assert task.error_code is None
    assert task.completed_at is None
    assert task.selected_tool_run_id is None
    assert task.selected_result_id is None
    assert len(store.messages) == 1


def test_complete_valid_tool_candidate_stays_running_when_m7_chain_is_enabled() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(
            lambda _: {
                "route": "TOOL_CANDIDATES",
                "candidates": [{
                    "tool_id": "zta35g_sem_virtual_lab",
                    "candidate_input_delta": {
                        "material": "ZTA35G",
                        **_valid_tool_payload()["candidate_parameters"],
                        "requested_outputs": ["sem_image", "mechanical_properties"],
                    },
                }],
            }
        ),
        clock=_SequenceClock(),
        id_factory=_id_factory,
        tool_chain_enabled=True,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.task_type == "TOOL_EXECUTION"
    assert projection.task.current_status == "READY"
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

    assert captured.value.status_code == 500
    assert captured.value.code == "AGENT_INTERNAL_ERROR"
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

    def request_metadata(
        self,
        orchestration_input: object,
    ) -> ChatOrchestrationRequestMetadata:
        content = getattr(orchestration_input, "content_text")
        return ChatOrchestrationRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id="replaceable-chat",
            prompt_template_version="1",
            prompt_digest=sha256(content.encode("utf-8")).hexdigest(),
            generation_parameters={"temperature": 0, "max_tokens": 256},
        )

    def orchestrate(self, _orchestration_input: object) -> object:
        if isinstance(self._result_or_error, Exception):
            raise self._result_or_error
        return self._result_or_error


@pytest.mark.parametrize(
    ("result_or_error", "expected_status", "expected_code"),
    [
        (RuntimeError("PRIVATE replacement failure"), 503, "CHAT_ORCHESTRATION_FAILED"),
        (object(), 500, "AGENT_INTERNAL_ERROR"),
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
            500,
            "AGENT_INTERNAL_ERROR",
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


def test_provider_failure_persists_detailed_llm_error_but_public_task_error() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    port = _UnnormalizedPort(
        ChatOrchestrationProviderError(
            error_code="LLM_AUTHENTICATION_FAILED",
            safe_error_message="LLM provider authentication failed.",
            provider_request_id="failure-request-1",
        )
    )
    service = _service_type()(
        factory,
        port,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.status_code == 503
    call = store.llm_calls["llm_unit"]
    task = store.tasks[submission.task.task_id]
    assert call.error_code == "LLM_AUTHENTICATION_FAILED"
    assert call.safe_error_message == "LLM provider authentication failed."
    assert call.provider_request_id == "failure-request-1"
    assert task.error_code == "CHAT_ORCHESTRATION_FAILED"


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
    service.orchestrate_submission(actor, submission)
    store.revisions.clear()

    _assert_repeat_conflict(service, actor, submission)


def test_tool_unavailable_with_assistant_is_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
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
        created_at=task.updated_at,
    )

    _assert_repeat_conflict(service, actor, submission)


def test_normalizer_validation_failure_with_revision_is_stable_on_repeat() -> None:
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
    assert len(store.revisions) == 1

    repeated = service.finalize_result(
        actor,
        submission,
        llm_call_id="llm_unit",
        result=KnowledgeAnswer("stale"),
    )
    assert repeated.task.error_code == "VALIDATION_FAILED"


def test_two_revisions_for_same_call_are_rejected_on_repeat() -> None:
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    service = _service(factory, lambda _: _valid_tool_payload())
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


class _RoutingRegistrySpy:
    def __init__(
        self,
        normalizations: dict[str, object],
        *,
        denied: tuple[str, ...] = (),
    ) -> None:
        from types import SimpleNamespace

        from materialsagent.domain.ports.tool_registry import (
            RoutingCatalogEntry,
            RoutingCatalogSnapshot,
            ToolRef,
        )

        self.events: list[str] = []
        self._denied = set(denied)
        self._snapshot = RoutingCatalogSnapshot(
            entries=tuple(
                RoutingCatalogEntry(
                    tool_id=tool_id,
                    version="1",
                    schema_hash=(str(index + 1) * 64),
                    display_name=tool_id,
                    description=f"Route to {tool_id}.",
                    input_schema={"type": "object"},
                    candidate_input_schema={
                        "type": "object",
                        "properties": {"value": {"type": ["string", "null"]}},
                    },
                    supported_outputs=("result",),
                )
                for index, tool_id in enumerate(normalizations)
            )
        )
        self._definitions = {}
        for entry in self._snapshot.entries:
            result = normalizations[entry.tool_id]

            def normalize(
                candidate_input,
                prior_normalized_input=None,
                *,
                _result=result,
                _tool_id=entry.tool_id,
            ):
                self.events.append("normalize")
                assert prior_normalized_input is None
                assert candidate_input == {"value": _tool_id}
                return _result

            self._definitions[entry.tool_id] = SimpleNamespace(
                ref=ToolRef(entry.tool_id, entry.version, entry.schema_hash),
                normalize=normalize,
                normalizer=normalize,
                supported_outputs=("result",),
                candidate_input_schema=entry.candidate_input_schema,
            )

    def routing_snapshot(self):
        self.events.append("catalog")
        return self._snapshot

    def resolve(self, tool_id, *, snapshot=None):
        from materialsagent.application.tool_registry import UnknownToolError

        self.events.append(f"resolve:{tool_id}")
        definition = self._definitions.get(tool_id)
        matching_entries = (
            ()
            if snapshot is None
            else tuple(
                entry for entry in snapshot.entries if entry.tool_id == tool_id
            )
        )
        if definition is None or (
            snapshot is not None
            and (
                len(matching_entries) != 1
                or matching_entries[0].ref != definition.ref
            )
        ):
            raise UnknownToolError("Unknown Tool.")
        return definition

    def authorize(self, *, registration, action, bound_ref):
        from materialsagent.application.tool_registry import (
            ToolAuthorizationDenialReason,
            ToolAuthorizationError,
        )
        from materialsagent.domain.ports.tool_registry import (
            AuthorizationDecision,
            ExecutionPolicy,
            ToolAction,
        )

        current_ref = registration.ref
        self.events.append(f"authorize:{current_ref.tool_id}")
        if current_ref.tool_id in self._denied:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.POLICY_ACTION_DENIED
            )
        if action is ToolAction.NEW_BINDING:
            if bound_ref is not None:
                raise ToolAuthorizationError(
                    ToolAuthorizationDenialReason.BOUND_TOOL_MISMATCH
                )
        elif bound_ref is None or bound_ref.tool_id != current_ref.tool_id:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.BOUND_TOOL_MISMATCH
            )
        elif bound_ref.schema_hash != current_ref.schema_hash:
            raise ToolAuthorizationError(
                ToolAuthorizationDenialReason.SCHEMA_DRIFT
            )
        return AuthorizationDecision(current_ref, ExecutionPolicy.ANY_TASK)


def _generic_route_service(factory, registry, responder):
    def recorded(orchestration_input):
        assert factory.active == 0
        events = getattr(registry, "events", None)
        if isinstance(events, list):
            events.append("router")
        return responder(orchestration_input)

    return _service_type()(
        factory,
        MockChatOrchestrationAdapter(recorded),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )


def test_unique_complete_candidate_resolves_normalizes_then_binds_ready() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "zta35g_sem_virtual_lab": ReadyNormalization(
                normalized_input={
                    "value": "normalized",
                    "requested_outputs": ["result"],
                },
                requested_outputs=("result",),
            )
        }
    )
    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "zta35g_sem_virtual_lab",
                    "candidate_input_delta": {"value": "zta35g_sem_virtual_lab"},
                }
            ],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "READY"
    assert projection.task.tool_id == "zta35g_sem_virtual_lab"
    assert projection.revision.normalized_input == {
        "value": "normalized",
        "requested_outputs": ["result"],
    }
    assert registry.events == [
        "catalog",
        "router",
        "resolve:zta35g_sem_virtual_lab",
        "normalize",
        "authorize:zta35g_sem_virtual_lab",
    ]


def test_unique_incomplete_candidate_binds_before_needs_input_persistence() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "zta35g_sem_virtual_lab": NeedsInputNormalization(
                normalized_input={"value": None, "requested_outputs": ["result"]},
                missing_fields=("value",),
                ambiguous_fields=(),
                follow_up_suggestion="Provide value.",
            )
        }
    )
    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "zta35g_sem_virtual_lab",
                    "candidate_input_delta": {"value": "zta35g_sem_virtual_lab"},
                }
            ],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.task.tool_id == "zta35g_sem_virtual_lab"
    assert projection.revision.missing_fields == ["value"]


def test_multiple_valid_candidates_remain_unbound_without_normalization() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    ready = ReadyNormalization(
        normalized_input={"unused": True, "requested_outputs": ["result"]},
        requested_outputs=("result",),
    )
    registry = _RoutingRegistrySpy({"tool_one": ready, "tool_two": ready})
    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}},
                {"tool_id": "tool_two", "candidate_input_delta": {"value": "tool_two"}},
            ],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.task.tool_id is None
    assert len(projection.revision.candidate_tool_refs) == 2
    assert "normalize" not in registry.events


@pytest.mark.parametrize("unresolvable_kind", ["unknown_id", "mismatched_ref"])
def test_any_unresolvable_candidate_rejects_the_entire_candidate_set(
    unresolvable_kind: str,
) -> None:
    from materialsagent.domain.ports.tool_registry import (
        ReadyNormalization,
        RoutingCatalogSnapshot,
    )

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    ready = ReadyNormalization(
        normalized_input={"value": "normalized", "requested_outputs": ["result"]},
        requested_outputs=("result",),
    )
    registry = _RoutingRegistrySpy({"tool_one": ready, "tool_two": ready})
    rejected_tool_id = "unknown_tool"
    if unresolvable_kind == "mismatched_ref":
        rejected_tool_id = "tool_two"
        first, second = registry._snapshot.entries
        registry._snapshot = RoutingCatalogSnapshot(
            entries=(first, replace(second, version="stale-snapshot-version")),
        )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}},
                {
                    "tool_id": rejected_tool_id,
                    "candidate_input_delta": {"value": rejected_tool_id},
                },
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    task = store.tasks[submission.task.task_id]
    assert task.bound_tool_ref is None
    assert task.selected_tool_run_id is None
    assert task.error_code == "AGENT_INTERNAL_ERROR"
    assert store.llm_calls["llm_unit"].error_code == "LLM_SCHEMA_MISMATCH"
    assert store.revisions == {}
    assert "normalize" not in registry.events


def test_knowledge_answer_never_resolves_normalizes_or_binds() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "Answer."},
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "SUCCEEDED"
    assert projection.task.tool_id is None
    assert registry.events == ["catalog", "router"]


def _disabled_only_ml_registry():
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.domain.ports.tool_registry import (
        ExecutionPolicy,
        ToolStatus,
    )
    from backend.tests.support.heterogeneous_tools import (
        build_ml_training_test_definition,
    )

    return ToolRegistry(
        (
            replace(
                build_ml_training_test_definition(),
                status=ToolStatus.DISABLED,
                execution_policy=ExecutionPolicy.NONE,
            ),
        )
    )


def test_knowledge_answer_is_audited_with_an_empty_routing_catalog() -> None:
    actor, submission, store = _submission()
    registry = _disabled_only_ml_registry()

    projection = _generic_route_service(
        _UnitOfWorkFactory(store),
        registry,
        lambda orchestration_input: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": (
                "No active Tool is needed to answer this knowledge question."
            ),
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "SUCCEEDED"
    assert projection.task.bound_tool_ref is None
    assert projection.llm_call is not None
    assert projection.llm_call.catalog_snapshot_refs == ()
    assert projection.llm_call.catalog_hash == sha256(b"[]").hexdigest()
    assert projection.llm_call.structured_output_summary["route"] == (
        "KNOWLEDGE_ANSWER"
    )


def test_tool_candidate_cannot_resolve_from_an_empty_routing_catalog() -> None:
    actor, submission, store = _submission()
    service = _generic_route_service(
        _UnitOfWorkFactory(store),
        _disabled_only_ml_registry(),
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "ml_training_test",
                    "candidate_input_delta": {
                        "dataset": "dataset_fixture_1",
                        "task_type": "regression",
                        "split_ratio": 0.8,
                        "target_column": "yield_strength",
                        "shuffle": True,
                    },
                }
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.tasks[submission.task.task_id].bound_tool_ref is None
    assert store.tasks[submission.task.task_id].selected_tool_run_id is None
    assert store.revisions == {}
    assert store.llm_calls["llm_unit"].catalog_snapshot_refs == ()
    assert store.llm_calls["llm_unit"].error_code == "LLM_SCHEMA_MISMATCH"


def test_unknown_candidate_becomes_agent_internal_error_without_binding() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "known_tool": ReadyNormalization(
                normalized_input={"requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "unknown_tool", "candidate_input_delta": {"value": "unknown_tool"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    task = store.tasks[submission.task.task_id]
    call = store.llm_calls["llm_unit"]
    assert task.tool_id is None
    assert task.error_code == "AGENT_INTERNAL_ERROR"
    assert call.error_code == "LLM_SCHEMA_MISMATCH"
    assert store.revisions == {}


def test_new_binding_is_normalized_before_current_authorization_denial() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "disabled_tool": ReadyNormalization(
                normalized_input={"requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        },
        denied=("disabled_tool",),
    )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "disabled_tool",
                    "candidate_input_delta": {"value": "disabled_tool"},
                }
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.tasks[submission.task.task_id].tool_id is None
    assert store.llm_calls["llm_unit"].error_code == "LLM_SCHEMA_MISMATCH"
    assert registry.events[-2:] == ["normalize", "authorize:disabled_tool"]


def test_inconsistent_ready_normalization_is_rejected_before_binding() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "normalized", "requested_outputs": ["other"]},
                requested_outputs=("result",),
            )
        }
    )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.tasks[submission.task.task_id].bound_tool_ref is None
    assert store.revisions == {}


def test_ready_normalization_cannot_omit_schema_declared_outputs() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "normalized"},
                requested_outputs=("result",),
            )
        }
    )
    registry._definitions["tool_one"].input_schema = {
        "type": "object",
        "properties": {"requested_outputs": {"type": "array"}},
    }
    service = _generic_route_service(
        _UnitOfWorkFactory(store),
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.tasks[submission.task.task_id].bound_tool_ref is None
    assert store.revisions == {}


@pytest.mark.parametrize(
    ("mutation", "normalized_input", "missing_fields", "follow_up"),
    [
        (
            "non_json",
            {"value": None, "unsafe": float("nan"), "requested_outputs": ("result",)},
            ("value",),
            "Clarify.",
        ),
        (
            "empty_unresolved",
            {"value": None, "requested_outputs": ("result",)},
            (),
            "Clarify.",
        ),
        (
            "blank_follow_up",
            {"value": None, "requested_outputs": ("result",)},
            ("value",),
            "   ",
        ),
    ],
)
def test_forged_invalid_needs_input_is_rejected_before_binding(
    mutation: str,
    normalized_input: dict[str, object],
    missing_fields: tuple[str, ...],
    follow_up: str,
) -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    forged = object.__new__(NeedsInputNormalization)
    object.__setattr__(forged, "normalized_input", normalized_input)
    object.__setattr__(forged, "missing_fields", missing_fields)
    object.__setattr__(forged, "ambiguous_fields", ())
    object.__setattr__(forged, "follow_up_suggestion", follow_up)
    actor, submission, store = _submission()
    service = _generic_route_service(
        _UnitOfWorkFactory(store),
        _RoutingRegistrySpy({"tool_one": forged}),
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert mutation
    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.tasks[submission.task.task_id].bound_tool_ref is None
    assert store.revisions == {}


def test_incomplete_forged_normalization_maps_to_controlled_schema_mismatch() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    forged = object.__new__(NeedsInputNormalization)
    object.__setattr__(
        forged,
        "normalized_input",
        {"value": None, "requested_outputs": ("result",)},
    )
    object.__setattr__(forged, "missing_fields", ("value",))
    object.__setattr__(forged, "ambiguous_fields", ())
    actor, submission, store = _submission()
    service = _generic_route_service(
        _UnitOfWorkFactory(store),
        _RoutingRegistrySpy({"tool_one": forged}),
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert store.llm_calls["llm_unit"].error_code == "LLM_SCHEMA_MISMATCH"
    assert store.tasks[submission.task.task_id].bound_tool_ref is None
    assert store.revisions == {}


@pytest.mark.parametrize("difference", ["binding", "structured_content"])
def test_uncertain_knowledge_recovery_rejects_extra_terminal_state(
    difference: str,
) -> None:
    actor, submission, store = _submission()

    def mutate(committed: _Store) -> None:
        if difference == "binding":
            task = committed.tasks[submission.task.task_id]
            committed.tasks[task.task_id] = replace(
                task,
                tool_id="tool_one",
                bound_tool_version="1",
                bound_schema_hash="1" * 64,
            )
            return
        assistant = committed.messages["msg_assistant"]
        committed.messages[assistant.message_id] = replace(
            assistant,
            structured_content={"unexpected": True},
        )

    factory = _UnitOfWorkFactory(
        store,
        uncertain_commit_call=3,
        uncertain_commit_mutator=mutate,
    )

    with pytest.raises(Exception) as captured:
        _service(
            factory,
            lambda _: {
                "route": "KNOWLEDGE_ANSWER",
                "answer_text": "受控答案。",
            },
        ).orchestrate_submission(actor, submission)

    assert captured.value.code == "RESOURCE_CONFLICT"


@pytest.mark.parametrize("mutation", ["binding", "revision", "state"])
def test_uncertain_finalize_rejects_different_committed_tool_route(mutation: str) -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()

    def mutate(committed: _Store) -> None:
        task = committed.tasks[submission.task.task_id]
        revision = committed.revisions["revision_unit"]
        if mutation == "binding":
            committed.tasks[task.task_id] = replace(
                task,
                tool_id="tool_two",
                bound_tool_version="1",
                bound_schema_hash="2" * 64,
            )
            return
        if mutation == "revision":
            committed.revisions["revision_unit"] = replace(
                revision,
                normalized_input={"value": "different", "requested_outputs": ["result"]},
            )
            return
        committed.tasks[task.task_id] = replace(task, current_status="NEEDS_INPUT")
        committed.revisions["revision_unit"] = replace(
            revision,
            normalized_input={"value": None, "requested_outputs": ["result"]},
            missing_fields=["value"],
        )
        committed.messages["msg_assistant"] = Message(
            message_id="msg_assistant",
            conversation_id=submission.conversation_id,
            task_id=submission.task.task_id,
            actor_id=actor.actor_id,
            request_id=submission.user_message.request_id,
            role="ASSISTANT",
            generation_source="LLM",
            content_text="请补充缺失参数或明确存在歧义的参数。",
            structured_content=None,
            llm_call_id="llm_unit",
            created_at=BASE_TIME + timedelta(seconds=10),
        )

    factory = _UnitOfWorkFactory(
        store,
        uncertain_commit_call=3,
        uncertain_commit_mutator=mutate,
    )
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "normalized", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            ),
            "tool_two": ReadyNormalization(
                normalized_input={"value": "other", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            ),
        }
    )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "RESOURCE_CONFLICT"


def test_uncertain_finalize_recovers_exact_committed_tool_route() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(
        store,
        uncertain_commit_call=3,
        uncertain_commit_mutator=lambda _store: None,
    )
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "normalized", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )

    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "READY"
    assert projection.task.tool_id == "tool_one"
    assert projection.revision.normalized_input == {
        "value": "normalized",
        "requested_outputs": ["result"],
    }


def test_uncertain_finalize_recovers_schema_less_fixed_output_tool_route() -> None:
    from materialsagent.application.tool_registry import ToolRegistry
    from backend.tests.support.heterogeneous_tools import (
        build_ml_training_test_definition,
    )

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(
        store,
        uncertain_commit_call=3,
        uncertain_commit_mutator=lambda _store: None,
    )
    registry = ToolRegistry((build_ml_training_test_definition(),))
    candidate_input = {
        "dataset": "dataset_fixture_1",
        "task_type": "regression",
        "split_ratio": 0.8,
        "target_column": "yield_strength",
        "shuffle": True,
    }

    projection = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "ml_training_test",
                    "candidate_input_delta": candidate_input,
                }
            ],
        },
    ).orchestrate_submission(actor, submission)

    assert projection.task.current_status == "READY"
    assert projection.task.bound_tool_ref == registry.resolve(
        "ml_training_test"
    ).ref
    assert projection.revision is not None
    assert projection.revision.normalized_input == candidate_input
    assert "requested_outputs" not in projection.revision.normalized_input


def test_uncertain_bound_needs_input_rejects_extra_assistant_structure() -> None:
    from materialsagent.application.chat_orchestration import _ResolvedToolRoute
    from materialsagent.domain.ports.chat_orchestration import (
        ToolCandidateProposal,
        ToolCandidateSet,
    )
    from materialsagent.domain.ports.tool_registry import (
        NeedsInputNormalization,
        ToolRef,
    )

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    normalization = NeedsInputNormalization(
        normalized_input={"value": None, "requested_outputs": ["result"]},
        missing_fields=("value",),
        ambiguous_fields=(),
        follow_up_suggestion="Provide value.",
    )
    service = _generic_route_service(
        factory,
        _RoutingRegistrySpy({"tool_one": normalization}),
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )
    service.orchestrate_submission(actor, submission)
    assistant = store.messages["msg_assistant"]
    store.messages[assistant.message_id] = replace(
        assistant,
        structured_content={"unexpected": True},
    )
    proposal_set = ToolCandidateSet(
        (ToolCandidateProposal("tool_one", {"value": "tool_one"}),)
    )
    with pytest.raises(Exception) as captured:
        service._recover_successful_finalize(
            actor,
            submission,
            llm_call_id="llm_unit",
            result=_ResolvedToolRoute(
                proposal_set=proposal_set,
                candidate_refs=(ToolRef("tool_one", "1", "1" * 64),),
                selected_ref=ToolRef("tool_one", "1", "1" * 64),
                selected_input=proposal_set.candidates[0].candidate_input,
                normalization=normalization,
            ),
            usage=None,
            provider_request_id=None,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"


@pytest.mark.parametrize("difference", ["binding", "revision", "outputs", "state"])
def test_uncertain_recovery_compares_full_intended_resolved_route(difference: str) -> None:
    from materialsagent.application.chat_orchestration import _ResolvedToolRoute
    from materialsagent.domain.ports.chat_orchestration import (
        ToolCandidateProposal,
        ToolCandidateSet,
    )
    from materialsagent.domain.ports.tool_registry import (
        NeedsInputNormalization,
        ReadyNormalization,
        ToolRef,
    )

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    original_ref = ToolRef("tool_one", "1", "1" * 64)
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "normalized", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}}
            ],
        },
    )
    service.orchestrate_submission(actor, submission)

    normalization = ReadyNormalization(
        normalized_input={"value": "normalized", "requested_outputs": ["result"]},
        requested_outputs=("result",),
    )
    selected_ref = original_ref
    if difference == "binding":
        selected_ref = ToolRef("tool_two", "1", "2" * 64)
    elif difference == "revision":
        normalization = ReadyNormalization(
            normalized_input={"value": "different", "requested_outputs": ["result"]},
            requested_outputs=("result",),
        )
    elif difference == "outputs":
        normalization = ReadyNormalization(
            normalized_input={"value": "normalized", "requested_outputs": ["result"]},
            requested_outputs=("other",),
        )
    elif difference == "state":
        normalization = NeedsInputNormalization(
            normalized_input={"value": None, "requested_outputs": ["result"]},
            missing_fields=("value",),
            ambiguous_fields=(),
            follow_up_suggestion="Provide value.",
        )
    proposal_set = ToolCandidateSet(
        (ToolCandidateProposal("tool_one", {"value": "tool_one"}),)
    )
    intended = _ResolvedToolRoute(
        proposal_set=proposal_set,
        candidate_refs=(original_ref,),
        selected_ref=selected_ref,
        selected_input=proposal_set.candidates[0].candidate_input,
        normalization=normalization,
    )
    with pytest.raises(Exception) as captured:
        service._recover_successful_finalize(
            actor,
            submission,
            llm_call_id="llm_unit",
            result=intended,
            usage=None,
            provider_request_id=None,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"


@pytest.mark.parametrize(
    "difference",
    ["candidate_refs", "follow_up", "structured_content"],
)
def test_uncertain_recovery_compares_ambiguous_refs_and_follow_up(difference: str) -> None:
    from materialsagent.application.chat_orchestration import _ResolvedToolRoute
    from materialsagent.domain.ports.chat_orchestration import (
        ToolCandidateProposal,
        ToolCandidateSet,
    )
    from materialsagent.domain.ports.tool_registry import ReadyNormalization, ToolRef

    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    ready = ReadyNormalization(
        normalized_input={"value": "unused", "requested_outputs": ["result"]},
        requested_outputs=("result",),
    )
    registry = _RoutingRegistrySpy({"tool_one": ready, "tool_two": ready})
    service = _generic_route_service(
        factory,
        registry,
        lambda _: {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {"tool_id": "tool_one", "candidate_input_delta": {"value": "tool_one"}},
                {"tool_id": "tool_two", "candidate_input_delta": {"value": "tool_two"}},
            ],
        },
    )
    service.orchestrate_submission(actor, submission)

    refs = (
        ToolRef("tool_one", "1", "1" * 64),
        ToolRef("tool_two", "1", "2" * 64),
    )
    if difference == "candidate_refs":
        store.revisions["revision_unit"] = replace(
            store.revisions["revision_unit"],
            candidate_tool_refs=(refs[0], ToolRef("tool_three", "1", "3" * 64)),
        )
    elif difference == "follow_up":
        store.messages["msg_assistant"] = replace(
            store.messages["msg_assistant"],
            content_text="Different follow-up.",
        )
    else:
        store.messages["msg_assistant"] = replace(
            store.messages["msg_assistant"],
            structured_content={"unexpected": True},
        )
    proposal_set = ToolCandidateSet(
        (
            ToolCandidateProposal("tool_one", {"value": "tool_one"}),
            ToolCandidateProposal("tool_two", {"value": "tool_two"}),
        )
    )
    intended = _ResolvedToolRoute(
        proposal_set=proposal_set,
        candidate_refs=refs,
        selected_ref=None,
        selected_input=None,
        normalization=None,
    )

    with pytest.raises(Exception) as captured:
        service._recover_successful_finalize(
            actor,
            submission,
            llm_call_id="llm_unit",
            result=intended,
            usage=None,
            provider_request_id=None,
        )

    assert captured.value.code == "RESOURCE_CONFLICT"


def test_bound_needs_input_supplement_never_invokes_first_route_router() -> None:
    try:
        extraction = __import__(
            "materialsagent.domain.ports.tool_input_extraction",
            fromlist=["ToolInputExtractionOutcome", "ToolInputExtractionRequestMetadata"],
        )
    except ModuleNotFoundError:
        pytest.fail("The fixed-Tool input extraction protocol is not implemented.")

    from materialsagent.domain.ports.tool_registry import (
        NeedsInputNormalization,
        ReadyNormalization,
        ToolRef,
    )

    actor, submission, store = _submission()
    bound_ref = ToolRef("tool_one", "1", "1" * 64)
    task = replace(
        submission.task,
        task_type="TOOL_EXECUTION",
        current_status="NEEDS_INPUT",
        tool_id=bound_ref.tool_id,
        bound_tool_version=bound_ref.version,
        bound_schema_hash=bound_ref.schema_hash,
    )
    prior = TaskInputRevision(
        task_input_revision_id="revision_prior",
        task_id=task.task_id,
        request_id="req_prior",
        source_llm_call_id="llm_prior",
        source_message_ids=[submission.user_message.message_id],
        revision=1,
        raw_input={"value": None},
        normalized_input={"value": None, "requested_outputs": ["result"]},
        missing_fields=["value"],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE_TIME,
    )
    supplement_message = Message.user(
        message_id="msg_supplement",
        conversation_id=task.conversation_id,
        task_id=task.task_id,
        actor_id=task.actor_id,
        request_id="req_supplement",
        content_text="value is complete",
        created_at=BASE_TIME + timedelta(seconds=1),
    )
    store.tasks[task.task_id] = task
    store.messages[supplement_message.message_id] = supplement_message
    store.revisions[prior.task_input_revision_id] = prior
    from materialsagent.domain.models.idempotency_record import IdempotencyRecord

    record = IdempotencyRecord(
        idempotency_record_id="idem_supplement",
        actor_id=actor.actor_id,
        operation="TASK_INPUT_SUPPLEMENT",
        idempotency_key="supplement-key",
        request_digest="f" * 64,
        first_request_id=supplement_message.request_id,
        task_id=task.task_id,
        message_id=supplement_message.message_id,
        task_input_revision_id=None,
        tool_run_id=None,
        explanation_id=None,
        created_at=supplement_message.created_at,
        expires_at=None,
    )
    store.idempotency_records[record.idempotency_record_id] = record
    supplement = PreparedSubmission(
        conversation_id=task.conversation_id,
        user_message=supplement_message,
        task=task,
        submission_mode="SUPPLEMENT_TASK",
        idempotency_record_id="idem_supplement",
    )
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "tool_one": NeedsInputNormalization(
                normalized_input={"value": None, "requested_outputs": ["result"]},
                missing_fields=("value",),
                ambiguous_fields=(),
                follow_up_suggestion="Provide value.",
            )
        }
    )

    class RouterMustNotRun:
        provider = "mock"
        model_name = "mock-chat-orchestration-v1"

        def request_metadata(self, _value):
            pytest.fail("A bound supplement rebuilt the routing catalog.")

        def orchestrate(self, _value):
            pytest.fail("A bound supplement invoked the first-route Router.")

    class Extractor:
        provider = "mock"
        model_name = "mock-tool-input-v1"

        def request_metadata(self, _value):
            return extraction.ToolInputExtractionRequestMetadata(
                provider=self.provider,
                model_name=self.model_name,
                prompt_template_id="tool-input-extraction",
                prompt_template_version="1",
                prompt_digest="a" * 64,
                generation_parameters={"temperature": 0, "max_tokens": 256},
            )

        def extract(self, command):
            assert factory.active == 0
            registry.events.append("extract")
            assert command.tool_context_ref == bound_ref
            assert command.candidate_input_schema == {
                "type": "object",
                "properties": {"value": {"type": ("string", "null")}},
            }
            assert command.missing_fields == ("value",)
            assert command.ambiguous_fields == ()
            assert command.content_text == "value is complete"
            return extraction.ToolInputExtractionOutcome(
                candidate_input_delta={"value": "complete"},
                request_metadata=self.request_metadata(command),
            )

    definition = registry._definitions["tool_one"]
    original_normalize = definition.normalize

    def normalize(delta, prior_normalized_input=None):
        registry.events.append("normalize")
        assert delta == {"value": "complete"}
        assert prior_normalized_input == {
            "value": None,
            "requested_outputs": ["result"],
        }
        return ReadyNormalization(
            normalized_input={"value": "complete", "requested_outputs": ["result"]},
            requested_outputs=("result",),
        )

    definition.normalize = normalize
    definition.normalizer = normalize
    service = _service_type()(
        factory,
        RouterMustNotRun(),
        tool_input_extraction_port=Extractor(),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, supplement)

    assert original_normalize is not None
    assert projection.task.current_status == "READY"
    assert projection.task.bound_tool_ref == bound_ref
    assert projection.revision.revision == 2
    assert prior.revision == 1
    assert projection.revision.raw_input == {"value": "complete"}
    assert registry.events == [
        "resolve:tool_one",
        "authorize:tool_one",
        "extract",
        "normalize",
    ]
    saved_call = store.llm_calls["llm_unit"]
    assert saved_call.purpose == "TOOL_INPUT_EXTRACTION"
    assert saved_call.tool_context_ref == {
        "tool_id": "tool_one",
        "version": "1",
        "schema_hash": "1" * 64,
    }
    assert saved_call.catalog_snapshot_refs is None
    assert saved_call.catalog_hash is None
    assert saved_call.structured_output_summary == {
        "candidate_input_delta": {"value": "complete"}
    }
    assert store.idempotency_records[
        "idem_supplement"
    ].task_input_revision_id == projection.revision.task_input_revision_id
    replayed = service.load_current_submission(actor, supplement)
    assert replayed.task == projection.task
    assert replayed.llm_call == saved_call
    assert replayed.revision == projection.revision


def _bound_supplement_case():
    from materialsagent.domain.models.idempotency_record import IdempotencyRecord
    from materialsagent.domain.ports.tool_registry import ToolRef

    actor, original, store = _submission()
    bound_ref = ToolRef("tool_one", "1", "1" * 64)
    task = replace(
        original.task,
        task_type="TOOL_EXECUTION",
        current_status="NEEDS_INPUT",
        tool_id=bound_ref.tool_id,
        bound_tool_version=bound_ref.version,
        bound_schema_hash=bound_ref.schema_hash,
    )
    prior = TaskInputRevision(
        task_input_revision_id="revision_prior",
        task_id=task.task_id,
        request_id="req_prior",
        source_llm_call_id="llm_prior",
        source_message_ids=[original.user_message.message_id],
        revision=1,
        raw_input={"value": None},
        normalized_input={"value": None, "requested_outputs": ["result"]},
        missing_fields=["value"],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE_TIME,
    )
    message = Message.user(
        message_id="msg_supplement",
        conversation_id=task.conversation_id,
        task_id=task.task_id,
        actor_id=task.actor_id,
        request_id="req_supplement",
        content_text="value is complete",
        created_at=BASE_TIME + timedelta(seconds=1),
    )
    record = IdempotencyRecord(
        idempotency_record_id="idem_supplement",
        actor_id=actor.actor_id,
        operation="TASK_INPUT_SUPPLEMENT",
        idempotency_key="supplement-key",
        request_digest="f" * 64,
        first_request_id=message.request_id,
        task_id=task.task_id,
        message_id=message.message_id,
        task_input_revision_id=None,
        tool_run_id=None,
        explanation_id=None,
        created_at=message.created_at,
        expires_at=None,
    )
    store.tasks[task.task_id] = task
    store.messages[message.message_id] = message
    store.revisions[prior.task_input_revision_id] = prior
    store.idempotency_records[record.idempotency_record_id] = record
    return (
        actor,
        PreparedSubmission(
            conversation_id=task.conversation_id,
            user_message=message,
            task=task,
            submission_mode="SUPPLEMENT_TASK",
            idempotency_record_id=record.idempotency_record_id,
        ),
        store,
        prior,
        bound_ref,
    )


class _RecordingToolInputExtractor:
    provider = "mock"
    model_name = "mock-tool-input-v1"

    def __init__(self, events, delta=None, before_return=None):
        self.events = events
        self.delta = {"value": "complete"} if delta is None else delta
        self.before_return = before_return
        self.calls = 0
        self.commands = []

    def request_metadata(self, _command):
        from materialsagent.domain.ports.tool_input_extraction import (
            ToolInputExtractionRequestMetadata,
        )

        return ToolInputExtractionRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id="tool-input-extraction",
            prompt_template_version="1",
            prompt_digest="a" * 64,
            generation_parameters={"temperature": 0, "max_tokens": 256},
        )

    def extract(self, command):
        from materialsagent.domain.ports.tool_input_extraction import (
            ToolInputExtractionOutcome,
            ToolInputExtractionRequestMetadata,
        )

        self.calls += 1
        self.commands.append(command)
        self.events.append("extract")
        if self.before_return is not None:
            self.before_return(command)
        return ToolInputExtractionOutcome(
            candidate_input_delta=self.delta,
            request_metadata=ToolInputExtractionRequestMetadata(
                provider=self.provider,
                model_name=self.model_name,
                prompt_template_id="tool-input-extraction",
                prompt_template_version="1",
                prompt_digest="a" * 64,
                generation_parameters={"temperature": 0, "max_tokens": 256},
            ),
        )


class _RouterMustNotRun:
    provider = "mock"
    model_name = "mock-chat-orchestration-v1"

    def request_metadata(self, _value):
        pytest.fail("A bound supplement rebuilt a routing catalog.")

    def orchestrate(self, _value):
        pytest.fail("A bound supplement invoked the first-route Router.")


def test_registry_schema_drift_denial_blocks_supplement_before_extraction() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization, ToolRef

    actor, submission, store, prior, _ = _bound_supplement_case()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": NeedsInputNormalization(
                normalized_input={"value": None, "requested_outputs": ["result"]},
                missing_fields=("value",),
                ambiguous_fields=(),
                follow_up_suggestion="Provide value.",
            )
        }
    )
    registry._definitions["tool_one"].ref = ToolRef(
        "tool_one", "2", "2" * 64
    )
    extractor = _RecordingToolInputExtractor(registry.events)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "TOOL_SCHEMA_DRIFT"
    assert "新任务" in str(captured.value)
    assert registry.events == ["resolve:tool_one", "authorize:tool_one"]
    assert extractor.calls == 0
    assert list(store.revisions.values()) == [prior]
    assert store.llm_calls == {}


def test_supplement_policy_denial_blocks_before_extraction() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, submission, store, prior, _ = _bound_supplement_case()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": NeedsInputNormalization(
                normalized_input={"value": None, "requested_outputs": ["result"]},
                missing_fields=("value",),
                ambiguous_fields=(),
                follow_up_suggestion="Provide value.",
            )
        },
        denied=("tool_one",),
    )
    extractor = _RecordingToolInputExtractor(registry.events)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "TOOL_EXECUTION_NOT_ALLOWED"
    assert registry.events == ["resolve:tool_one", "authorize:tool_one"]
    assert extractor.calls == 0
    assert list(store.revisions.values()) == [prior]
    assert store.llm_calls == {}


def test_supplement_rechecks_latest_revision_after_external_extraction() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store, prior, bound_ref = _bound_supplement_case()
    original_task = store.tasks[submission.task.task_id]
    factory = _UnitOfWorkFactory(store)
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "complete", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    definition = registry._definitions["tool_one"]

    def normalize(_delta, prior_normalized_input=None):
        assert prior_normalized_input == prior.normalized_input
        return ReadyNormalization(
            normalized_input={"value": "complete", "requested_outputs": ["result"]},
            requested_outputs=("result",),
        )

    definition.normalize = normalize
    definition.normalizer = normalize

    competing_uows: list[_UnitOfWork] = []

    def concurrent_revision(_command):
        competing = factory()
        competing_uows.append(competing)
        with competing:
            locked = competing.tasks.get_owned_for_update(
                submission.task.task_id,
                actor.actor_id,
            )
            assert locked is not None
            competing.task_input_revisions.add(
                replace(
                    prior,
                    task_input_revision_id="revision_concurrent",
                    request_id="req_concurrent",
                    source_llm_call_id="llm_concurrent",
                    revision=2,
                    raw_input={"value": None},
                    normalized_input={
                        "value": None,
                        "requested_outputs": ["result"],
                    },
                    missing_fields=["value"],
                )
            )
            updated = replace(
                locked,
                updated_at=BASE_TIME + timedelta(seconds=2),
            )
            assert competing.tasks.update(
                updated,
                expected_status="NEEDS_INPUT",
            ) == updated
            competing.commit()

    extractor = _RecordingToolInputExtractor(
        registry.events,
        before_return=concurrent_revision,
    )
    service = _service_type()(
        factory,
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "RESOURCE_CONFLICT"
    assert competing_uows and competing_uows[0]._committed
    assert len(factory.instances) >= 5
    assert factory.active == 0
    assert len(store.revisions) == 2
    assert all(
        revision.request_id != submission.user_message.request_id
        for revision in store.revisions.values()
    )
    assert len(store.llm_calls) == 1
    assert store.llm_calls["llm_unit"].status == "RUNNING"
    assert store.tasks[submission.task.task_id].bound_tool_ref == bound_ref
    assert original_task.bound_tool_ref == bound_ref


def test_invalid_supplement_normalization_is_audited_without_new_revision() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store, prior, _ = _bound_supplement_case()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "complete", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    definition = registry._definitions["tool_one"]
    definition.normalize = lambda *_args, **_kwargs: object()
    definition.normalizer = definition.normalize
    extractor = _RecordingToolInputExtractor(registry.events)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert list(store.revisions.values()) == [prior]
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert len(store.llm_calls) == 1
    failed_call = next(iter(store.llm_calls.values()))
    assert failed_call.purpose == "TOOL_INPUT_EXTRACTION"
    assert failed_call.status == "FAILED"
    assert failed_call.error_code == "LLM_SCHEMA_MISMATCH"
    assert failed_call.structured_output_summary is None


@pytest.mark.parametrize("exception_type", [AttributeError, OverflowError])
def test_supplement_normalizer_runtime_validation_errors_are_controlled(
    exception_type: type[Exception],
) -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization

    actor, submission, store, prior, _ = _bound_supplement_case()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={
                    "value": "complete",
                    "requested_outputs": ["result"],
                },
                requested_outputs=("result",),
            )
        }
    )
    definition = registry._definitions["tool_one"]

    def fail_normalization(*_args, **_kwargs):
        raise exception_type("PRIVATE invalid normalizer output")

    definition.normalize = fail_normalization
    definition.normalizer = fail_normalization
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=_RecordingToolInputExtractor(
            registry.events
        ),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert "PRIVATE" not in str(captured.value)
    assert list(store.revisions.values()) == [prior]
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert len(store.llm_calls) == 1
    failed_call = next(iter(store.llm_calls.values()))
    assert failed_call.purpose == "TOOL_INPUT_EXTRACTION"
    assert failed_call.status == "FAILED"
    assert failed_call.error_code == "LLM_SCHEMA_MISMATCH"
    assert failed_call.structured_output_summary is None


def test_still_ambiguous_supplement_preserves_prior_controlled_candidates() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, submission, store, prior, _ = _bound_supplement_case()
    ambiguous_prior = replace(
        prior,
        missing_fields=[],
        ambiguous_fields=[{"field": "value", "candidates": [2, 3]}],
    )
    store.revisions[prior.task_input_revision_id] = ambiguous_prior
    registry = _RoutingRegistrySpy(
        {
            "tool_one": NeedsInputNormalization(
                normalized_input={"value": None, "requested_outputs": ["result"]},
                missing_fields=(),
                ambiguous_fields=("value",),
                follow_up_suggestion="Choose a value.",
            )
        }
    )
    definition = registry._definitions["tool_one"]

    def normalize(delta, prior_normalized_input=None):
        assert delta == {"value": {"candidates": [2, 3]}}
        assert prior_normalized_input == ambiguous_prior.normalized_input
        return NeedsInputNormalization(
            normalized_input={"value": None, "requested_outputs": ["result"]},
            missing_fields=(),
            ambiguous_fields=("value",),
            follow_up_suggestion="Choose a value.",
        )

    definition.normalize = normalize
    definition.normalizer = normalize
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=_RecordingToolInputExtractor(
            registry.events,
            delta={},
        ),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.revision.revision == 2
    assert projection.revision.ambiguous_fields == [
        {"field": "value", "candidates": [2, 3]}
    ]


@pytest.mark.parametrize(
    ("controlled_candidates", "misleading_candidates"),
    [
        ([1, 0], [True, False]),
        ([True, False], [1, 0]),
        (
            [{"nested": [1.0]}, {"nested": [2]}],
            [{"nested": [1]}, {"nested": [2.0]}],
        ),
    ],
)
def test_ambiguity_history_uses_type_sensitive_standard_json_equivalence(
    controlled_candidates: list[object],
    misleading_candidates: list[object],
) -> None:
    from materialsagent.application.chat_orchestration import (
        ChatOrchestrationService,
        _BoundSupplementSnapshot,
    )
    from materialsagent.domain.ports.tool_registry import ToolRef

    _, _, _, prior, _ = _bound_supplement_case()
    exact_raw = {
        "value": {"candidates": controlled_candidates},
        "unit": "h",
    }
    misleading_raw = {
        "value": {"candidates": misleading_candidates},
        "unit": "h",
    }
    older_exact_revision = replace(
        prior,
        task_input_revision_id="revision_older_exact",
        revision=1,
        raw_input={
            "value": {
                "legacy": {"candidates": controlled_candidates},
                "marker": "older exact shape",
            }
        },
    )
    exact_revision = replace(
        prior,
        task_input_revision_id="revision_exact",
        revision=2,
        raw_input={"value": exact_raw},
    )
    misleading_revision = replace(
        prior,
        task_input_revision_id="revision_misleading",
        revision=3,
        raw_input={"value": misleading_raw},
    )
    latest_revision = replace(
        prior,
        task_input_revision_id="revision_latest",
        revision=4,
        raw_input={"other": "new delta"},
        missing_fields=[],
        ambiguous_fields=[
            {"field": "value", "candidates": controlled_candidates}
        ],
    )
    snapshot = _BoundSupplementSnapshot(
        bound_tool_ref=ToolRef("tool_one", "1", "1" * 64),
        latest_revision=latest_revision,
        revision_history=(
            older_exact_revision,
            exact_revision,
            misleading_revision,
            latest_revision,
        ),
        prior_normalized_input=latest_revision.normalized_input,
    )

    reconstructed = ChatOrchestrationService._supplement_normalization_delta(
        {},
        snapshot,
    )

    assert json.dumps(
        reconstructed,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        {"value": exact_raw},
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("controlled_candidates", "misleading_candidates"),
    [
        ([1, 2], [True, 2]),
        ([1, 2], [1.0, 2.0]),
    ],
)
def test_service_skips_newer_type_mismatch_for_real_zta_normalizer(
    controlled_candidates: list[object],
    misleading_candidates: list[object],
) -> None:
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.application.zta35g_tool import (
        build_zta35g_tool_definition,
    )
    from materialsagent.domain.ports.tool_registry import (
        NeedsInputNormalization,
        ToolRef,
    )

    actor, submission, store, prior, _ = _bound_supplement_case()
    base_definition = build_zta35g_tool_definition()
    real_normalizer = base_definition.normalizer
    assert real_normalizer is not None
    observed_normalizer_inputs: list[dict[str, object]] = []

    def recording_real_normalizer(candidate_input, prior_normalized_input=None):
        observed_normalizer_inputs.append(
            json.loads(json.dumps(candidate_input, ensure_ascii=False))
        )
        return real_normalizer(candidate_input, prior_normalized_input)

    registry = ToolRegistry(
        (
            replace(
                base_definition,
                version="2",
                normalizer=recording_real_normalizer,
            ),
        )
    )
    current = registry.resolve("zta35g_sem_virtual_lab")
    original_binding = ToolRef(
        current.ref.tool_id,
        "1",
        current.ref.schema_hash,
    )
    exact_solution_time = {
        "value": {"candidates": controlled_candidates},
        "unit": "h",
    }
    initial_candidate = {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": exact_solution_time,
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3, "unit": "h"},
        "requested_outputs": ["sem_image"],
    }
    initial = base_definition.normalize(initial_candidate)
    assert isinstance(initial, NeedsInputNormalization)
    latest = base_definition.normalize(
        {
            "solution_time": exact_solution_time,
            "aging_temperature": {"value": 740, "unit": "°C"},
        },
        prior_normalized_input=initial.normalized_input,
    )
    assert isinstance(latest, NeedsInputNormalization)
    bound_task = replace(
        store.tasks[submission.task.task_id],
        tool_id=original_binding.tool_id,
        bound_tool_version=original_binding.version,
        bound_schema_hash=original_binding.schema_hash,
    )
    exact_revision = replace(
        prior,
        task_input_revision_id="revision_exact_zta",
        revision=1,
        raw_input=initial_candidate,
        normalized_input=dict(initial.normalized_input),
        missing_fields=[],
        ambiguous_fields=[
            {
                "field": "solution_time",
                "candidates": controlled_candidates,
            }
        ],
    )
    misleading_revision = replace(
        exact_revision,
        task_input_revision_id="revision_misleading_zta",
        request_id="req_misleading_zta",
        source_llm_call_id="llm_misleading_zta",
        revision=2,
        raw_input={
            "solution_time": {
                "value": {"candidates": misleading_candidates},
                "unit": "h",
            }
        },
        ambiguous_fields=[
            {
                "field": "solution_time",
                "candidates": misleading_candidates,
            }
        ],
        created_at=BASE_TIME + timedelta(seconds=1),
    )
    latest_revision = replace(
        exact_revision,
        task_input_revision_id="revision_latest_zta",
        request_id="req_latest_zta",
        source_llm_call_id="llm_latest_zta",
        revision=3,
        raw_input={
            "aging_temperature": {"value": 740, "unit": "°C"}
        },
        normalized_input=dict(latest.normalized_input),
        created_at=BASE_TIME + timedelta(seconds=2),
    )
    store.tasks[bound_task.task_id] = bound_task
    store.revisions = {
        item.task_input_revision_id: item
        for item in (
            exact_revision,
            misleading_revision,
            latest_revision,
        )
    }
    submission = replace(submission, task=bound_task)
    provider_delta = {"aging_time": {"value": 4, "unit": "h"}}
    extractor = _RecordingToolInputExtractor([], delta=provider_delta)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert json.dumps(
        observed_normalizer_inputs,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        [
            {
                "aging_time": {"value": 4, "unit": "h"},
                "solution_time": exact_solution_time,
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    command = extractor.commands[0]
    assert command.tool_context_ref == current.ref
    assert not hasattr(command, "prior_normalized_input")
    assert not hasattr(command, "raw_input")
    assert projection.task.bound_tool_ref == original_binding
    assert projection.llm_call.tool_context_ref == {
        "tool_id": current.ref.tool_id,
        "version": current.ref.version,
        "schema_hash": current.ref.schema_hash,
    }
    assert projection.llm_call.structured_output_summary == {
        "candidate_input_delta": provider_delta
    }
    assert projection.revision.raw_input == provider_delta
    assert projection.revision.revision == 4
    assert projection.revision.ambiguous_fields == [
        {"field": "solution_time", "candidates": controlled_candidates}
    ]


def test_zta35g_supplement_keeps_untouched_prior_ambiguity_for_real_normalizer() -> None:
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.application.zta35g_tool import (
        build_zta35g_tool_definition,
    )
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, submission, store, prior, _ = _bound_supplement_case()
    definition = build_zta35g_tool_definition()
    registry = ToolRegistry((definition,))
    current = registry.resolve("zta35g_sem_virtual_lab")
    candidate = {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {
            "value": {"candidates": [2, 3]},
            "unit": "h",
        },
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3, "unit": "h"},
        "requested_outputs": ["sem_image"],
    }
    initial = current.normalize(candidate)
    assert isinstance(initial, NeedsInputNormalization)
    bound_task = replace(
        store.tasks[submission.task.task_id],
        tool_id=current.ref.tool_id,
        bound_tool_version=current.ref.version,
        bound_schema_hash=current.ref.schema_hash,
    )
    zta_prior = replace(
        prior,
        raw_input=candidate,
        normalized_input=dict(initial.normalized_input),
        missing_fields=[],
        ambiguous_fields=[
            {"field": "solution_time", "candidates": [2, 3]}
        ],
    )
    store.tasks[bound_task.task_id] = bound_task
    store.revisions[prior.task_input_revision_id] = zta_prior
    supplement = replace(submission, task=bound_task)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=_RecordingToolInputExtractor(
            [],
            delta={
                "aging_temperature": {"value": 740, "unit": "°C"}
            },
        ),
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, supplement)

    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.task.bound_tool_ref == current.ref
    assert projection.revision.raw_input == {
        "aging_temperature": {"value": 740, "unit": "°C"}
    }
    assert projection.revision.normalized_input["aging_temperature"] == {
        "value": 740,
        "unit": "°C",
    }
    assert projection.revision.normalized_input["solution_time"] is None
    assert projection.revision.ambiguous_fields == [
        {"field": "solution_time", "candidates": [2, 3]}
    ]


def test_two_zta35g_supplements_recover_older_untouched_ambiguity_shape() -> None:
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.application.zta35g_tool import (
        build_zta35g_tool_definition,
    )
    from materialsagent.domain.models.idempotency_record import IdempotencyRecord
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, first_submission, store, prior, _ = _bound_supplement_case()
    registry = ToolRegistry((build_zta35g_tool_definition(),))
    current = registry.resolve("zta35g_sem_virtual_lab")
    initial_candidate = {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {
            "value": {"candidates": [2, 3]},
            "unit": "h",
        },
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3, "unit": "h"},
        "requested_outputs": ["sem_image"],
    }
    initial = current.normalize(initial_candidate)
    assert isinstance(initial, NeedsInputNormalization)
    bound_task = replace(
        store.tasks[first_submission.task.task_id],
        tool_id=current.ref.tool_id,
        bound_tool_version=current.ref.version,
        bound_schema_hash=current.ref.schema_hash,
    )
    store.tasks[bound_task.task_id] = bound_task
    store.revisions[prior.task_input_revision_id] = replace(
        prior,
        raw_input=initial_candidate,
        normalized_input=dict(initial.normalized_input),
        missing_fields=[],
        ambiguous_fields=[
            {"field": "solution_time", "candidates": [2, 3]}
        ],
    )
    first_submission = replace(first_submission, task=bound_task)
    extractor = _RecordingToolInputExtractor(
        [],
        delta={"aging_temperature": {"value": 740, "unit": "°C"}},
    )
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_SequenceIdFactory(),
    )

    first = service.orchestrate_submission(actor, first_submission)

    second_message = Message.user(
        message_id="msg_second_supplement",
        conversation_id=first.task.conversation_id,
        task_id=first.task.task_id,
        actor_id=actor.actor_id,
        request_id="req_second_supplement",
        content_text="aging time is 4 h",
        created_at=BASE_TIME + timedelta(seconds=5),
    )
    second_record = IdempotencyRecord(
        idempotency_record_id="idem_second_supplement",
        actor_id=actor.actor_id,
        operation="TASK_INPUT_SUPPLEMENT",
        idempotency_key="second-supplement-key",
        request_digest="e" * 64,
        first_request_id=second_message.request_id,
        task_id=first.task.task_id,
        message_id=second_message.message_id,
        task_input_revision_id=None,
        tool_run_id=None,
        explanation_id=None,
        created_at=second_message.created_at,
        expires_at=None,
    )
    store.messages[second_message.message_id] = second_message
    store.idempotency_records[
        second_record.idempotency_record_id
    ] = second_record
    second_submission = PreparedSubmission(
        conversation_id=first.task.conversation_id,
        user_message=second_message,
        task=first.task,
        submission_mode="SUPPLEMENT_TASK",
        idempotency_record_id=second_record.idempotency_record_id,
    )
    extractor.delta = {"aging_time": {"value": 4, "unit": "h"}}

    second = service.orchestrate_submission(actor, second_submission)

    assert first.revision.raw_input == {
        "aging_temperature": {"value": 740, "unit": "°C"}
    }
    assert second.revision.raw_input == {
        "aging_time": {"value": 4, "unit": "h"}
    }
    assert second.revision.revision == 3
    assert second.revision.normalized_input["aging_temperature"] == {
        "value": 740,
        "unit": "°C",
    }
    assert second.revision.normalized_input["aging_time"] == {
        "value": 4,
        "unit": "h",
    }
    assert second.revision.ambiguous_fields == [
        {"field": "solution_time", "candidates": [2, 3]}
    ]
    assert [
        call.structured_output_summary
        for call in store.llm_calls.values()
    ] == [
        {
            "candidate_input_delta": {
                "aging_temperature": {"value": 740, "unit": "°C"}
            }
        },
        {
            "candidate_input_delta": {
                "aging_time": {"value": 4, "unit": "h"}
            }
        },
    ]


def test_same_schema_new_tool_version_supplements_without_rewriting_binding() -> None:
    from materialsagent.domain.ports.tool_registry import ReadyNormalization, ToolRef

    actor, submission, store, prior, bound_ref = _bound_supplement_case()
    registry = _RoutingRegistrySpy(
        {
            "tool_one": ReadyNormalization(
                normalized_input={"value": "complete", "requested_outputs": ["result"]},
                requested_outputs=("result",),
            )
        }
    )
    definition = registry._definitions["tool_one"]
    current_ref = ToolRef("tool_one", "2", bound_ref.schema_hash)
    definition.ref = current_ref

    def normalize(delta, prior_normalized_input=None):
        assert delta == {"value": "complete"}
        assert prior_normalized_input == prior.normalized_input
        return ReadyNormalization(
            normalized_input={"value": "complete", "requested_outputs": ["result"]},
            requested_outputs=("result",),
        )

    definition.normalize = normalize
    definition.normalizer = normalize
    extractor = _RecordingToolInputExtractor(registry.events)
    service = _service_type()(
        _UnitOfWorkFactory(store),
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert projection.task.bound_tool_ref == bound_ref
    assert extractor.commands[0].tool_context_ref == current_ref
    assert projection.llm_call.tool_context_ref == {
        "tool_id": current_ref.tool_id,
        "version": current_ref.version,
        "schema_hash": current_ref.schema_hash,
    }


def _uncertain_supplement_service(
    store: _Store,
    mutate: Callable[[_Store], None],
    *,
    invalid_normalizer: bool = False,
):
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    normalization = NeedsInputNormalization(
        normalized_input={"value": None, "requested_outputs": ["result"]},
        missing_fields=("value",),
        ambiguous_fields=(),
        follow_up_suggestion="Provide value.",
    )
    registry = _RoutingRegistrySpy({"tool_one": normalization})
    definition = registry._definitions["tool_one"]
    if invalid_normalizer:
        definition.normalize = lambda *_args, **_kwargs: object()
    else:
        definition.normalize = lambda *_args, **_kwargs: normalization
    definition.normalizer = definition.normalize
    extractor = _RecordingToolInputExtractor(registry.events)
    factory = _UnitOfWorkFactory(
        store,
        uncertain_commit_call=3,
        uncertain_commit_mutator=mutate,
    )
    service = _service_type()(
        factory,
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )
    return service, extractor


def test_supplement_replay_resumes_same_task_and_pending_extraction_call() -> None:
    from materialsagent.domain.ports.tool_registry import NeedsInputNormalization

    actor, submission, store, _prior, _bound_ref = _bound_supplement_case()
    normalization = NeedsInputNormalization(
        normalized_input={"value": None, "requested_outputs": ["result"]},
        missing_fields=("value",),
        ambiguous_fields=(),
        follow_up_suggestion="Provide value.",
    )
    registry = _RoutingRegistrySpy({"tool_one": normalization})
    definition = registry._definitions["tool_one"]
    definition.normalize = lambda *_args, **_kwargs: normalization
    definition.normalizer = definition.normalize
    extractor = _RecordingToolInputExtractor(registry.events)
    factory = _UnitOfWorkFactory(store, fail_commit_calls={2})
    service = _service_type()(
        factory,
        _RouterMustNotRun(),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=_id_factory,
    )

    with pytest.raises(Exception):
        service.orchestrate_submission(actor, submission)

    assert extractor.calls == 0
    assert len(store.tasks) == 1
    assert len([message for message in store.messages.values() if message.role == "USER"]) == 2
    assert sum(
        message.message_id == submission.user_message.message_id
        for message in store.messages.values()
    ) == 1
    pending_call = store.llm_calls["llm_unit"]
    assert pending_call.status == "PENDING"

    factory.fail_commit_calls.clear()
    projection = service.resume_or_load_submission(actor, submission)

    assert extractor.calls == 1
    assert projection.task.task_id == submission.task.task_id
    assert projection.llm_call.llm_call_id == pending_call.llm_call_id
    assert projection.llm_call.status == "SUCCEEDED"
    assert len(store.tasks) == 1
    assert len([message for message in store.messages.values() if message.role == "USER"]) == 2
    assert sum(
        message.message_id == submission.user_message.message_id
        for message in store.messages.values()
    ) == 1


def test_uncertain_supplement_success_recovers_exact_committed_projection() -> None:
    actor, submission, store, prior, bound_ref = _bound_supplement_case()
    service, extractor = _uncertain_supplement_service(
        store,
        lambda _store: None,
    )

    projection = service.orchestrate_submission(actor, submission)

    assert extractor.calls == 1
    assert projection.task.current_status == "NEEDS_INPUT"
    assert projection.task.bound_tool_ref == bound_ref
    assert projection.llm_call.status == "SUCCEEDED"
    assert projection.llm_call.purpose == "TOOL_INPUT_EXTRACTION"
    assert projection.revision.revision == prior.revision + 1
    assert projection.assistant_message == store.messages["msg_assistant"]
    assert store.idempotency_records[
        "idem_supplement"
    ].task_input_revision_id == projection.revision.task_input_revision_id
    assert service.load_current_submission(actor, submission) == projection


@pytest.mark.parametrize(
    "difference",
    ["call", "task", "revision", "assistant", "idempotency"],
)
def test_uncertain_supplement_success_rejects_mismatched_committed_facts(
    difference: str,
) -> None:
    actor, submission, store, _, _ = _bound_supplement_case()

    def mutate(committed: _Store) -> None:
        if difference == "call":
            committed.llm_calls["llm_unit"] = replace(
                committed.llm_calls["llm_unit"],
                provider_request_id="different-provider-request",
            )
        elif difference == "task":
            committed.tasks[submission.task.task_id] = replace(
                committed.tasks[submission.task.task_id],
                bound_tool_version="different-version",
            )
        elif difference == "revision":
            committed.revisions["revision_unit"] = replace(
                committed.revisions["revision_unit"],
                raw_input={"value": "different"},
            )
        elif difference == "assistant":
            committed.messages["msg_assistant"] = replace(
                committed.messages["msg_assistant"],
                content_text="Different follow-up.",
            )
        else:
            committed.idempotency_records["idem_supplement"] = replace(
                committed.idempotency_records["idem_supplement"],
                task_input_revision_id="revision_different",
            )

    service, _ = _uncertain_supplement_service(store, mutate)

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "RESOURCE_CONFLICT"


def test_uncertain_supplement_failure_recovers_exact_failed_audit() -> None:
    actor, submission, store, prior, bound_ref = _bound_supplement_case()
    service, extractor = _uncertain_supplement_service(
        store,
        lambda _store: None,
        invalid_normalizer=True,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "AGENT_INTERNAL_ERROR"
    assert extractor.calls == 1
    assert store.tasks[submission.task.task_id].current_status == "FAILED"
    assert store.tasks[submission.task.task_id].bound_tool_ref == bound_ref
    assert store.llm_calls["llm_unit"].purpose == "TOOL_INPUT_EXTRACTION"
    assert store.llm_calls["llm_unit"].status == "FAILED"
    assert list(store.revisions.values()) == [prior]
    assert store.idempotency_records[
        "idem_supplement"
    ].task_input_revision_id is None
    replay = service.load_current_submission(actor, submission)
    assert replay.llm_call == store.llm_calls["llm_unit"]
    assert replay.revision is None
    assert replay.assistant_message is None


@pytest.mark.parametrize(
    "difference",
    ["call", "task", "revision", "assistant", "idempotency"],
)
def test_uncertain_supplement_failure_rejects_mismatched_committed_facts(
    difference: str,
) -> None:
    actor, submission, store, prior, _ = _bound_supplement_case()

    def mutate(committed: _Store) -> None:
        if difference == "call":
            committed.llm_calls["llm_unit"] = replace(
                committed.llm_calls["llm_unit"],
                safe_error_message="Different safe failure.",
            )
        elif difference == "task":
            committed.tasks[submission.task.task_id] = replace(
                committed.tasks[submission.task.task_id],
                bound_tool_version="different-version",
            )
        elif difference == "revision":
            committed.revisions["revision_unexpected"] = replace(
                prior,
                task_input_revision_id="revision_unexpected",
                request_id=submission.user_message.request_id,
                source_llm_call_id="llm_unit",
                revision=2,
            )
        elif difference == "assistant":
            committed.messages["msg_assistant"] = Message(
                message_id="msg_assistant",
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
                actor_id=actor.actor_id,
                request_id=submission.user_message.request_id,
                role="ASSISTANT",
                generation_source="LLM",
                content_text="Unexpected follow-up.",
                structured_content=None,
                llm_call_id="llm_unit",
                created_at=BASE_TIME + timedelta(seconds=2),
            )
        else:
            committed.idempotency_records["idem_supplement"] = replace(
                committed.idempotency_records["idem_supplement"],
                task_input_revision_id="revision_unexpected",
            )

    service, _ = _uncertain_supplement_service(
        store,
        mutate,
        invalid_normalizer=True,
    )

    with pytest.raises(Exception) as captured:
        service.orchestrate_submission(actor, submission)

    assert captured.value.code == "RESOURCE_CONFLICT"


def test_ml_candidate_binds_once_and_fixed_tool_supplement_becomes_ready() -> None:
    from materialsagent.application.tool_registry import ToolRegistry
    from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
    from materialsagent.domain.models.idempotency_record import IdempotencyRecord
    from backend.tests.support.heterogeneous_tools import (
        build_ml_training_test_definition,
    )

    normalization_calls: list[tuple[dict[str, object], dict[str, object] | None]] = []

    def record_normalization(candidate, prior):
        normalization_calls.append(
            (
                dict(candidate),
                None if prior is None else dict(prior),
            )
        )

    def forbidden_zta_normalizer(*_args, **_kwargs):
        pytest.fail("The ML route invoked the ZTA35G normalizer.")

    zta = replace(
        build_zta35g_tool_definition(),
        normalizer=forbidden_zta_normalizer,
    )
    ml = build_ml_training_test_definition(
        normalization_observer=record_normalization,
    )
    registry = ToolRegistry((zta, ml))
    actor, submission, store = _submission()
    factory = _UnitOfWorkFactory(store)
    ids = _SequenceIdFactory()
    router_calls: list[object] = []

    def route_to_incomplete_ml(orchestration_input):
        assert factory.active == 0
        router_calls.append(orchestration_input)
        assert {
            entry.tool_id for entry in orchestration_input.routing_catalog.entries
        } == {"zta35g_sem_virtual_lab", "ml_training_test"}
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "ml_training_test",
                    "candidate_input_delta": {
                        "dataset": "dataset_fixture_1",
                        "task_type": "regression",
                        "split_ratio": None,
                        "target_column": "yield_strength",
                        "shuffle": True,
                    },
                }
            ],
        }

    extraction_events: list[str] = []
    extractor = _RecordingToolInputExtractor(
        extraction_events,
        delta={"split_ratio": 0.8},
    )
    service = _service_type()(
        factory,
        MockChatOrchestrationAdapter(route_to_incomplete_ml),
        tool_input_extraction_port=extractor,
        tool_registry=registry,
        clock=_SequenceClock(),
        id_factory=ids,
    )

    first = service.orchestrate_submission(actor, submission)

    ml_ref = registry.resolve("ml_training_test").ref
    assert first.task.current_status == "NEEDS_INPUT"
    assert first.task.bound_tool_ref == ml_ref
    assert first.revision is not None
    assert first.revision.normalized_input == {
        "dataset": "dataset_fixture_1",
        "task_type": "regression",
        "split_ratio": None,
        "target_column": "yield_strength",
        "shuffle": True,
    }
    assert first.revision.missing_fields == ["split_ratio"]

    supplement_message = Message.user(
        message_id="msg_ml_supplement",
        conversation_id=first.task.conversation_id,
        task_id=first.task.task_id,
        actor_id=first.task.actor_id,
        request_id="req_ml_supplement",
        content_text="Use an 80 percent training split.",
        created_at=BASE_TIME + timedelta(seconds=30),
    )
    record = IdempotencyRecord(
        idempotency_record_id="idem_ml_supplement",
        actor_id=actor.actor_id,
        operation="TASK_INPUT_SUPPLEMENT",
        idempotency_key="ml-supplement-key",
        request_digest="d" * 64,
        first_request_id=supplement_message.request_id,
        task_id=first.task.task_id,
        message_id=supplement_message.message_id,
        task_input_revision_id=None,
        tool_run_id=None,
        explanation_id=None,
        created_at=supplement_message.created_at,
        expires_at=None,
    )
    store.messages[supplement_message.message_id] = supplement_message
    store.idempotency_records[record.idempotency_record_id] = record
    supplement = PreparedSubmission(
        conversation_id=first.task.conversation_id,
        user_message=supplement_message,
        task=first.task,
        submission_mode="SUPPLEMENT_TASK",
        idempotency_record_id=record.idempotency_record_id,
    )

    completed = service.orchestrate_submission(actor, supplement)

    assert completed.task.current_status == "READY"
    assert completed.task.bound_tool_ref == ml_ref
    assert completed.revision is not None
    assert completed.revision.revision == 2
    assert completed.revision.normalized_input == {
        "dataset": "dataset_fixture_1",
        "task_type": "regression",
        "split_ratio": 0.8,
        "target_column": "yield_strength",
        "shuffle": True,
    }
    assert len(router_calls) == 1
    assert extraction_events == ["extract"]
    assert normalization_calls == [
        (
            {
                "dataset": "dataset_fixture_1",
                "task_type": "regression",
                "split_ratio": None,
                "target_column": "yield_strength",
                "shuffle": True,
            },
            None,
        ),
        (
            {"split_ratio": 0.8},
            {
                "dataset": "dataset_fixture_1",
                "task_type": "regression",
                "split_ratio": None,
                "target_column": "yield_strength",
                "shuffle": True,
            },
        ),
    ]
