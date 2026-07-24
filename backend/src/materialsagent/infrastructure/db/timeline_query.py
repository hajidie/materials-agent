from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import (
    Engine,
    case,
    func,
    literal,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from materialsagent.domain.ports.timeline_query import (
    TaskDetailQuerySnapshot,
    TimelineKey,
    TimelinePosition,
    TimelineQueryPage,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.db.actor import (
    _raise_safe_persistence_error,
)
from materialsagent.infrastructure.db.asset import (
    AssetRow,
    _from_row as _asset_from_row,
)
from materialsagent.infrastructure.db.conversation_task import (
    ConversationRow,
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
    _conversation_from_row,
    _message_from_row,
    _revision_from_row,
    _task_from_row,
)
from materialsagent.infrastructure.db.explanation import (
    NaturalLanguageExplanationRow,
    _from_row as _explanation_from_row,
)
from materialsagent.infrastructure.db.llm_call import (
    LLMCallRow,
    _from_row as _llm_call_from_row,
)
from materialsagent.infrastructure.db.tool_result import (
    ResultAssetLinkRow,
    ToolResultRow,
    _link_from_row,
    _result_from_row,
)
from materialsagent.infrastructure.db.tool_run import (
    ToolRunRow,
    _from_row as _tool_run_from_row,
)


class SQLAlchemyTimelineQueryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        connection = None
        transaction = None
        try:
            connection = self._engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            )
            transaction = connection.begin()
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            isolation = connection.exec_driver_sql(
                "SHOW transaction_isolation"
            ).scalar_one()
            read_only = connection.exec_driver_sql(
                "SHOW transaction_read_only"
            ).scalar_one()
            if isolation != "repeatable read" or read_only != "on":
                raise PersistenceError(
                    "Read query transaction is not read-only."
                )
            with Session(
                bind=connection,
                autoflush=False,
                expire_on_commit=False,
            ) as session:
                yield session
        finally:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
            if connection is not None:
                connection.close()

    def fetch_owned_page(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        after: TimelinePosition | None,
        limit: int,
    ) -> TimelineQueryPage | None:
        try:
            with self._read_session() as session:
                return self._fetch(
                    session,
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    after=after,
                    limit=limit,
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        raise PersistenceError("Timeline query failed.")

    def fetch_owned_task_detail(
        self,
        *,
        actor_id: str,
        task_id: str,
    ) -> TaskDetailQuerySnapshot | None:
        try:
            with self._read_session() as session:
                return self._fetch_owned_task_detail(
                    session,
                    actor_id=actor_id,
                    task_id=task_id,
                )
        except PersistenceError:
            raise
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        raise PersistenceError("Task detail query failed.")

    @staticmethod
    def _fetch_owned_task_detail(
        session: Session,
        *,
        actor_id: str,
        task_id: str,
    ) -> TaskDetailQuerySnapshot | None:
        task_row = session.scalar(
            select(TaskRow).where(
                TaskRow.task_id == task_id,
                TaskRow.actor_id == actor_id,
            )
        )
        if task_row is None:
            return None

        message_rows = session.scalars(
            select(MessageRow)
            .where(MessageRow.task_id == task_id)
            .order_by(
                MessageRow.created_at.asc(),
                MessageRow.message_id.asc(),
            )
        ).all()
        revision_rows = session.scalars(
            select(TaskInputRevisionRow)
            .where(TaskInputRevisionRow.task_id == task_id)
            .order_by(
                TaskInputRevisionRow.revision.asc(),
                TaskInputRevisionRow.task_input_revision_id.asc(),
            )
        ).all()
        tool_run_rows = session.scalars(
            select(ToolRunRow)
            .where(ToolRunRow.task_id == task_id)
            .order_by(
                ToolRunRow.created_at.asc(),
                ToolRunRow.attempt_no.asc(),
                ToolRunRow.tool_run_id.asc(),
            )
        ).all()
        selected_result_id = task_row.selected_result_id
        result_row = session.scalar(
            select(ToolResultRow).where(
                ToolResultRow.result_id == selected_result_id,
                ToolResultRow.task_id == task_id,
                ToolResultRow.actor_id == actor_id,
            )
        )
        link_asset_rows = session.execute(
            select(ResultAssetLinkRow, AssetRow)
            .join(
                AssetRow,
                AssetRow.asset_id == ResultAssetLinkRow.asset_id,
            )
            .where(
                ResultAssetLinkRow.result_id == selected_result_id
            )
            .order_by(
                ResultAssetLinkRow.artifact_order.asc(),
                ResultAssetLinkRow.asset_id.asc(),
            )
        ).all()
        explanation_call_rows = session.execute(
            select(NaturalLanguageExplanationRow, LLMCallRow)
            .join(
                LLMCallRow,
                LLMCallRow.llm_call_id
                == NaturalLanguageExplanationRow.llm_call_id,
            )
            .where(
                NaturalLanguageExplanationRow.result_id
                == selected_result_id
            )
            .order_by(
                NaturalLanguageExplanationRow.created_at.asc(),
                NaturalLanguageExplanationRow.completed_at.asc().nulls_last(),
                NaturalLanguageExplanationRow.explanation_id.asc(),
            )
        ).all()

        return TaskDetailQuerySnapshot(
            task=_task_from_row(task_row),
            messages=tuple(
                _message_from_row(row) for row in message_rows
            ),
            revisions=tuple(
                _revision_from_row(row) for row in revision_rows
            ),
            tool_runs=tuple(
                _tool_run_from_row(row) for row in tool_run_rows
            ),
            selected_result=(
                None
                if result_row is None
                else _result_from_row(result_row)
            ),
            result_asset_links=tuple(
                _link_from_row(link_row)
                for link_row, _asset_row in link_asset_rows
            ),
            assets=tuple(
                _asset_from_row(asset_row)
                for _link_row, asset_row in link_asset_rows
            ),
            explanations=tuple(
                _explanation_from_row(explanation_row)
                for explanation_row, _call_row in explanation_call_rows
            ),
            llm_calls=tuple(
                _llm_call_from_row(call_row)
                for _explanation_row, call_row in explanation_call_rows
            ),
        )

    @staticmethod
    def _fetch(
        session: Session,
        *,
        actor_id: str,
        conversation_id: str,
        after: TimelinePosition | None,
        limit: int,
    ) -> TimelineQueryPage | None:
        conversation_row = session.scalar(
            select(ConversationRow).where(
                ConversationRow.conversation_id == conversation_id,
                ConversationRow.actor_id == actor_id,
            )
        )
        if conversation_row is None:
            return None

        message_rank = case(
            (MessageRow.role == "USER", 10),
            else_=20,
        )
        message_type = case(
            (MessageRow.role == "USER", "USER_MESSAGE"),
            else_="ASSISTANT_MESSAGE",
        )
        message_items = (
            select(
                MessageRow.created_at.label("anchor_at"),
                message_rank.label("item_type_rank"),
                MessageRow.message_id.label("item_id"),
                MessageRow.task_id.label("task_id"),
                message_type.label("item_type"),
            )
            .join(TaskRow, TaskRow.task_id == MessageRow.task_id)
            .where(
                MessageRow.conversation_id == conversation_id,
                or_(
                    TaskRow.task_type.is_(None),
                    TaskRow.task_type != "TOOL_EXECUTION",
                ),
            )
        )
        earliest_user = (
            select(func.min(MessageRow.created_at))
            .where(
                MessageRow.task_id == TaskRow.task_id,
                MessageRow.role == "USER",
            )
            .correlate(TaskRow)
            .scalar_subquery()
        )
        tool_items = select(
            func.coalesce(
                earliest_user,
                TaskRow.created_at,
            ).label("anchor_at"),
            literal(30).label("item_type_rank"),
            TaskRow.task_id.label("item_id"),
            TaskRow.task_id.label("task_id"),
            literal("TOOL_TASK").label("item_type"),
        ).where(
            TaskRow.conversation_id == conversation_id,
            TaskRow.task_type == "TOOL_EXECUTION",
        )
        item_union = union_all(message_items, tool_items).subquery(
            "timeline_items"
        )
        key_statement = select(item_union)
        if after is not None:
            key_statement = key_statement.where(
                tuple_(
                    item_union.c.anchor_at,
                    item_union.c.item_type_rank,
                    item_union.c.item_id,
                )
                > tuple_(
                    after.anchor_at,
                    after.item_type_rank,
                    after.item_id,
                )
            )
        key_statement = key_statement.order_by(
            item_union.c.anchor_at.asc(),
            item_union.c.item_type_rank.asc(),
            item_union.c.item_id.asc(),
        ).limit(limit + 1)
        raw_keys = session.execute(key_statement).mappings().all()
        has_more = len(raw_keys) > limit
        raw_keys = raw_keys[:limit]
        keys = tuple(
            TimelineKey(
                item_type=row["item_type"],
                item_id=row["item_id"],
                task_id=row["task_id"],
                anchor_at=row["anchor_at"],
                item_type_rank=row["item_type_rank"],
            )
            for row in raw_keys
        )

        message_ids = [
            key.item_id
            for key in keys
            if key.item_type != "TOOL_TASK"
        ]
        tool_task_ids = [
            key.task_id
            for key in keys
            if key.item_type == "TOOL_TASK"
        ]
        all_task_ids = sorted({key.task_id for key in keys})
        message_rows = session.scalars(
            select(MessageRow)
            .where(
                or_(
                    MessageRow.message_id.in_(message_ids),
                    MessageRow.task_id.in_(tool_task_ids),
                )
            )
            .order_by(
                MessageRow.created_at.asc(),
                MessageRow.message_id.asc(),
            )
        ).all()
        task_rows = session.scalars(
            select(TaskRow)
            .where(TaskRow.task_id.in_(all_task_ids))
            .order_by(TaskRow.task_id.asc())
        ).all()
        revision_rows = session.scalars(
            select(TaskInputRevisionRow)
            .where(TaskInputRevisionRow.task_id.in_(tool_task_ids))
            .order_by(
                TaskInputRevisionRow.task_id.asc(),
                TaskInputRevisionRow.revision.asc(),
                TaskInputRevisionRow.task_input_revision_id.asc(),
            )
        ).all()
        tool_run_rows = session.scalars(
            select(ToolRunRow)
            .where(ToolRunRow.task_id.in_(tool_task_ids))
            .order_by(
                ToolRunRow.task_id.asc(),
                ToolRunRow.created_at.asc(),
                ToolRunRow.attempt_no.asc(),
                ToolRunRow.tool_run_id.asc(),
            )
        ).all()
        selected_result_ids = sorted(
            {
                row.selected_result_id
                for row in task_rows
                if (
                    row.task_id in tool_task_ids
                    and row.selected_result_id is not None
                )
            }
        )
        result_rows = session.scalars(
            select(ToolResultRow)
            .where(ToolResultRow.result_id.in_(selected_result_ids))
            .order_by(ToolResultRow.result_id.asc())
        ).all()
        link_asset_rows = session.execute(
            select(ResultAssetLinkRow, AssetRow)
            .join(
                AssetRow,
                AssetRow.asset_id == ResultAssetLinkRow.asset_id,
            )
            .where(
                ResultAssetLinkRow.result_id.in_(selected_result_ids)
            )
            .order_by(
                ResultAssetLinkRow.result_id.asc(),
                ResultAssetLinkRow.artifact_order.asc(),
                ResultAssetLinkRow.asset_id.asc(),
            )
        ).all()
        explanation_call_rows = session.execute(
            select(NaturalLanguageExplanationRow, LLMCallRow)
            .join(
                LLMCallRow,
                LLMCallRow.llm_call_id
                == NaturalLanguageExplanationRow.llm_call_id,
            )
            .where(
                NaturalLanguageExplanationRow.result_id.in_(
                    selected_result_ids
                )
            )
            .order_by(
                NaturalLanguageExplanationRow.result_id.asc(),
                NaturalLanguageExplanationRow.created_at.asc(),
                NaturalLanguageExplanationRow.completed_at.asc().nulls_last(),
                NaturalLanguageExplanationRow.explanation_id.asc(),
            )
        ).all()

        return TimelineQueryPage(
            conversation=_conversation_from_row(conversation_row),
            keys=keys,
            has_more=has_more,
            messages=tuple(_message_from_row(row) for row in message_rows),
            tasks=tuple(_task_from_row(row) for row in task_rows),
            revisions=tuple(
                _revision_from_row(row) for row in revision_rows
            ),
            tool_runs=tuple(
                _tool_run_from_row(row) for row in tool_run_rows
            ),
            results=tuple(
                _result_from_row(row) for row in result_rows
            ),
            result_asset_links=tuple(
                _link_from_row(link_row)
                for link_row, _asset_row in link_asset_rows
            ),
            assets=tuple(
                _asset_from_row(asset_row)
                for _link_row, asset_row in link_asset_rows
            ),
            explanations=tuple(
                _explanation_from_row(explanation_row)
                for explanation_row, _call_row in explanation_call_rows
            ),
            llm_calls=tuple(
                _llm_call_from_row(call_row)
                for _explanation_row, call_row in explanation_call_rows
            ),
        )
