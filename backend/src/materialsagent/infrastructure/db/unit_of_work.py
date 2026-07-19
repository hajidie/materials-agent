from __future__ import annotations

from types import TracebackType

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from materialsagent.domain.ports.unit_of_work import (
    ActorRepository,
    ConversationRepository,
    DatabaseUnavailableError,
    MessageRepository,
    PersistenceConflictError,
    PersistenceError,
    TaskInputRevisionRepository,
    TaskRepository,
)
from materialsagent.infrastructure.db.actor import SQLAlchemyActorRepository
from materialsagent.infrastructure.db.conversation_task import (
    SQLAlchemyConversationRepository,
    SQLAlchemyMessageRepository,
    SQLAlchemyTaskInputRevisionRepository,
    SQLAlchemyTaskRepository,
)


class SQLAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None
        self._actors: ActorRepository | None = None
        self._conversations: ConversationRepository | None = None
        self._messages: MessageRepository | None = None
        self._tasks: TaskRepository | None = None
        self._task_input_revisions: TaskInputRevisionRepository | None = None

    @property
    def actors(self) -> ActorRepository:
        if self._actors is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._actors

    @property
    def conversations(self) -> ConversationRepository:
        if self._conversations is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._conversations

    @property
    def messages(self) -> MessageRepository:
        if self._messages is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._messages

    @property
    def tasks(self) -> TaskRepository:
        if self._tasks is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._tasks

    @property
    def task_input_revisions(self) -> TaskInputRevisionRepository:
        if self._task_input_revisions is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._task_input_revisions

    def __enter__(self) -> SQLAlchemyUnitOfWork:
        if self.session is not None:
            raise RuntimeError("UnitOfWork is already active.")
        self.session = self._session_factory()
        self._actors = SQLAlchemyActorRepository(self.session)
        self._conversations = SQLAlchemyConversationRepository(self.session)
        self._messages = SQLAlchemyMessageRepository(self.session)
        self._tasks = SQLAlchemyTaskRepository(self.session)
        self._task_input_revisions = SQLAlchemyTaskInputRevisionRepository(
            self.session
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        try:
            self.rollback()
        finally:
            try:
                self.session.close()
            finally:
                self.session = None
                self._actors = None
                self._conversations = None
                self._messages = None
                self._tasks = None
                self._task_input_revisions = None

    def _active_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self.session

    def commit(self) -> None:
        session = self._active_session()
        try:
            session.commit()
        except IntegrityError:
            self.rollback()
            raise PersistenceConflictError("Persistence conflict.") from None
        except DBAPIError:
            self.rollback()
            raise DatabaseUnavailableError("Database unavailable.") from None
        except SQLAlchemyError:
            self.rollback()
            raise PersistenceError("Persistence operation failed.") from None

    def rollback(self) -> None:
        session = self._active_session()
        try:
            session.rollback()
        except DBAPIError:
            raise DatabaseUnavailableError("Database unavailable.") from None
        except SQLAlchemyError:
            raise PersistenceError("Persistence operation failed.") from None
