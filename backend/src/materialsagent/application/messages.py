from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


NEW_TASK = "NEW_TASK"
TitleGenerator = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    conversation_id: str
    user_message: Message
    task: Task


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
    ) -> PreparedSubmission:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ApplicationValidationError()
        if not isinstance(request_id, str) or not request_id.strip():
            raise ApplicationValidationError()
        if not isinstance(content_text, str) or not content_text.strip():
            raise ApplicationValidationError()
        if submission_mode != NEW_TASK or target_task_id is not None:
            raise ApplicationValidationError()

        normalized_content = content_text.strip()
        timestamp = _validated_utc_now(self._clock)
        try:
            with self._unit_of_work_factory() as unit_of_work:
                conversation = unit_of_work.conversations.get_owned(
                    conversation_id,
                    actor_context.actor_id,
                )
                if conversation is None:
                    raise ResourceNotFoundError()
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
                    task_id=self._id_factory("task"),
                    conversation_id=conversation_id,
                    actor_id=actor_context.actor_id,
                    created_at=timestamp,
                )
                message = Message.user(
                    message_id=self._id_factory("msg"),
                    conversation_id=conversation_id,
                    task_id=task.task_id,
                    actor_id=actor_context.actor_id,
                    request_id=request_id,
                    content_text=normalized_content,
                    created_at=timestamp,
                )
                conversation.updated_at = timestamp
                unit_of_work.tasks.add(task)
                unit_of_work.messages.add(message)
                if unit_of_work.conversations.update(conversation) is None:
                    raise ResourceNotFoundError()
                unit_of_work.commit()
        except PersistenceError as error:
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
