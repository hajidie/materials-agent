"""Ordered, versioned item records under one Managed ToolRun."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from materialsagent.domain.models.tc4 import ToolRunItem
from .base import Base


class RuntimeReceiptCleanupRow(Base):
    """Deletion outbox survives conversation/Task cascades and service restarts."""
    __tablename__ = "runtime_receipt_cleanup"
    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    __table_args__ = (CheckConstraint("status IN ('PENDING','COMPLETED')", name="ck_runtime_receipt_cleanup_status"),)


class ToolRunItemRow(Base):
    __tablename__ = "tool_run_item"
    __table_args__ = (
        UniqueConstraint("tool_run_id", "ordinal", name="uq_tool_run_item_order"),
        UniqueConstraint("request_id", name="uq_tool_run_item_request"),
        CheckConstraint("ordinal BETWEEN 0 AND 9", name="ck_tool_run_item_ordinal"),
        CheckConstraint("version >= 0", name="ck_tool_run_item_version"),
        CheckConstraint("status IN ('NOT_DISPATCHED','DISPATCHED','RESULT_RECEIVED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','SKIPPED')", name="ck_tool_run_item_status"),
    )
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tool_run_id: Mapped[str] = mapped_column(ForeignKey("tool_run.tool_run_id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actor.actor_id", ondelete="RESTRICT"), nullable=False)
    input_asset_id: Mapped[str] = mapped_column(ForeignKey("asset.asset_id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ToolRunItemRepository:
    def __init__(self, session):
        self.session = session

    def list_for_tool_run(self, tool_run_id):
        rows = self.session.scalars(select(ToolRunItemRow).where(ToolRunItemRow.tool_run_id == tool_run_id)
            .order_by(ToolRunItemRow.ordinal)).all()
        return [ToolRunItem.model_validate_json(__import__("json").dumps(row.document)) for row in rows]

    def add(self, item):
        self.session.add(ToolRunItemRow(**{key: getattr(item, key) for key in
            ("item_id", "tool_run_id", "actor_id", "input_asset_id", "ordinal", "request_id", "status", "version")},
            document=item.model_dump(mode="json")))
        self.session.flush()

    def queue_cleanup(self, actor_id, conversation_id):
        from .conversation_task import TaskRow
        from .tool_run import ToolRunRow
        from materialsagent.application.errors import ConversationBusyError
        rows = self.session.scalars(select(ToolRunItemRow).join(ToolRunRow, ToolRunRow.tool_run_id == ToolRunItemRow.tool_run_id)
            .join(TaskRow, TaskRow.task_id == ToolRunRow.task_id)
            .where(TaskRow.actor_id == actor_id, TaskRow.conversation_id == conversation_id).with_for_update()).all()
        for row in rows:
            item = ToolRunItem.model_validate_json(__import__("json").dumps(row.document))
            if item.status in {"DISPATCHED", "OUTCOME_UNKNOWN", "RESULT_RECEIVED"}:
                raise ConversationBusyError(conversation_id=conversation_id)
            if item.receipt is not None and self.session.get(RuntimeReceiptCleanupRow, item.request_id) is None:
                request = item.receipt["request"]
                if request["conversation_id"] != conversation_id or request["item_id"] != item.item_id:
                    raise ValueError("Receipt ownership mismatch.")
                self.session.add(RuntimeReceiptCleanupRow(request_id=item.request_id, status="PENDING", request=request))

    def pending_cleanup(self, limit):
        return [(r.request_id, dict(r.request)) for r in self.session.scalars(select(RuntimeReceiptCleanupRow)
            .where(RuntimeReceiptCleanupRow.status == "PENDING").order_by(RuntimeReceiptCleanupRow.request_id).limit(limit)).all()]

    def complete_cleanup(self, request_id):
        row = self.session.get(RuntimeReceiptCleanupRow, request_id)
        if row:
            row.status = "COMPLETED"

    def update(self, item, *, expected_version):
        row = self.session.scalar(select(ToolRunItemRow).where(ToolRunItemRow.item_id == item.item_id).with_for_update())
        if row is None or row.version != expected_version or item.version != expected_version + 1:
            return None
        before = ToolRunItem.model_validate_json(__import__("json").dumps(row.document))
        if any(getattr(before, key) != getattr(item, key) for key in
               ("tool_run_id", "actor_id", "input_asset_id", "ordinal", "request_id", "input_sha256", "name", "created_at")):
            return None
        allowed = {"NOT_DISPATCHED": {"DISPATCHED", "FAILED", "SKIPPED"},
            "DISPATCHED": {"RESULT_RECEIVED", "OUTCOME_UNKNOWN", "NOT_DISPATCHED"},
            "OUTCOME_UNKNOWN": {"RESULT_RECEIVED"}, "RESULT_RECEIVED": {"SUCCEEDED", "FAILED"}}
        if item.status not in allowed.get(before.status, set()):
            return None
        row.status, row.version, row.document = item.status, item.version, item.model_dump(mode="json")
        if item.status == "DISPATCHED":
            self.session.info["tc4_new_dispatch"] = True
        self.session.flush()
        return item
