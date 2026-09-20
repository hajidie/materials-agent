from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.tool_invocation import (
    InvocationResult,
    InvocationRun,
    InvocationStatus,
    InvocationTrigger,
)
from materialsagent.domain.ports.tool_registry import ToolExecutionProfile
from materialsagent.infrastructure.db.actor import _raise_safe_persistence_error
from materialsagent.infrastructure.db.base import Base


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class InvocationResultRow(Base):
    __tablename__ = "invocation_result"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(invocation_result_id)) > 0",
            name="ck_invocation_result_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(invocation_run_id)) > 0",
            name="ck_invocation_result_run_id_not_blank",
        ),
        CheckConstraint(
            "jsonb_typeof(data) = 'object'",
            name="ck_invocation_result_data_object",
        ),
        CheckConstraint(
            "jsonb_typeof(presentation) = 'object'",
            name="ck_invocation_result_presentation_object",
        ),
        UniqueConstraint(
            "invocation_run_id",
            name="uq_invocation_result_run",
        ),
    )

    invocation_result_id: Mapped[str] = mapped_column(Text, primary_key=True)
    invocation_run_id: Mapped[str] = mapped_column(
        ForeignKey(
            "invocation_run.invocation_run_id",
            name="fk_invocation_result_run",
            ondelete="CASCADE",
            use_alter=True,
        ),
        nullable=False,
    )
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    presentation: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvocationRunRow(Base):
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    __tablename__ = "invocation_run"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('MESSAGE_PROPOSAL', 'EXPLICIT_RETRY')",
            name="ck_invocation_trigger_allowed",
        ),
        CheckConstraint(
            "execution_profile IN ('STANDARD', 'SIDE_EFFECT', 'MANAGED')",
            name="ck_invocation_profile_allowed",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PENDING_CONFIRMATION', 'RUNNING', 'SUCCEEDED', "
            "'FAILED', 'DENIED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN')",
            name="ck_invocation_status_allowed",
        ),
        CheckConstraint("schema_hash ~ '^[0-9a-f]{64}$'", name="ck_invocation_schema_hash"),
        CheckConstraint(
            "jsonb_typeof(proposed_arguments) = 'object' AND jsonb_typeof(policy_snapshot) = 'object'",
            name="ck_invocation_json_objects",
        ),
        CheckConstraint(
            "jsonb_typeof(tool_projection) = 'object'",
            name="ck_invocation_tool_projection_object",
        ),
        CheckConstraint(
            "(execution_profile = 'MANAGED' AND task_id IS NOT NULL AND invocation_result_id IS NULL) OR "
            "(execution_profile <> 'MANAGED' AND task_id IS NULL AND managed_tool_run_id IS NULL)",
            name="ck_invocation_profile_relationship",
        ),
        CheckConstraint(
            "(execution_profile = 'SIDE_EFFECT') = confirmation_required",
            name="ck_invocation_profile_confirmation",
        ),
        CheckConstraint(
            "NOT (invocation_result_id IS NOT NULL AND managed_tool_run_id IS NOT NULL)",
            name="ck_invocation_result_relationship_exclusive",
        ),
        CheckConstraint(
            "confirmation_required OR (confirmation_expires_at IS NULL AND confirmed_at IS NULL "
            "AND confirmed_by IS NULL AND rejected_at IS NULL AND rejected_by IS NULL AND expired_at IS NULL)",
            name="ck_invocation_confirmation_scope",
        ),
        CheckConstraint(
            "NOT confirmation_required OR confirmation_expires_at IS NOT NULL",
            name="ck_invocation_confirmation_expiry_required",
        ),
        CheckConstraint(
            "(confirmed_at IS NULL) = (confirmed_by IS NULL) AND "
            "(rejected_at IS NULL) = (rejected_by IS NULL)",
            name="ck_invocation_confirmation_fact_pairs",
        ),
        CheckConstraint(
            "NOT (confirmed_at IS NOT NULL AND rejected_at IS NOT NULL)",
            name="ck_invocation_confirmation_facts_exclusive",
        ),
        CheckConstraint(
            "status <> 'PENDING_CONFIRMATION' OR "
            "(confirmation_required AND confirmed_at IS NULL AND rejected_at IS NULL AND expired_at IS NULL)",
            name="ck_invocation_pending_confirmation_shape",
        ),
        CheckConstraint("status <> 'REJECTED' OR rejected_at IS NOT NULL", name="ck_invocation_rejected_facts"),
        CheckConstraint("status <> 'EXPIRED' OR expired_at IS NOT NULL", name="ck_invocation_expired_facts"),
        CheckConstraint("status <> 'DENIED' OR denied_at IS NOT NULL", name="ck_invocation_denied_facts"),
        CheckConstraint(
            "status <> 'RUNNING' OR (execution_claim_token IS NOT NULL AND execution_lease_expires_at IS NOT NULL)",
            name="ck_invocation_running_claim",
        ),
        CheckConstraint(
            "status <> 'OUTCOME_UNKNOWN' OR execution_profile = 'SIDE_EFFECT' OR "
            "(executor_id = 'mcp' AND binding_snapshot IS NOT NULL)",
            name="ck_invocation_outcome_unknown_profile",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR "
            "(execution_attempt_count > 0 AND execution_claim_token IS NOT NULL)",
            name="ck_invocation_dispatch_attempt",
        ),
        CheckConstraint("execution_attempt_count >= 0", name="ck_invocation_attempt_count"),
        CheckConstraint(
            "COALESCE((executor_id = 'mcp' AND binding_snapshot IS NOT NULL AND jsonb_typeof(binding_snapshot) = 'object' "
            "AND binding_snapshot->>'executor_id' = 'mcp' AND binding_snapshot->>'tool_id' = tool_id "
            "AND binding_snapshot->>'tool_version' = tool_version AND binding_snapshot->>'schema_hash' = schema_hash "
            "AND (remote_operation IS NULL OR jsonb_typeof(remote_operation) = 'object') "
            "AND (remote_receipt IS NULL OR jsonb_typeof(remote_receipt) = 'object')) OR "
            "(executor_id <> 'mcp' AND binding_snapshot IS NULL AND remote_operation IS NULL AND remote_receipt IS NULL), false)",
            name="ck_invocation_mcp_facts",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED', 'DENIED', 'REJECTED', 'EXPIRED', 'OUTCOME_UNKNOWN')) "
            "= (completed_at IS NOT NULL)",
            name="ck_invocation_terminal_completion",
        ),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR "
            "((execution_profile = 'MANAGED' AND managed_tool_run_id IS NOT NULL) OR "
            "(execution_profile <> 'MANAGED' AND invocation_result_id IS NOT NULL))",
            name="ck_invocation_success_result_required",
        ),
        CheckConstraint(
            "invocation_result_id IS NULL OR status = 'SUCCEEDED'",
            name="ck_invocation_result_terminal",
        ),
        CheckConstraint("updated_at >= created_at", name="ck_invocation_updated_order"),
        ForeignKeyConstraint(
            ["source_message_id", "conversation_id"],
            ["message.message_id", "message.conversation_id"],
            name="fk_invocation_source_message_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["managed_tool_run_id", "task_id"],
            ["tool_run.tool_run_id", "tool_run.task_id"],
            name="fk_invocation_managed_tool_run_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint("invocation_result_id", name="uq_invocation_result"),
        UniqueConstraint("actor_id", "idempotency_key", name="uq_invocation_actor_idempotency"),
        Index(
            "ix_invocation_conversation_created",
            "conversation_id",
            "created_at",
            "invocation_run_id",
        ),
        Index("ix_invocation_recovery", "status", "execution_lease_expires_at"),
    )

    invocation_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actor.actor_id", name="fk_invocation_actor", ondelete="RESTRICT"),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey(
            "conversation.conversation_id",
            name="fk_invocation_conversation",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    source_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task.task_id", name="fk_invocation_task", ondelete="CASCADE"),
        nullable=True,
    )
    retry_of_invocation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "invocation_run.invocation_run_id",
            name="fk_invocation_retry_of",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    executor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_arguments: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    tool_projection: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmation_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authorization_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    denied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_claim_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    invocation_result_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "invocation_result.invocation_result_id",
            name="fk_invocation_result",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    managed_tool_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    binding_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    remote_operation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    remote_receipt: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


def _result_from_row(row: InvocationResultRow) -> InvocationResult:
    return InvocationResult(
        invocation_result_id=row.invocation_result_id,
        invocation_run_id=row.invocation_run_id,
        data=row.data,
        presentation=row.presentation,
        created_at=row.created_at,
    )


def _run_from_row(row: InvocationRunRow) -> InvocationRun:
    return InvocationRun(
        binding_snapshot=row.binding_snapshot,
        remote_operation=row.remote_operation,
        remote_receipt=row.remote_receipt,
        invocation_run_id=row.invocation_run_id,
        actor_id=row.actor_id,
        conversation_id=row.conversation_id,
        source_message_id=row.source_message_id,
        task_id=row.task_id,
        retry_of_invocation_run_id=row.retry_of_invocation_run_id,
        request_id=row.request_id,
        idempotency_key=row.idempotency_key,
        trigger=InvocationTrigger(row.trigger),
        tool_id=row.tool_id,
        tool_version=row.tool_version,
        schema_hash=row.schema_hash,
        execution_profile=ToolExecutionProfile(row.execution_profile),
        executor_id=row.executor_id,
        proposed_arguments=row.proposed_arguments,
        policy_snapshot=row.policy_snapshot,
        tool_projection=row.tool_projection,
        status=InvocationStatus(row.status),
        confirmation_required=row.confirmation_required,
        confirmation_expires_at=row.confirmation_expires_at,
        confirmed_at=row.confirmed_at,
        confirmed_by=row.confirmed_by,
        rejected_at=row.rejected_at,
        rejected_by=row.rejected_by,
        expired_at=row.expired_at,
        authorization_checked_at=row.authorization_checked_at,
        denied_at=row.denied_at,
        execution_claim_token=row.execution_claim_token,
        execution_lease_expires_at=row.execution_lease_expires_at,
        dispatch_started_at=row.dispatch_started_at,
        execution_attempt_count=row.execution_attempt_count,
        invocation_result_id=row.invocation_result_id,
        managed_tool_run_id=row.managed_tool_run_id,
        error_code=row.error_code,
        safe_error_message=row.safe_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        version=row.version,
    )


class SQLAlchemyInvocationResultRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, invocation_result_id: str) -> InvocationResult | None:
        try:
            row = self._session.get(InvocationResultRow, invocation_result_id)
            return None if row is None else _result_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def add(self, result: InvocationResult) -> None:
        try:
            self._session.add(
                InvocationResultRow(
                    invocation_result_id=result.invocation_result_id,
                    invocation_run_id=result.invocation_run_id,
                    data=_plain(result.data),
                    presentation=_plain(result.presentation),
                    created_at=result.created_at,
                )
            )
            # InvocationRun points back to this row when it transitions to
            # SUCCEEDED. Sessions use autoflush=False, so make the result
            # visible to that FK-bearing CAS update without committing the
            # surrounding transaction.
            self._session.flush()
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


class SQLAlchemyInvocationRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_mcp_recovery(self, actor_id, cutoff, limit, statuses=("RUNNING", "OUTCOME_UNKNOWN")):
        rows = self._session.scalars(select(InvocationRunRow).where(
            InvocationRunRow.actor_id == actor_id, InvocationRunRow.executor_id == "mcp",
            InvocationRunRow.status.in_(statuses), InvocationRunRow.updated_at < cutoff)
            .order_by(InvocationRunRow.updated_at, InvocationRunRow.invocation_run_id).limit(limit)).all()
        return [_run_from_row(row) for row in rows]

    def get(self, invocation_run_id: str) -> InvocationRun | None:
        try:
            row = self._session.get(InvocationRunRow, invocation_run_id)
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def get_owned(self, invocation_run_id: str, actor_id: str) -> InvocationRun | None:
        try:
            row = self._session.scalar(
                select(InvocationRunRow).where(
                    InvocationRunRow.invocation_run_id == invocation_run_id,
                    InvocationRunRow.actor_id == actor_id,
                )
            )
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def get_owned_for_update(self, invocation_run_id: str, actor_id: str) -> InvocationRun | None:
        from .ml_resources import lock_conversation
        parent = self.get_owned(invocation_run_id, actor_id)
        if parent:
            lock_conversation(self._session, actor_id, parent.conversation_id)
        try:
            row = self._session.scalar(
                select(InvocationRunRow)
                .where(
                    InvocationRunRow.invocation_run_id == invocation_run_id,
                    InvocationRunRow.actor_id == actor_id,
                )
                .with_for_update()
            )
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def get_by_message(self, source_message_id: str) -> InvocationRun | None:
        try:
            row = self._session.scalar(
                select(InvocationRunRow).where(
                    InvocationRunRow.source_message_id == source_message_id,
                    InvocationRunRow.trigger == InvocationTrigger.MESSAGE_PROPOSAL.value,
                )
            )
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def get_by_idempotency(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> InvocationRun | None:
        try:
            row = self._session.scalar(
                select(InvocationRunRow).where(
                    InvocationRunRow.actor_id == actor_id,
                    InvocationRunRow.idempotency_key == idempotency_key,
                )
            )
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def list_for_conversation(self, conversation_id: str, actor_id: str) -> list[InvocationRun]:
        try:
            rows = self._session.scalars(
                select(InvocationRunRow)
                .where(
                    InvocationRunRow.conversation_id == conversation_id,
                    InvocationRunRow.actor_id == actor_id,
                )
                .order_by(InvocationRunRow.created_at, InvocationRunRow.invocation_run_id)
            ).all()
            return [_run_from_row(row) for row in rows]
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def add(self, run: InvocationRun) -> None:
        from .ml_resources import lock_conversation, ResourceTransaction
        lock_conversation(self._session, run.actor_id, run.conversation_id, writable=True)
        if run.executor_id == "mcp":
            b = run.binding_snapshot
            ResourceTransaction(self._session, run.actor_id, run.conversation_id).bind(
                {"service_id": b["server_id"], "binding_version": b["binding_version"], "endpoint_digest": b["endpoint_digest"]},
                "invocation:" + run.invocation_run_id)
        try:
            self._session.add(InvocationRunRow(**_run_values(run)))
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

    def update(
        self,
        run: InvocationRun,
        *,
        expected_status: InvocationStatus,
        expected_claim_token: str | None = None,
    ) -> InvocationRun | None:
        from .ml_resources import lock_conversation
        lock_conversation(self._session, run.actor_id, run.conversation_id,
                          writable=run.status.value in ("PENDING", "RUNNING", "PENDING_CONFIRMATION"))
        predicates = [
            InvocationRunRow.invocation_run_id == run.invocation_run_id,
            InvocationRunRow.status == expected_status.value,
            InvocationRunRow.version == run.version,
        ]
        if expected_claim_token is not None:
            predicates.append(InvocationRunRow.execution_claim_token == expected_claim_token)
        try:
            row = self._session.scalar(
                update(InvocationRunRow)
                .where(*predicates)
                .values(**{**_run_values(run, include_identity=False), "version": run.version + 1})
                .returning(InvocationRunRow)
            )
            return None if row is None else _run_from_row(row)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)


def _run_values(run: InvocationRun, *, include_identity: bool = True) -> dict[str, object]:
    values: dict[str, object] = {
        "binding_snapshot": _plain(run.binding_snapshot),
        "remote_operation": _plain(run.remote_operation),
        "remote_receipt": _plain(run.remote_receipt),
        "version": run.version,
        "actor_id": run.actor_id,
        "conversation_id": run.conversation_id,
        "source_message_id": run.source_message_id,
        "task_id": run.task_id,
        "retry_of_invocation_run_id": run.retry_of_invocation_run_id,
        "request_id": run.request_id,
        "idempotency_key": run.idempotency_key,
        "trigger": run.trigger.value,
        "tool_id": run.tool_id,
        "tool_version": run.tool_version,
        "schema_hash": run.schema_hash,
        "execution_profile": run.execution_profile.value,
        "executor_id": run.executor_id,
        "proposed_arguments": _plain(run.proposed_arguments),
        "policy_snapshot": _plain(run.policy_snapshot),
        "tool_projection": _plain(run.tool_projection),
        "status": run.status.value,
        "confirmation_required": run.confirmation_required,
        "confirmation_expires_at": run.confirmation_expires_at,
        "confirmed_at": run.confirmed_at,
        "confirmed_by": run.confirmed_by,
        "rejected_at": run.rejected_at,
        "rejected_by": run.rejected_by,
        "expired_at": run.expired_at,
        "authorization_checked_at": run.authorization_checked_at,
        "denied_at": run.denied_at,
        "execution_claim_token": run.execution_claim_token,
        "execution_lease_expires_at": run.execution_lease_expires_at,
        "dispatch_started_at": run.dispatch_started_at,
        "execution_attempt_count": run.execution_attempt_count,
        "invocation_result_id": run.invocation_result_id,
        "managed_tool_run_id": run.managed_tool_run_id,
        "error_code": run.error_code,
        "safe_error_message": run.safe_error_message,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
    }
    if include_identity:
        values["invocation_run_id"] = run.invocation_run_id
    return values
