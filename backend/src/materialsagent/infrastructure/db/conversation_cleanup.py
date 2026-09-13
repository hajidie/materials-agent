from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    case,
    literal,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation_object_cleanup import (
    ConversationObjectCleanup,
)
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.asset import AssetRow, _from_row as _asset_from_row
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import ConversationRow, TaskRow
from materialsagent.infrastructure.db.explanation import NaturalLanguageExplanationRow
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.tool_run import ToolRunRow


class ConversationObjectCleanupRow(Base):
    __tablename__ = "conversation_object_cleanup"
    __table_args__ = (
        CheckConstraint("length(btrim(cleanup_id)) > 0", name="ck_cleanup_id_not_blank"),
        CheckConstraint("length(btrim(actor_id)) > 0", name="ck_cleanup_actor_id_not_blank"),
        CheckConstraint("length(btrim(conversation_id)) > 0", name="ck_cleanup_conversation_id_not_blank"),
        CheckConstraint("length(btrim(asset_id)) > 0", name="ck_cleanup_asset_id_not_blank"),
        CheckConstraint("length(btrim(operation_id)) > 0", name="ck_cleanup_operation_id_not_blank"),
        CheckConstraint("length(btrim(producer_tool_run_id)) > 0", name="ck_cleanup_producer_run_id_not_blank"),
        CheckConstraint("length(btrim(object_key)) > 0", name="ck_cleanup_object_key_not_blank"),
        CheckConstraint("length(btrim(bucket)) > 0", name="ck_cleanup_bucket_not_blank"),
        CheckConstraint("length(btrim(storage_namespace)) > 0", name="ck_cleanup_namespace_not_blank"),
        CheckConstraint("identity_version IN ('LEGACY_DB_KEY', 'METADATA_V1')", name="ck_cleanup_identity_version_allowed"),
        CheckConstraint("status IN ('PENDING', 'COMPLETED', 'SAFETY_BLOCKED')", name="ck_cleanup_status_allowed"),
        CheckConstraint("attempts >= 0", name="ck_cleanup_attempts_nonnegative"),
        CheckConstraint("safety_error_code IS NULL OR length(btrim(safety_error_code)) > 0", name="ck_cleanup_error_not_blank"),
        UniqueConstraint("asset_id", name="uq_cleanup_asset"),
        Index("ix_cleanup_pending_created", "status", "created_at", "cleanup_id"),
    )

    cleanup_id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    asset_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_tool_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    storage_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    identity_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    safety_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


def _cleanup_from_row(row: ConversationObjectCleanupRow) -> ConversationObjectCleanup:
    return ConversationObjectCleanup(
        **{
            field: getattr(row, field)
            for field in ConversationObjectCleanup.__dataclass_fields__
        }
    )


class SQLAlchemyConversationObjectCleanupRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, cleanup_id: str) -> ConversationObjectCleanup | None:
        try:
            row = self._session.get(ConversationObjectCleanupRow, cleanup_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return None if row is None else _cleanup_from_row(row)

    def list_pending(
        self,
        *,
        limit: int,
        preferred_ids: tuple[str, ...] = (),
    ) -> list[ConversationObjectCleanup]:
        bounded_limit = max(0, min(limit, 100))
        if bounded_limit == 0:
            return []
        preferred = tuple(dict.fromkeys(preferred_ids))
        priority = (
            case(
                (ConversationObjectCleanupRow.cleanup_id.in_(preferred), 0),
                else_=1,
            )
            if preferred
            else literal(1)
        )
        statement = (
            select(ConversationObjectCleanupRow)
            .where(ConversationObjectCleanupRow.status == "PENDING")
            .order_by(
                priority,
                ConversationObjectCleanupRow.created_at,
                ConversationObjectCleanupRow.cleanup_id,
            )
            .limit(bounded_limit)
            .with_for_update(skip_locked=True)
        )
        try:
            rows = self._session.scalars(statement).all()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
        return [_cleanup_from_row(row) for row in rows]

    def add(self, cleanup: ConversationObjectCleanup) -> None:
        try:
            self._session.add(
                ConversationObjectCleanupRow(
                    **{
                        field: getattr(cleanup, field)
                        for field in ConversationObjectCleanup.__dataclass_fields__
                    }
                )
            )
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(
        self,
        cleanup: ConversationObjectCleanup,
        *,
        expected_status: str,
    ) -> ConversationObjectCleanup | None:
        try:
            row = self._session.get(
                ConversationObjectCleanupRow,
                cleanup.cleanup_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row is None or row.status != expected_status:
                return None
            for field in ConversationObjectCleanup.__dataclass_fields__:
                setattr(row, field, getattr(cleanup, field))
            self._session.flush()
            return _cleanup_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


class SQLAlchemyConversationLifecycleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _locked_tasks(
        self,
        *,
        actor_id: str,
        conversation_id: str | None,
    ) -> list[TaskRow]:
        statement = select(TaskRow).where(TaskRow.actor_id == actor_id)
        if conversation_id is not None:
            statement = statement.where(TaskRow.conversation_id == conversation_id)
        return list(
            self._session.scalars(
                statement.order_by(TaskRow.task_id).with_for_update()
            ).all()
        )

    def recover_stale(
        self,
        *,
        actor_id: str,
        process_cutoff: datetime,
        recovered_at: datetime,
        conversation_id: str | None = None,
    ) -> dict[str, int]:
        counts = {"llm_calls": 0, "tool_runs": 0, "explanations": 0, "assets": 0, "tasks": 0}
        try:
            tasks = self._locked_tasks(actor_id=actor_id, conversation_id=conversation_id)
            task_by_id = {row.task_id: row for row in tasks}
            task_ids = tuple(task_by_id)
            if not task_ids:
                return counts

            calls = self._session.scalars(
                select(LLMCallRow)
                .where(LLMCallRow.task_id.in_(task_ids))
                .order_by(LLMCallRow.llm_call_id)
                .with_for_update()
            ).all()
            runs = self._session.scalars(
                select(ToolRunRow)
                .where(ToolRunRow.task_id.in_(task_ids))
                .order_by(ToolRunRow.tool_run_id)
                .with_for_update()
            ).all()
            explanations = self._session.scalars(
                select(NaturalLanguageExplanationRow)
                .where(NaturalLanguageExplanationRow.task_id.in_(task_ids))
                .order_by(NaturalLanguageExplanationRow.explanation_id)
                .with_for_update()
            ).all()
            assets = self._session.scalars(
                select(AssetRow)
                .where(AssetRow.task_id.in_(task_ids))
                .order_by(AssetRow.asset_id)
                .with_for_update()
            ).all()

            unsafe_task_ids: set[str] = set()
            pending_before_start: set[str] = set()
            for call in calls:
                if call.created_at >= process_cutoff or call.status not in {"PENDING", "RUNNING"}:
                    continue
                was_pending = call.status == "PENDING"
                call.status = "FAILED"
                call.started_at = call.started_at or call.created_at
                call.completed_at = recovered_at
                call.duration_ms = max(0, int((recovered_at - call.started_at).total_seconds() * 1000))
                call.error_code = "PROCESS_INTERRUPTED_BEFORE_START" if was_pending else "PROCESS_INTERRUPTED"
                call.safe_error_message = "进程中断，调用未能完成。"
                counts["llm_calls"] += 1
                if was_pending and call.purpose in {
                    "CHAT_ORCHESTRATION",
                    "TOOL_INPUT_EXTRACTION",
                }:
                    pending_before_start.add(call.task_id)
                else:
                    unsafe_task_ids.add(call.task_id)

            for run in runs:
                if run.created_at >= process_cutoff or run.current_status not in {"PENDING", "RUNNING"}:
                    continue
                run.current_status = "FAILED"
                run.started_at = run.started_at or run.created_at
                run.completed_at = recovered_at
                run.duration_ms = max(0, int((recovered_at - run.started_at).total_seconds() * 1000))
                run.completed_outputs = []
                run.failed_outputs = list(run.requested_outputs)
                run.error_code = "PROCESS_INTERRUPTED"
                run.safe_error_message = "进程中断，工具执行未能完成。"
                counts["tool_runs"] += 1
                unsafe_task_ids.add(run.task_id)

            for explanation in explanations:
                if explanation.created_at >= process_cutoff or explanation.status not in {"PENDING", "RUNNING"}:
                    continue
                explanation.status = "FAILED"
                explanation.started_at = explanation.started_at or explanation.created_at
                explanation.completed_at = recovered_at
                explanation.duration_ms = max(0, int((recovered_at - explanation.started_at).total_seconds() * 1000))
                explanation.text = None
                explanation.error_code = "PROCESS_INTERRUPTED"
                explanation.safe_error_message = "进程中断，解释生成未能完成。"
                counts["explanations"] += 1
                unsafe_task_ids.add(explanation.task_id)

            for asset in assets:
                if asset.created_at >= process_cutoff or asset.current_status != "PENDING":
                    continue
                asset.current_status = "ORPHANED"
                asset.orphaned_at = recovered_at
                asset.orphan_reason = "PROCESS_INTERRUPTED"
                asset.orphan_details = {}
                counts["assets"] += 1

            for task_id in pending_before_start - unsafe_task_ids:
                task = task_by_id[task_id]
                if task.current_status == "RUNNING":
                    task.current_status = "PENDING"
                    task.completed_at = None
                    task.error_code = None
                    task.safe_error_message = None
                    counts["tasks"] += 1
            for task_id in unsafe_task_ids:
                task = task_by_id[task_id]
                if task.current_status in {"PENDING", "RUNNING", "READY", "NEEDS_INPUT"}:
                    task.current_status = "FAILED"
                    task.completed_at = recovered_at
                    task.error_code = "PROCESS_INTERRUPTED"
                    task.safe_error_message = "进程中断，任务未能安全继续。"
                    counts["tasks"] += 1
            self._session.flush()
            return counts
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def current_activity_exists(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        process_cutoff: datetime,
    ) -> bool:
        try:
            from materialsagent.infrastructure.db.agent import AgentRunRow
            agent_activity = self._session.scalar(select(AgentRunRow.agent_run_id).where(
                AgentRunRow.actor_id == actor_id, AgentRunRow.conversation_id == conversation_id,
                AgentRunRow.status.in_(("PENDING", "RUNNING"))).limit(1))
            if agent_activity is not None:
                return True
            linked = self._session.scalars(select(AgentRunRow).where(
                AgentRunRow.actor_id == actor_id, AgentRunRow.conversation_id == conversation_id)).all()
            managed_ids = set()
            for row in linked:
                document = row.document
                for record in [document.get("draft"), document.get("pending_execution"), *document.get("executions", []), *document.get("observations", [])]:
                    if record and record.get("task_id"):
                        managed_ids.add(record["task_id"])
            tasks = [task for task in self._locked_tasks(actor_id=actor_id, conversation_id=conversation_id)
                if task.task_id not in managed_ids]
            if any(
                task.current_status in {"PENDING", "RUNNING", "READY"}
                and task.updated_at >= process_cutoff
                for task in tasks
            ):
                return True
            task_ids = tuple(task.task_id for task in tasks)
            if not task_ids:
                return False
            active_calls = self._session.scalar(
                select(LLMCallRow.llm_call_id)
                .where(
                    LLMCallRow.task_id.in_(task_ids),
                    LLMCallRow.status.in_(("PENDING", "RUNNING")),
                    LLMCallRow.created_at >= process_cutoff,
                )
                .limit(1)
            )
            active_runs = self._session.scalar(
                select(ToolRunRow.tool_run_id)
                .where(
                    ToolRunRow.task_id.in_(task_ids),
                    ToolRunRow.current_status.in_(("PENDING", "RUNNING")),
                    ToolRunRow.created_at >= process_cutoff,
                )
                .limit(1)
            )
            active_explanations = self._session.scalar(
                select(NaturalLanguageExplanationRow.explanation_id)
                .where(
                    NaturalLanguageExplanationRow.task_id.in_(task_ids),
                    NaturalLanguageExplanationRow.status.in_(("PENDING", "RUNNING")),
                    NaturalLanguageExplanationRow.created_at >= process_cutoff,
                )
                .limit(1)
            )
            return any(value is not None for value in (active_calls, active_runs, active_explanations))
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def list_assets_for_delete(
        self,
        *,
        actor_id: str,
        conversation_id: str,
    ) -> list[Asset]:
        try:
            rows = self._session.scalars(
                select(AssetRow)
                .outerjoin(TaskRow, TaskRow.task_id == AssetRow.task_id)
                .where(
                    ((TaskRow.actor_id == actor_id) & (TaskRow.conversation_id == conversation_id))
                    | (AssetRow.conversation_id == conversation_id),
                    AssetRow.actor_id == actor_id,
                )
                .order_by(AssetRow.asset_id)
                .with_for_update(of=AssetRow)
            ).all()
            return [_asset_from_row(row) for row in rows]
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
