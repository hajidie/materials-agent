from __future__ import annotations

from typing import Protocol, Self

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision


class PersistenceError(RuntimeError):
    """Safe persistence failure without driver or SQL details."""


class PersistenceConflictError(PersistenceError):
    """A stable persistence uniqueness or concurrency conflict."""


class DatabaseUnavailableError(PersistenceError):
    """The configured database could not be reached or authenticated."""


class ActorRepository(Protocol):
    def get(self, actor_id: str) -> Actor | None: ...

    def add(self, actor: Actor) -> None: ...


class ConversationRepository(Protocol):
    def get(self, conversation_id: str) -> Conversation | None: ...

    def get_owned(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Conversation | None: ...

    def list_owned(self, actor_id: str) -> list[Conversation]: ...

    def add(self, conversation: Conversation) -> None: ...


class MessageRepository(Protocol):
    def get(self, message_id: str) -> Message | None: ...

    def add(self, message: Message) -> None: ...


class TaskRepository(Protocol):
    def get(self, task_id: str) -> Task | None: ...

    def get_owned(self, task_id: str, actor_id: str) -> Task | None: ...

    def add(self, task: Task) -> None: ...


class TaskInputRevisionRepository(Protocol):
    def get(
        self,
        task_input_revision_id: str,
    ) -> TaskInputRevision | None: ...

    def add(self, revision: TaskInputRevision) -> None: ...

    def list_for_task(self, task_id: str) -> list[TaskInputRevision]: ...


class UnitOfWork(Protocol):
    actors: ActorRepository
    conversations: ConversationRepository
    messages: MessageRepository
    tasks: TaskRepository
    task_input_revisions: TaskInputRevisionRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
