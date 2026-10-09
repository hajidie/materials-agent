"""Generic image assets and durable TC4 batch items."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_tc4_batch_segmentation"
down_revision = "0026_managed_outcome_unknown"
branch_labels = None
depends_on = None
TC4 = "tc4_primary_alpha_segmentation"


def upgrade():
    op.create_table("runtime_receipt_cleanup", sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False), sa.Column("request", JSONB(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','COMPLETED')", name="ck_runtime_receipt_cleanup_status"))
    op.drop_constraint("ck_asset_type_sem_image", "asset", type_="check")
    op.create_check_constraint("ck_asset_type_sem_image", "asset", "asset_type IN ('sem_image','ebsd_image','image')")
    op.drop_constraint("ck_asset_generated_source", "asset", type_="check")
    op.create_check_constraint("ck_asset_generated_source", "asset",
        "(asset_type IN ('sem_image','image') AND source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL AND task_id IS NOT NULL AND conversation_id IS NULL) OR "
        "(asset_type IN ('ebsd_image','image') AND source_type = 'UPLOADED' AND producer_tool_run_id IS NULL AND task_id IS NULL AND conversation_id IS NOT NULL AND role = 'supporting')")
    op.create_table("tool_run_item",
        sa.Column("item_id", sa.Text(), primary_key=True),
        sa.Column("tool_run_id", sa.Text(), sa.ForeignKey("tool_run.tool_run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Text(), sa.ForeignKey("actor.actor_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("input_asset_id", sa.Text(), sa.ForeignKey("asset.asset_id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.UniqueConstraint("tool_run_id", "ordinal", name="uq_tool_run_item_order"),
        sa.UniqueConstraint("request_id", name="uq_tool_run_item_request"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 9", name="ck_tool_run_item_ordinal"),
        sa.CheckConstraint("version >= 0", name="ck_tool_run_item_version"),
        sa.CheckConstraint("status IN ('NOT_DISPATCHED','DISPATCHED','RESULT_RECEIVED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','SKIPPED')", name="ck_tool_run_item_status"))
    for name in ("producer_tool_run_item_id", "input_asset_id", "model_version", "preprocessing_version"):
        op.add_column("asset", sa.Column(name, sa.Text(), nullable=True))
    op.create_foreign_key("fk_asset_item", "asset", "tool_run_item", ["producer_tool_run_item_id"], ["item_id"], ondelete="CASCADE")
    op.create_foreign_key("fk_asset_input", "asset", "asset", ["input_asset_id"], ["asset_id"], ondelete="CASCADE")
    op.drop_constraint("ck_tool_run_output_sets_disjoint", "tool_run", type_="check")
    op.create_check_constraint("ck_tool_run_output_sets_disjoint", "tool_run",
        f"tool_id = '{TC4}' OR NOT (completed_outputs && failed_outputs)")
    op.drop_constraint("ck_tool_result_output_sets", "tool_result", type_="check")
    op.create_check_constraint("ck_tool_result_output_sets", "tool_result",
        "completed_outputs <@ requested_outputs AND failed_outputs <@ requested_outputs "
        f"AND (tool_id = '{TC4}' OR NOT (completed_outputs && failed_outputs)) "
        "AND requested_outputs <@ (completed_outputs || failed_outputs)")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM asset WHERE asset_type = 'image' UNION ALL SELECT 1 FROM tool_run_item UNION ALL SELECT 1 FROM runtime_receipt_cleanup WHERE status = 'PENDING' LIMIT 1")).first():
        raise RuntimeError("Preserve image and batch evidence; remove owned conversations before downgrading.")
    op.drop_constraint("ck_tool_result_output_sets", "tool_result", type_="check")
    op.create_check_constraint("ck_tool_result_output_sets", "tool_result", "completed_outputs <@ requested_outputs AND failed_outputs <@ requested_outputs AND NOT (completed_outputs && failed_outputs) AND requested_outputs <@ (completed_outputs || failed_outputs)")
    op.drop_constraint("ck_tool_run_output_sets_disjoint", "tool_run", type_="check")
    op.create_check_constraint("ck_tool_run_output_sets_disjoint", "tool_run", "NOT (completed_outputs && failed_outputs)")
    op.drop_constraint("fk_asset_item", "asset", type_="foreignkey")
    op.drop_constraint("fk_asset_input", "asset", type_="foreignkey")
    for name in ("producer_tool_run_item_id", "input_asset_id", "model_version", "preprocessing_version"):
        op.drop_column("asset", name)
    op.drop_table("tool_run_item")
    op.drop_table("runtime_receipt_cleanup")
    op.drop_constraint("ck_asset_type_sem_image", "asset", type_="check")
    op.create_check_constraint("ck_asset_type_sem_image", "asset", "asset_type IN ('sem_image','ebsd_image')")
    op.drop_constraint("ck_asset_generated_source", "asset", type_="check")
    op.create_check_constraint("ck_asset_generated_source", "asset",
        "(asset_type = 'sem_image' AND source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL AND task_id IS NOT NULL AND conversation_id IS NULL) OR "
        "(asset_type = 'ebsd_image' AND source_type = 'UPLOADED' AND producer_tool_run_id IS NULL AND task_id IS NULL AND conversation_id IS NOT NULL AND role = 'supporting')")
