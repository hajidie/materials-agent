"""Persist registry routing state without reconstructing historical catalogs.

Revision ID: 0010_registry_routing_state
Revises: 0009_timeline_query_indexes
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0010_registry_routing_state"
down_revision: str | None = "0009_timeline_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ZTA35G_TOOL_ID = "zta35g_sem_virtual_lab"
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


def upgrade() -> None:
    op.add_column("task", sa.Column("tool_id", sa.Text(), nullable=True))
    op.add_column(
        "task",
        sa.Column("bound_tool_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "task",
        sa.Column("bound_schema_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "task_input_revision",
        sa.Column(
            "candidate_tool_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "llm_call",
        sa.Column("catalog_snapshot_refs", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "llm_call",
        sa.Column("catalog_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "llm_call",
        sa.Column("tool_context_ref", postgresql.JSONB(), nullable=True),
    )

    bind = op.get_bind()
    conflicting_tasks = bind.scalar(
        sa.text(
            "SELECT count(*) FROM task t "
            "WHERE t.task_type = 'TOOL_EXECUTION' AND ("
            "EXISTS (SELECT 1 FROM tool_run tr "
            "WHERE tr.task_id = t.task_id AND tr.tool_id <> :tool_id) "
            "OR EXISTS (SELECT 1 FROM llm_call lc "
            "WHERE lc.task_id = t.task_id "
            "AND lc.structured_output_summary ? 'tool_id' "
            "AND lc.structured_output_summary ->> 'tool_id' <> :tool_id)"
            ")"
        ),
        {"tool_id": _ZTA35G_TOOL_ID},
    )
    if conflicting_tasks:
        raise RuntimeError(
            "Cannot safely backfill Task Tool bindings: historical Tool IDs conflict."
        )
    bind.execute(
        sa.text(
            "UPDATE task SET tool_id = :tool_id, bound_tool_version = '1', "
            "bound_schema_hash = :schema_hash "
            "WHERE task_type = 'TOOL_EXECUTION'"
        ),
        {"tool_id": _ZTA35G_TOOL_ID, "schema_hash": _zta35g_schema_hash()},
    )

    op.drop_constraint("ck_task_current_status_allowed", "task", type_="check")
    op.create_check_constraint(
        "ck_task_current_status_allowed",
        "task",
        "current_status IN ('PENDING', 'RUNNING', 'NEEDS_INPUT', 'READY', "
        "'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_task_tool_binding_all_or_none",
        "task",
        "(tool_id IS NULL AND bound_tool_version IS NULL "
        "AND bound_schema_hash IS NULL) OR "
        "(tool_id IS NOT NULL AND bound_tool_version IS NOT NULL "
        "AND bound_schema_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_task_tool_id_not_blank",
        "task",
        "tool_id IS NULL OR length(btrim(tool_id)) > 0",
    )
    op.create_check_constraint(
        "ck_task_bound_tool_version_not_blank",
        "task",
        "bound_tool_version IS NULL OR length(btrim(bound_tool_version)) > 0",
    )
    op.create_check_constraint(
        "ck_task_bound_schema_hash_sha256",
        "task",
        "bound_schema_hash IS NULL OR bound_schema_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_task_input_revision_candidate_tool_refs_array",
        "task_input_revision",
        "jsonb_typeof(candidate_tool_refs) = 'array'",
    )

    op.drop_constraint("ck_llm_call_purpose_allowed", "llm_call", type_="check")
    op.drop_constraint(
        "ck_llm_call_input_result_purpose", "llm_call", type_="check"
    )
    op.create_check_constraint(
        "ck_llm_call_purpose_allowed",
        "llm_call",
        "purpose IN ('CHAT_ORCHESTRATION', 'TOOL_RESULT_EXPLANATION', "
        "'TOOL_INPUT_EXTRACTION')",
    )
    op.create_check_constraint(
        "ck_llm_call_input_result_purpose",
        "llm_call",
        "(purpose = 'CHAT_ORCHESTRATION' AND input_result_id IS NULL) OR "
        "(purpose = 'TOOL_RESULT_EXPLANATION' AND input_result_id IS NOT NULL) OR "
        "(purpose = 'TOOL_INPUT_EXTRACTION' AND input_result_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_llm_call_catalog_snapshot_refs_array",
        "llm_call",
        "catalog_snapshot_refs IS NULL OR jsonb_typeof(catalog_snapshot_refs) = 'array'",
    )
    op.create_check_constraint(
        "ck_llm_call_catalog_hash_sha256",
        "llm_call",
        "catalog_hash IS NULL OR catalog_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_llm_call_tool_context_ref_object",
        "llm_call",
        "tool_context_ref IS NULL OR jsonb_typeof(tool_context_ref) = 'object'",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM task WHERE current_status = 'READY'")):
        raise RuntimeError(
            "Cannot downgrade registry routing state while READY Tasks exist."
        )
    if bind.scalar(
        sa.text(
            "SELECT count(*) FROM llm_call "
            "WHERE purpose = 'TOOL_INPUT_EXTRACTION'"
        )
    ):
        raise RuntimeError(
            "Cannot downgrade registry routing state while "
            "TOOL_INPUT_EXTRACTION LLMCalls exist."
        )

    op.drop_constraint("ck_llm_call_tool_context_ref_object", "llm_call", type_="check")
    op.drop_constraint("ck_llm_call_catalog_hash_sha256", "llm_call", type_="check")
    op.drop_constraint(
        "ck_llm_call_catalog_snapshot_refs_array", "llm_call", type_="check"
    )
    op.drop_constraint("ck_llm_call_input_result_purpose", "llm_call", type_="check")
    op.drop_constraint("ck_llm_call_purpose_allowed", "llm_call", type_="check")
    op.create_check_constraint(
        "ck_llm_call_purpose_allowed",
        "llm_call",
        "purpose IN ('CHAT_ORCHESTRATION', 'TOOL_RESULT_EXPLANATION')",
    )
    op.create_check_constraint(
        "ck_llm_call_input_result_purpose",
        "llm_call",
        "(purpose = 'CHAT_ORCHESTRATION' AND input_result_id IS NULL) OR "
        "(purpose = 'TOOL_RESULT_EXPLANATION' AND input_result_id IS NOT NULL)",
    )
    op.drop_column("llm_call", "tool_context_ref")
    op.drop_column("llm_call", "catalog_hash")
    op.drop_column("llm_call", "catalog_snapshot_refs")

    op.drop_constraint(
        "ck_task_input_revision_candidate_tool_refs_array",
        "task_input_revision",
        type_="check",
    )
    op.drop_column("task_input_revision", "candidate_tool_refs")
    op.drop_constraint("ck_task_bound_schema_hash_sha256", "task", type_="check")
    op.drop_constraint("ck_task_bound_tool_version_not_blank", "task", type_="check")
    op.drop_constraint("ck_task_tool_id_not_blank", "task", type_="check")
    op.drop_constraint("ck_task_tool_binding_all_or_none", "task", type_="check")
    op.drop_constraint("ck_task_current_status_allowed", "task", type_="check")
    op.create_check_constraint(
        "ck_task_current_status_allowed",
        "task",
        "current_status IN ('PENDING', 'RUNNING', 'NEEDS_INPUT', "
        "'SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED')",
    )
    op.drop_column("task", "bound_schema_hash")
    op.drop_column("task", "bound_tool_version")
    op.drop_column("task", "tool_id")
