from __future__ import annotations

from types import TracebackType

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from materialsagent.domain.ports.unit_of_work import (
    ActorRepository,
    AssetRepository,
    ConversationRepository,
    DatabaseUnavailableError,
    ExplanationRepository,
    IdempotencyRecordRepository,
    LLMCallRepository,
    MessageRepository,
    PersistenceConflictError,
    PersistenceError,
    ResultAssetLinkRepository,
    TaskInputRevisionRepository,
    TaskRepository,
    ToolRunRepository,
    ToolResultRepository,
)
from materialsagent.infrastructure.db.actor import SQLAlchemyActorRepository
from materialsagent.infrastructure.db.asset import SQLAlchemyAssetRepository
from materialsagent.infrastructure.db.llm_call import SQLAlchemyLLMCallRepository
from materialsagent.infrastructure.db.explanation import (
    SQLAlchemyExplanationRepository,
)
from materialsagent.infrastructure.db.idempotency_record import (
    SQLAlchemyIdempotencyRecordRepository,
)
from materialsagent.infrastructure.db.tool_result import (
    SQLAlchemyResultAssetLinkRepository,
    SQLAlchemyToolResultRepository,
)
from materialsagent.infrastructure.db.tool_run import SQLAlchemyToolRunRepository
from materialsagent.infrastructure.db.conversation_task import (
    SQLAlchemyConversationRepository,
    SQLAlchemyMessageRepository,
    SQLAlchemyTaskInputRevisionRepository,
    SQLAlchemyTaskRepository,
    TaskInputRevisionRow,
    TaskRow,
    _revision_from_row,
    _task_from_row,
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
        self._llm_calls: LLMCallRepository | None = None
        self._tool_runs: ToolRunRepository | None = None
        self._assets: AssetRepository | None = None
        self._tool_results: ToolResultRepository | None = None
        self._result_asset_links: ResultAssetLinkRepository | None = None
        self._explanations: ExplanationRepository | None = None
        self._idempotency_records: IdempotencyRecordRepository | None = None

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

    @property
    def llm_calls(self) -> LLMCallRepository:
        if self._llm_calls is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._llm_calls

    @property
    def tool_runs(self) -> ToolRunRepository:
        if self._tool_runs is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._tool_runs

    @property
    def assets(self) -> AssetRepository:
        if self._assets is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._assets

    @property
    def tool_results(self) -> ToolResultRepository:
        if self._tool_results is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._tool_results

    @property
    def result_asset_links(self) -> ResultAssetLinkRepository:
        if self._result_asset_links is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._result_asset_links

    @property
    def explanations(self) -> ExplanationRepository:
        if self._explanations is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._explanations

    @property
    def idempotency_records(self) -> IdempotencyRecordRepository:
        if self._idempotency_records is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self._idempotency_records

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
        self._llm_calls = SQLAlchemyLLMCallRepository(self.session)
        self._tool_runs = SQLAlchemyToolRunRepository(self.session)
        self._assets = SQLAlchemyAssetRepository(self.session)
        self._tool_results = SQLAlchemyToolResultRepository(self.session)
        self._result_asset_links = SQLAlchemyResultAssetLinkRepository(
            self.session
        )
        self._explanations = SQLAlchemyExplanationRepository(self.session)
        self._idempotency_records = SQLAlchemyIdempotencyRecordRepository(
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
                self._llm_calls = None
                self._tool_runs = None
                self._assets = None
                self._tool_results = None
                self._result_asset_links = None
                self._explanations = None
                self._idempotency_records = None

    def _active_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("UnitOfWork has not been entered.")
        return self.session

    def commit(self) -> None:
        session = self._active_session()
        try:
            self._validate_routing_states(session)
            session.commit()
            self._clear_routing_state_tracking(session)
        except PersistenceConflictError:
            self.rollback()
            raise
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
            self._clear_routing_state_tracking(session)
        except DBAPIError:
            raise DatabaseUnavailableError("Database unavailable.") from None
        except SQLAlchemyError:
            raise PersistenceError("Persistence operation failed.") from None

    @staticmethod
    def _validate_routing_states(session: Session) -> None:
        session_info = getattr(session, "info", None)
        if session_info is None:
            return
        task_ids = session_info.get("routing_state_task_ids")
        if task_ids is None:
            return
        if not isinstance(task_ids, set):
            raise PersistenceConflictError("Persistence conflict.")
        if not task_ids:
            return
        session.flush()
        for task_id in task_ids:
            row = session.get(TaskRow, task_id)
            if row is None or row.current_status not in {"READY", "NEEDS_INPUT"}:
                continue
            latest_revision = session.scalar(
                select(TaskInputRevisionRow)
                .where(TaskInputRevisionRow.task_id == task_id)
                .order_by(
                    TaskInputRevisionRow.revision.desc(),
                    TaskInputRevisionRow.task_input_revision_id.desc(),
                )
                .limit(1)
            )
            try:
                _task_from_row(row).validate_routing_state(
                    None
                    if latest_revision is None
                    else _revision_from_row(latest_revision)
                )
            except ValueError:
                raise PersistenceConflictError("Persistence conflict.") from None

    @staticmethod
    def _clear_routing_state_tracking(session: Session) -> None:
        session_info = getattr(session, "info", None)
        if session_info is not None:
            session_info.pop("routing_state_task_ids", None)
