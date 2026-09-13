"""Support conversation-owned EBSD input assets."""
from alembic import op
import sqlalchemy as sa

revision = "0016_ebsd_uploaded_assets"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("asset", sa.Column("conversation_id", sa.Text(), nullable=True))
    op.create_foreign_key("fk_asset_conversation", "asset", "conversation", ["conversation_id"], ["conversation_id"], ondelete="CASCADE")
    op.alter_column("asset", "task_id", nullable=True)
    op.alter_column("asset", "producer_tool_run_id", nullable=True)
    op.drop_constraint("ck_asset_type_sem_image", "asset", type_="check")
    op.drop_constraint("ck_asset_generated_source", "asset", type_="check")
    op.create_check_constraint("ck_asset_type_sem_image", "asset", "asset_type IN ('sem_image', 'ebsd_image')")
    op.create_check_constraint("ck_asset_generated_source", "asset",
        "(asset_type = 'sem_image' AND source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL AND task_id IS NOT NULL AND conversation_id IS NULL) OR "
        "(asset_type = 'ebsd_image' AND source_type = 'UPLOADED' AND producer_tool_run_id IS NULL AND task_id IS NULL AND conversation_id IS NOT NULL AND role = 'supporting')")
    op.alter_column("conversation_object_cleanup", "producer_tool_run_id", nullable=True)


def downgrade():
    # Do not silently discard user uploads when downgrading.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM asset WHERE asset_type = 'ebsd_image' LIMIT 1")).first():
        raise RuntimeError("Remove EBSD conversations through the application before downgrade.")
    if connection.execute(sa.text("SELECT 1 FROM conversation_object_cleanup WHERE producer_tool_run_id IS NULL LIMIT 1")).first():
        raise RuntimeError("EBSD cleanup records prevent this downgrade.")
    op.alter_column("conversation_object_cleanup", "producer_tool_run_id", nullable=False)
    op.drop_constraint("ck_asset_generated_source", "asset", type_="check")
    op.drop_constraint("ck_asset_type_sem_image", "asset", type_="check")
    op.create_check_constraint("ck_asset_type_sem_image", "asset", "asset_type = 'sem_image'")
    op.create_check_constraint("ck_asset_generated_source", "asset", "source_type = 'GENERATED' AND producer_tool_run_id IS NOT NULL")
    op.alter_column("asset", "task_id", nullable=False)
    op.alter_column("asset", "producer_tool_run_id", nullable=False)
    op.drop_constraint("fk_asset_conversation", "asset", type_="foreignkey")
    op.drop_column("asset", "conversation_id")
