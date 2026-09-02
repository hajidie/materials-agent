"""Persist authorized ToolRun provenance snapshots.

Revision ID: 0011_tool_run_provenance
Revises: 0010_registry_routing_state
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011_tool_run_provenance"
down_revision: str | None = "0010_registry_routing_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ZTA35G_TOOL_ID = "zta35g_sem_virtual_lab"
_LEGACY_ZTA_SCHEMA_VERSION = "1.0"
_ZTA35G_INPUT_SCHEMA = {
    "type": "object",
    "required": [
        "material",
        "solution_temperature",
        "solution_time",
        "aging_temperature",
        "aging_time",
        "requested_outputs",
    ],
    "properties": {
        "material": {"const": "ZTA35G"},
        "solution_temperature": {
            "unit": "°C",
            "minimum": 900,
            "maximum": 1100,
            "precision": 0,
        },
        "solution_time": {
            "unit": "h",
            "minimum": 1,
            "maximum": 5,
            "precision": 1,
        },
        "aging_temperature": {
            "unit": "°C",
            "minimum": 670,
            "maximum": 790,
            "precision": 0,
        },
        "aging_time": {
            "unit": "h",
            "minimum": 1,
            "maximum": 5,
            "precision": 1,
        },
        "requested_outputs": {
            "enum": ["sem_image", "mechanical_properties"],
        },
    },
}


def _zta35g_schema_hash() -> str:
    canonical = json.dumps(
        _ZTA35G_INPUT_SCHEMA,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_upgrade_history() -> None:
    bind = op.get_bind()
    unknown_tool_count = bind.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM tool_run WHERE tool_id <> :tool_id) + "
            "(SELECT count(*) FROM tool_result WHERE tool_id <> :tool_id)"
        ),
        {"tool_id": _ZTA35G_TOOL_ID},
    )
    if unknown_tool_count:
        raise RuntimeError(
            "Cannot migrate ToolRun provenance with unknown Tool IDs."
        )
    result_conflicts = bind.scalar(
        sa.text(
            "SELECT count(*) FROM tool_result result "
            "JOIN tool_run run ON run.tool_run_id = result.tool_run_id "
            "WHERE result.task_id <> run.task_id "
            "OR result.tool_id <> run.tool_id "
            "OR result.tool_version <> run.tool_version "
            "OR result.schema_version <> run.schema_version"
        )
    )
    if result_conflicts:
        raise RuntimeError(
            "ToolRun and ToolResult provenance conflict; migration refused."
        )
    legacy_version_conflicts = bind.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM tool_run "
            "WHERE schema_version <> :schema_version) + "
            "(SELECT count(*) FROM tool_result "
            "WHERE schema_version <> :schema_version)"
        ),
        {"schema_version": _LEGACY_ZTA_SCHEMA_VERSION},
    )
    if legacy_version_conflicts:
        raise RuntimeError(
            "Cannot migrate unknown historical ZTA schema versions."
        )
    invalid_revision_snapshots = bind.scalar(
        sa.text(
            "SELECT count(*) FROM tool_run run "
            "LEFT JOIN task_input_revision revision "
            "ON revision.task_input_revision_id = run.task_input_revision_id "
            "WHERE revision.task_input_revision_id IS NULL "
            "OR revision.task_id <> run.task_id "
            "OR revision.revision <= 0 "
            "OR revision.normalized_input IS NULL "
            "OR jsonb_typeof(revision.normalized_input) <> 'object' "
            "OR jsonb_typeof(run.execution_input) <> 'object'"
        )
    )
    if invalid_revision_snapshots:
        raise RuntimeError(
            "Cannot backfill valid ToolRun execution snapshots."
        )


def upgrade() -> None:
    _validate_upgrade_history()
    schema_hash = _zta35g_schema_hash()

    op.alter_column(
        "tool_run",
        "schema_version",
        new_column_name="schema_hash",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "tool_result",
        "schema_version",
        new_column_name="schema_hash",
        existing_type=sa.Text(),
        existing_nullable=False,
        type_=sa.String(length=64),
    )
    op.add_column(
        "tool_run",
        sa.Column("input_revision_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tool_run",
        sa.Column(
            "normalized_input_snapshot",
            postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "tool_run",
        sa.Column(
            "execution_policy_snapshot",
            sa.String(length=32),
            nullable=True,
        ),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE tool_run run SET schema_hash = :schema_hash, "
            "input_revision_no = revision.revision, "
            "normalized_input_snapshot = revision.normalized_input, "
            "execution_policy_snapshot = 'ANY_TASK' "
            "FROM task_input_revision revision "
            "WHERE revision.task_input_revision_id = run.task_input_revision_id"
        ),
        {"schema_hash": schema_hash},
    )
    bind.execute(
        sa.text("UPDATE tool_result SET schema_hash = :schema_hash"),
        {"schema_hash": schema_hash},
    )
    incomplete_backfill = bind.scalar(
        sa.text(
            "SELECT count(*) FROM tool_run WHERE input_revision_no IS NULL "
            "OR normalized_input_snapshot IS NULL "
            "OR execution_policy_snapshot IS NULL"
        )
    )
    if incomplete_backfill:
        raise RuntimeError("ToolRun provenance backfill was incomplete.")

    op.alter_column(
        "tool_run",
        "input_revision_no",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "tool_run",
        "normalized_input_snapshot",
        existing_type=postgresql.JSONB(),
        nullable=False,
    )
    op.alter_column(
        "tool_run",
        "execution_policy_snapshot",
        existing_type=sa.String(length=32),
        nullable=False,
    )

    op.drop_constraint(
        "ck_tool_run_schema_version_not_blank",
        "tool_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_result_schema_version_not_blank",
        "tool_result",
        type_="check",
    )
    op.create_check_constraint(
        "ck_tool_run_schema_hash_sha256",
        "tool_run",
        "schema_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_tool_result_schema_hash_sha256",
        "tool_result",
        "schema_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_tool_run_input_revision_no_positive",
        "tool_run",
        "input_revision_no > 0",
    )
    op.create_check_constraint(
        "ck_tool_run_normalized_input_snapshot_object",
        "tool_run",
        "jsonb_typeof(normalized_input_snapshot) = 'object'",
    )
    op.create_check_constraint(
        "ck_tool_run_execution_policy_snapshot_allowed",
        "tool_run",
        "execution_policy_snapshot IN ('ANY_TASK', 'EXISTING_TASK_ONLY', 'NONE')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    unknown_tool_count = bind.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM tool_run WHERE tool_id <> :tool_id) + "
            "(SELECT count(*) FROM tool_result WHERE tool_id <> :tool_id)"
        ),
        {"tool_id": _ZTA35G_TOOL_ID},
    )
    if unknown_tool_count:
        raise RuntimeError(
            "Cannot downgrade ToolRun provenance with unknown Tool IDs."
        )

    op.drop_constraint(
        "ck_tool_run_execution_policy_snapshot_allowed",
        "tool_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_run_normalized_input_snapshot_object",
        "tool_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_run_input_revision_no_positive",
        "tool_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_result_schema_hash_sha256",
        "tool_result",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_run_schema_hash_sha256",
        "tool_run",
        type_="check",
    )
    op.drop_column("tool_run", "execution_policy_snapshot")
    op.drop_column("tool_run", "normalized_input_snapshot")
    op.drop_column("tool_run", "input_revision_no")

    bind.execute(
        sa.text("UPDATE tool_run SET schema_hash = :schema_version"),
        {"schema_version": _LEGACY_ZTA_SCHEMA_VERSION},
    )
    bind.execute(
        sa.text("UPDATE tool_result SET schema_hash = :schema_version"),
        {"schema_version": _LEGACY_ZTA_SCHEMA_VERSION},
    )
    op.alter_column(
        "tool_run",
        "schema_hash",
        new_column_name="schema_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "tool_result",
        "schema_hash",
        new_column_name="schema_version",
        existing_type=sa.String(length=64),
        existing_nullable=False,
        type_=sa.Text(),
    )
    op.create_check_constraint(
        "ck_tool_run_schema_version_not_blank",
        "tool_run",
        "length(btrim(schema_version)) > 0",
    )
    op.create_check_constraint(
        "ck_tool_result_schema_version_not_blank",
        "tool_result",
        "length(btrim(schema_version)) > 0",
    )
