from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from materialsagent.application.idempotency import (
    IdempotencyOutcome,
    TASK_CREATE,
    TASK_INPUT_SUPPLEMENT,
    canonical_request_digest,
    recovered_idempotency_outcome,
    validate_idempotency_key,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import (
    Clock,
    IdFactory,
    _default_clock,
    _default_id_factory,
    _validated_utc_now,
    generate_conversation_title,
)
from materialsagent.application.errors import (
    ApplicationValidationError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    TargetTaskNotRecoverableError,
    from_persistence_error,
)
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.domain.models.task import NEEDS_INPUT, TOOL_EXECUTION
from materialsagent.domain.models.task import Task
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


NEW_TASK = "NEW_TASK"
SUPPLEMENT_TASK = "SUPPLEMENT_TASK"
TitleGenerator = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    conversation_id: str
    user_message: Message
    task: Task
    submission_mode: str = NEW_TASK
    idempotency_record_id: str | None = None
    idempotency_outcome: IdempotencyOutcome = IdempotencyOutcome.CREATED

    @property
    def idempotency_replayed(self) -> bool:
        return self.idempotency_outcome.replayed


class MessageSubmissionService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        title_generator: TitleGenerator | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or _default_clock
        self._id_factory = id_factory or _default_id_factory
        self._title_generator = title_generator or generate_conversation_title

    def prepare_submission(
        self,
        actor_context: ActorContext,
        *,
        conversation_id: str,
        request_id: str,
        content_text: str,
        submission_mode: str,
        target_task_id: str | None,
        idempotency_key: str,
    ) -> PreparedSubmission:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ApplicationValidationError()
        if not isinstance(request_id, str) or not request_id.strip():
            raise ApplicationValidationError()
        if not isinstance(content_text, str) or not content_text.strip():
            raise ApplicationValidationError()
        if submission_mode not in {NEW_TASK, SUPPLEMENT_TASK}:
            raise ApplicationValidationError()
        if (
            submission_mode == NEW_TASK
            and target_task_id is not None
        ) or (
            submission_mode == SUPPLEMENT_TASK
            and (
                not isinstance(target_task_id, str)
                or not target_task_id.strip()
            )
        ):
            raise ApplicationValidationError()
        try:
            validated_key = validate_idempotency_key(idempotency_key)
        except ValueError:
            raise ApplicationValidationError() from None

        normalized_content = content_text.strip()
        normalized_target = (
            None if target_task_id is None else target_task_id.strip()
        )
        operation = (
            TASK_CREATE
            if submission_mode == NEW_TASK
            else TASK_INPUT_SUPPLEMENT
        )
        request_digest = canonical_request_digest(
            {
                "conversation_id": conversation_id,
                "submission_mode": submission_mode,
                "content_text": normalized_content,
                "target_task_id": normalized_target,
            }
        )
        timestamp = _validated_utc_now(self._clock)
        task_id = (
            self._id_factory("task")
            if operation == TASK_CREATE
            else normalized_target
        )
        message_id = self._id_factory("msg")
        record_id = self._id_factory("idem")
        should_generate_title = False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                if (
                    unit_of_work.actors.get_for_update(
                        actor_context.actor_id
                    )
                    is None
                ):
                    raise ResourceNotFoundError()
                existing = unit_of_work.idempotency_records.get_by_scope(
                    actor_context.actor_id,
                    operation,
                    validated_key,
                )
                if existing is not None:
                    return self._load_replay(
                        unit_of_work,
                        actor_context,
                        conversation_id=conversation_id,
                        record=existing,
                        request_digest=request_digest,
                        idempotency_outcome=IdempotencyOutcome.REPLAY,
                    )
                conversation = unit_of_work.conversations.get_owned(
                    conversation_id,
                    actor_context.actor_id,
                )
                if conversation is None:
                    raise ResourceNotFoundError()
                if operation == TASK_CREATE:
                    is_first_message = (
                        unit_of_work.messages.get_latest_for_conversation(
                            conversation_id,
                            actor_context.actor_id,
                        )
                        is None
                    )
                    should_generate_title = (
                        is_first_message and conversation.title is None
                    )
                    task = Task.pending(
                        task_id=task_id,
                        conversation_id=conversation_id,
                        actor_id=actor_context.actor_id,
                        created_at=timestamp,
                    )
                else:
                    task = unit_of_work.tasks.get_owned_for_update(
                        task_id,
                        actor_context.actor_id,
                    )
                    if (
                        task is None
                        or task.conversation_id != conversation_id
                        or task.task_type != TOOL_EXECUTION
                        or unit_of_work.task_input_revisions.list_for_task(
                            task.task_id
                        )
                        == []
                    ):
                        raise ResourceNotFoundError()
                    if (
                        unit_of_work.idempotency_records
                        .get_unbound_supplement_for_task(task.task_id)
                        is not None
                        or any(
                            call.purpose == "CHAT_ORCHESTRATION"
                            and call.status in {"PENDING", "RUNNING"}
                            for call in unit_of_work.llm_calls.list_for_task(
                                task.task_id
                            )
                        )
                    ):
                        raise TargetTaskNotRecoverableError(
                            conversation_id=conversation_id,
                            task_id=task.task_id,
                        )
                    if (
                        task.current_status != NEEDS_INPUT
                        or any(
                            run.current_status in {"PENDING", "RUNNING"}
                            for run in unit_of_work.tool_runs.list_for_task(
                                task.task_id
                            )
                        )
                    ):
                        raise ResourceNotFoundError()
                message = Message.user(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    task_id=task.task_id,
                    actor_id=actor_context.actor_id,
                    request_id=request_id,
                    content_text=normalized_content,
                    created_at=timestamp,
                )
                record = IdempotencyRecord(
                    idempotency_record_id=record_id,
                    actor_id=actor_context.actor_id,
                    operation=operation,
                    idempotency_key=validated_key,
                    request_digest=request_digest,
                    first_request_id=request_id,
                    task_id=task.task_id,
                    message_id=message.message_id,
                    task_input_revision_id=None,
                    tool_run_id=None,
                    explanation_id=None,
                    created_at=timestamp,
                    expires_at=None,
                )
                conversation.updated_at = timestamp
                if operation == TASK_CREATE:
                    unit_of_work.tasks.add(task)
                unit_of_work.messages.add(message)
                unit_of_work.idempotency_records.add(record)
                if unit_of_work.conversations.update(conversation) is None:
                    raise ResourceNotFoundError()
                unit_of_work.commit()
        except PersistenceError as error:
            replay = self._recover_after_persistence_error(
                actor_context,
                conversation_id=conversation_id,
                operation=operation,
                idempotency_key=validated_key,
                request_digest=request_digest,
                request_id=request_id,
            )
            if replay is not None:
                if (
                    should_generate_title
                    and not replay.idempotency_replayed
                ):
                    self._try_set_first_title(
                        actor_context,
                        conversation_id,
                        normalized_content,
                    )
                return replay
            raise from_persistence_error(error) from None

        if should_generate_title:
            self._try_set_first_title(
                actor_context,
                conversation_id,
                normalized_content,
            )
        return PreparedSubmission(
            conversation_id=conversation_id,
            user_message=message,
            task=task,
            submission_mode=submission_mode,
            idempotency_record_id=record_id,
            idempotency_outcome=IdempotencyOutcome.CREATED,
        )

    def _recover_after_persistence_error(
        self,
        actor_context: ActorContext,
        *,
        conversation_id: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
    ) -> PreparedSubmission | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                record = unit_of_work.idempotency_records.get_by_scope(
                    actor_context.actor_id,
                    operation,
                    idempotency_key,
                )
                if record is None:
                    return None
                return self._load_replay(
                    unit_of_work,
                    actor_context,
                    conversation_id=conversation_id,
                    record=record,
                    request_digest=request_digest,
                    idempotency_outcome=recovered_idempotency_outcome(
                        first_request_id=record.first_request_id,
                        current_request_id=request_id,
                    ),
                )
        except PersistenceError:
            return None

    @staticmethod
    def _load_replay(
        unit_of_work: object,
        actor_context: ActorContext,
        *,
        conversation_id: str,
        record: IdempotencyRecord,
        request_digest: str,
        idempotency_outcome: IdempotencyOutcome,
    ) -> PreparedSubmission:
        if record.request_digest != request_digest:
            raise IdempotencyConflictError(
                conversation_id=conversation_id,
                task_id=record.task_id,
            )
        task = (
            None
            if record.task_id is None
            else unit_of_work.tasks.get_owned(
                record.task_id,
                actor_context.actor_id,
            )
        )
        message = (
            None
            if record.message_id is None
            else unit_of_work.messages.get(record.message_id)
        )
        conversation = unit_of_work.conversations.get_owned(
            conversation_id,
            actor_context.actor_id,
        )
        if (
            task is None
            or message is None
            or conversation is None
            or task.conversation_id != conversation_id
            or message.conversation_id != conversation_id
            or message.task_id != task.task_id
            or message.actor_id != actor_context.actor_id
            or message.request_id != record.first_request_id
        ):
            raise ResourceNotFoundError()
        return PreparedSubmission(
            conversation_id=conversation_id,
            user_message=message,
            task=task,
            submission_mode=(
                NEW_TASK
                if record.operation == TASK_CREATE
                else SUPPLEMENT_TASK
            ),
            idempotency_record_id=record.idempotency_record_id,
            idempotency_outcome=idempotency_outcome,
        )

    def _try_set_first_title(
        self,
        actor_context: ActorContext,
        conversation_id: str,
        content_text: str,
    ) -> None:
        try:
            generated_title = self._title_generator(content_text)
            if (
                not isinstance(generated_title, str)
                or not generated_title.strip()
            ):
                return
            with self._unit_of_work_factory() as unit_of_work:
                conversation = unit_of_work.conversations.get_owned(
                    conversation_id,
                    actor_context.actor_id,
                )
                if conversation is None or conversation.title is not None:
                    return
                conversation.title = generated_title.strip()
                if unit_of_work.conversations.update(conversation) is None:
                    return
                unit_of_work.commit()
        except Exception:
            return
