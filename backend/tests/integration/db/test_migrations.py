from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from materialsagent.infrastructure.config import (
    AppSettings,
    ConfigurationError,
)
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
)

BASE_REVISION = "0001_create_actor"
PREVIOUS_REVISION = "0002_create_conversation_message_task_revision"
M3_REVISION = "0003_task_time_order"
M4_REVISION = "0004_llm_call"
M5_REVISION = "0005_tool_run"
IMMEDIATE_PREVIOUS_REVISION = "0006_asset"
M8_REVISION = "0008_idempotency_record"
M9_REVISION = "0009_timeline_query_indexes"
M10_REVISION = "0010_registry_routing_state"
EXPECTED_REVISION = "0013_conversation_delete"
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
NEW_TASK_TIME_CHECKS = {
    "ck_task_started_at_not_before_created_at",
    "ck_task_completed_at_not_before_started_at",
}
M3_TABLES = {
    "actor",
    "conversation",
    "message",
    "task",
    "task_input_revision",
    "alembic_version",
}
M4A_TABLES = M3_TABLES | {"llm_call"}
M5_TABLES = M4A_TABLES | {"tool_run"}
M6_TABLES = M5_TABLES | {"asset"}
M7_TABLES = M6_TABLES | {
    "tool_result",
    "result_asset_link",
    "natural_language_explanation",
}
M8_TABLES = M7_TABLES | {
    "idempotency_record",
    "conversation_object_cleanup",
}


def _make_alembic_config(settings: AppSettings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _current_revision(settings: AppSettings) -> str | None:
    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _column_map(inspector: object, table_name: str) -> dict[str, object]:
    return {
        column["name"]: column
        for column in inspector.get_columns(table_name)  # type: ignore[attr-defined]
    }


def _task_check_names(engine: Engine) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(engine).get_check_constraints("task")
    }


def _insert_task(
    connection: Connection,
    *,
    task_id: str,
    created_at: datetime,
    started_at: datetime | None,
    updated_at: datetime,
    completed_at: datetime | None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO task ("
            "task_id, conversation_id, actor_id, task_type, current_status, "
            "selected_tool_run_id, selected_result_id, created_at, started_at, "
            "updated_at, completed_at, error_code, safe_error_message"
            ") VALUES ("
            ":task_id, :conversation_id, :actor_id, NULL, 'PENDING', "
            "NULL, NULL, :created_at, :started_at, :updated_at, :completed_at, "
            "NULL, NULL"
            ")"
        ),
        {
            "task_id": task_id,
            "conversation_id": "conversation_time_checks",
            "actor_id": "actor_time_checks",
            "created_at": created_at,
            "started_at": started_at,
            "updated_at": updated_at,
            "completed_at": completed_at,
        },
    )


def test_migration_round_trip_has_one_head_and_exact_schema(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [EXPECTED_REVISION]

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION

    engine = create_engine_from_settings(temporary_database)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names(schema="public")) == M8_TABLES
        version_columns = _column_map(inspector, "alembic_version")
        assert version_columns["version_num"]["type"].length >= len(
            EXPECTED_REVISION
        )
        idempotency_columns = _column_map(
            inspector,
            "idempotency_record",
        )
        assert set(idempotency_columns) == {
            "idempotency_record_id",
            "actor_id",
            "operation",
            "idempotency_key",
            "request_digest",
            "first_request_id",
            "conversation_id",
            "task_id",
            "message_id",
            "task_input_revision_id",
            "tool_run_id",
            "explanation_id",
            "created_at",
            "expires_at",
        }
        assert idempotency_columns["expires_at"]["nullable"] is True
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints(
                "idempotency_record"
            )
        } == {
            "uq_idempotency_first_request": ["first_request_id"],
            "uq_idempotency_scope_key": [
                "actor_id",
                "operation",
                "idempotency_key",
            ],
        }
        assert {
            item["name"]: item["referred_table"]
            for item in inspector.get_foreign_keys("idempotency_record")
        } == {
            "fk_idempotency_actor": "actor",
            "fk_idempotency_conversation": "conversation",
            "fk_idempotency_explanation": (
                "natural_language_explanation"
            ),
            "fk_idempotency_message": "message",
            "fk_idempotency_revision": "task_input_revision",
            "fk_idempotency_task": "task",
            "fk_idempotency_tool_run": "tool_run",
        }
        assert {
            item["name"]
            for item in inspector.get_check_constraints(
                "idempotency_record"
            )
        } == {
            "ck_idempotency_actor_id_not_blank",
            "ck_idempotency_digest_sha256",
            "ck_idempotency_first_request_not_blank",
            "ck_idempotency_key_safe",
            "ck_idempotency_operation_allowed",
            "ck_idempotency_operation_binding",
            "ck_idempotency_record_id_not_blank",
        }
        assert {
            item["name"]
            for item in inspector.get_indexes("idempotency_record")
        } == {
            "ix_idempotency_scope_created",
            "ix_idempotency_task_created",
            "uq_idempotency_first_request",
            "uq_idempotency_scope_key",
        }

        actor_columns = _column_map(inspector, "actor")
        assert set(actor_columns) == {
            "actor_id",
            "user_id",
            "actor_origin",
            "created_at",
            "linked_at",
        }
        assert actor_columns["actor_id"]["nullable"] is False
        assert actor_columns["user_id"]["nullable"] is True
        assert actor_columns["actor_origin"]["nullable"] is False
        assert actor_columns["created_at"]["nullable"] is False
        assert actor_columns["linked_at"]["nullable"] is True
        assert actor_columns["created_at"]["type"].timezone is True
        assert actor_columns["linked_at"]["type"].timezone is True

        primary_key = inspector.get_pk_constraint("actor")
        assert primary_key["constrained_columns"] == ["actor_id"]

        unique_constraints = inspector.get_unique_constraints("actor")
        assert all(
            constraint["column_names"] != ["user_id"]
            for constraint in unique_constraints
        )

        check_definitions = " ".join(
            constraint["sqltext"]
            for constraint in inspector.get_check_constraints("actor")
        )
        assert "actor_origin" in check_definitions
        assert "LOCAL_ANONYMOUS" in check_definitions

        expected_columns = {
            "conversation": {
                "conversation_id",
                "actor_id",
                "title",
                "created_at",
                "updated_at",
            },
            "task": {
                "task_id",
                "conversation_id",
                "actor_id",
                "task_type",
                "current_status",
                "tool_id",
                "bound_tool_version",
                "bound_schema_hash",
                "selected_tool_run_id",
                "selected_result_id",
                "created_at",
                "started_at",
                "updated_at",
                "completed_at",
                "error_code",
                "safe_error_message",
            },
            "message": {
                "message_id",
                "conversation_id",
                "task_id",
                "actor_id",
                "request_id",
                "role",
                "generation_source",
                "content_text",
                "structured_content",
                "llm_call_id",
                "created_at",
            },
            "task_input_revision": {
                "task_input_revision_id",
                "task_id",
                "request_id",
                "source_llm_call_id",
                "source_message_ids",
                "revision",
                "raw_input",
                "normalized_input",
                "missing_fields",
                "ambiguous_fields",
                "validation_errors",
                "candidate_tool_refs",
                "created_at",
            },
            "llm_call": {
                "llm_call_id",
                "task_id",
                "conversation_id",
                "request_id",
                "purpose",
                "input_result_id",
                "provider",
                "model_name",
                "prompt_template_id",
                "prompt_template_version",
                "prompt_digest",
                "catalog_snapshot_refs",
                "catalog_hash",
                "tool_context_ref",
                "context_snapshot",
                "generation_parameters",
                "structured_output_summary",
                "usage",
                "provider_request_id",
                "status",
                "created_at",
                "started_at",
                "completed_at",
                "duration_ms",
                "error_code",
                "safe_error_message",
            },
            "tool_run": {
                "tool_run_id",
                "task_id",
                "request_id",
                "task_input_revision_id",
                "input_revision_no",
                "attempt_no",
                "tool_id",
                "tool_version",
                "schema_hash",
                "normalized_input_snapshot",
                "execution_policy_snapshot",
                "requested_outputs",
                "completed_outputs",
                "failed_outputs",
                "execution_input",
                "actual_runtime_parameters",
                "diagnostics",
                "output_summary",
                "current_status",
                "model_bundle_id",
                "created_at",
                "started_at",
                "completed_at",
                "duration_ms",
                "error_code",
                "safe_error_message",
            },
            "asset": {
                "asset_id",
                "task_id",
                "producer_tool_run_id",
                "actor_id",
                "operation_id",
                "current_status",
                "asset_type",
                "source_type",
                "role",
                    "storage_identity_version",
                    "storage_bucket",
                    "storage_namespace",
                    "object_key",
                "media_type",
                "width",
                "height",
                "bit_depth",
                "sha256",
                "size_bytes",
                "encoding_rule",
                "pending_since",
                "created_at",
                "available_at",
                "failed_at",
                "orphaned_at",
                "error_code",
                "safe_error_message",
                "orphan_reason",
                "orphan_details",
            },
            "conversation_object_cleanup": {
                "cleanup_id",
                "actor_id",
                "conversation_id",
                "asset_id",
                "operation_id",
                "producer_tool_run_id",
                "object_key",
                "bucket",
                "storage_namespace",
                "identity_version",
                "status",
                "attempts",
                "created_at",
                "last_attempt_at",
                "completed_at",
                "safety_error_code",
            },
            "tool_result": {
                "result_id",
                "task_id",
                "tool_run_id",
                "actor_id",
                "status",
                "requested_outputs",
                "completed_outputs",
                "failed_outputs",
                "data",
                "warnings",
                "provenance",
                "error",
                "tool_id",
                "tool_version",
                "schema_hash",
                "created_at",
            },
            "result_asset_link": {
                "result_id",
                "asset_id",
                "artifact_order",
                "created_at",
            },
            "natural_language_explanation": {
                "explanation_id",
                "task_id",
                "result_id",
                "llm_call_id",
                "attempt_no",
                "status",
                "language",
                "text",
                "created_at",
                "started_at",
                "completed_at",
                "duration_ms",
                "error_code",
                "safe_error_message",
            },
        }
        for table_name, column_names in expected_columns.items():
            assert set(_column_map(inspector, table_name)) == column_names

        conversation_columns = _column_map(inspector, "conversation")
        assert conversation_columns["title"]["nullable"] is True
        assert conversation_columns["created_at"]["type"].timezone is True
        assert conversation_columns["updated_at"]["type"].timezone is True

        task_columns = _column_map(inspector, "task")
        for nullable_name in {
            "task_type",
            "selected_tool_run_id",
            "selected_result_id",
            "started_at",
            "completed_at",
            "error_code",
            "safe_error_message",
            "tool_id",
            "bound_tool_version",
            "bound_schema_hash",
        }:
            assert task_columns[nullable_name]["nullable"] is True
        for timestamp_name in {
            "created_at",
            "started_at",
            "updated_at",
            "completed_at",
        }:
            assert task_columns[timestamp_name]["type"].timezone is True

        message_columns = _column_map(inspector, "message")
        assert message_columns["structured_content"]["nullable"] is True
        assert message_columns["llm_call_id"]["nullable"] is True
        assert isinstance(message_columns["structured_content"]["type"], JSONB)
        assert message_columns["created_at"]["type"].timezone is True

        revision_columns = _column_map(inspector, "task_input_revision")
        assert revision_columns["source_llm_call_id"]["nullable"] is True
        assert revision_columns["normalized_input"]["nullable"] is True
        assert isinstance(revision_columns["source_message_ids"]["type"], ARRAY)
        assert isinstance(revision_columns["missing_fields"]["type"], ARRAY)
        for jsonb_name in {
            "raw_input",
            "normalized_input",
            "ambiguous_fields",
            "validation_errors",
        }:
            assert isinstance(revision_columns[jsonb_name]["type"], JSONB)
        assert revision_columns["created_at"]["type"].timezone is True

        llm_call_columns = _column_map(inspector, "llm_call")
        for jsonb_name in {
            "generation_parameters",
            "structured_output_summary",
            "usage",
        }:
            assert isinstance(llm_call_columns[jsonb_name]["type"], JSONB)
        for timestamp_name in {"created_at", "started_at", "completed_at"}:
            assert llm_call_columns[timestamp_name]["type"].timezone is True
        assert llm_call_columns["input_result_id"]["nullable"] is True

        tool_run_columns = _column_map(inspector, "tool_run")
        for array_name in {
            "requested_outputs",
            "completed_outputs",
            "failed_outputs",
        }:
            assert isinstance(tool_run_columns[array_name]["type"], ARRAY)
        for jsonb_name in {
            "execution_input",
            "normalized_input_snapshot",
            "actual_runtime_parameters",
            "diagnostics",
            "output_summary",
        }:
            assert isinstance(tool_run_columns[jsonb_name]["type"], JSONB)
        for timestamp_name in {"created_at", "started_at", "completed_at"}:
            assert tool_run_columns[timestamp_name]["type"].timezone is True

        asset_columns = _column_map(inspector, "asset")
        assert isinstance(asset_columns["orphan_details"]["type"], JSONB)
        for timestamp_name in {
            "pending_since",
            "created_at",
            "available_at",
            "failed_at",
            "orphaned_at",
        }:
            assert asset_columns[timestamp_name]["type"].timezone is True

        expected_foreign_keys = {
            "conversation": {"fk_conversation_actor": "actor"},
            "task": {
                "fk_task_actor": "actor",
                "fk_task_conversation": "conversation",
                "fk_task_selected_result": "tool_result",
                "fk_task_selected_tool_run": "tool_run",
            },
            "message": {
                "fk_message_actor": "actor",
                "fk_message_conversation": "conversation",
                "fk_message_llm_call": "llm_call",
                "fk_message_task": "task",
            },
            "task_input_revision": {
                "fk_task_input_revision_llm_call": "llm_call",
                "fk_task_input_revision_task": "task",
            },
            "llm_call": {
                "fk_llm_call_conversation": "conversation",
                "fk_llm_call_input_result": "tool_result",
                "fk_llm_call_task": "task",
            },
            "tool_run": {
                "fk_tool_run_revision": "task_input_revision",
                "fk_tool_run_task": "task",
            },
            "asset": {
                "fk_asset_actor": "actor",
                "fk_asset_task": "task",
                "fk_asset_tool_run": "tool_run",
            },
            "tool_result": {
                "fk_tool_result_actor": "actor",
                "fk_tool_result_task": "task",
                "fk_tool_result_tool_run": "tool_run",
            },
            "result_asset_link": {
                "fk_result_asset_link_asset": "asset",
                "fk_result_asset_link_result": "tool_result",
            },
            "natural_language_explanation": {
                "fk_explanation_llm_call": "llm_call",
                "fk_explanation_result": "tool_result",
                "fk_explanation_task": "task",
            },
        }
        expected_ondelete = {
            "conversation": {"fk_conversation_actor": "RESTRICT"},
            "task": {
                "fk_task_actor": "RESTRICT",
                "fk_task_conversation": "CASCADE",
                "fk_task_selected_result": "SET NULL",
                "fk_task_selected_tool_run": "SET NULL",
            },
            "message": {
                "fk_message_actor": "RESTRICT",
                "fk_message_conversation": "CASCADE",
                "fk_message_llm_call": "RESTRICT",
                "fk_message_task": "CASCADE",
            },
            "task_input_revision": {
                "fk_task_input_revision_llm_call": "SET NULL",
                "fk_task_input_revision_task": "CASCADE",
            },
            "llm_call": {
                "fk_llm_call_conversation": "CASCADE",
                "fk_llm_call_input_result": "SET NULL",
                "fk_llm_call_task": "CASCADE",
            },
            "tool_run": {
                "fk_tool_run_revision": "CASCADE",
                "fk_tool_run_task": "CASCADE",
            },
            "asset": {
                "fk_asset_actor": "RESTRICT",
                "fk_asset_task": "CASCADE",
                "fk_asset_tool_run": "CASCADE",
            },
            "tool_result": {
                "fk_tool_result_actor": "RESTRICT",
                "fk_tool_result_task": "CASCADE",
                "fk_tool_result_tool_run": "CASCADE",
            },
            "result_asset_link": {
                "fk_result_asset_link_asset": "CASCADE",
                "fk_result_asset_link_result": "CASCADE",
            },
            "natural_language_explanation": {
                "fk_explanation_llm_call": "CASCADE",
                "fk_explanation_result": "CASCADE",
                "fk_explanation_task": "CASCADE",
            },
        }
        for table_name, expected in expected_foreign_keys.items():
            foreign_keys = {
                constraint["name"]: constraint
                for constraint in inspector.get_foreign_keys(table_name)
            }
            assert {
                name: constraint["referred_table"]
                for name, constraint in foreign_keys.items()
            } == expected
            assert {
                name: constraint["options"].get("ondelete")
                for name, constraint in foreign_keys.items()
            } == expected_ondelete[table_name]

        expected_check_names = {
            "conversation": {
                "ck_conversation_actor_id_not_blank",
                "ck_conversation_conversation_id_not_blank",
                "ck_conversation_title_not_blank",
                "ck_conversation_updated_at_not_before_created_at",
            },
            "task": {
                "ck_task_actor_id_not_blank",
                "ck_task_completed_at_not_before_created_at",
                "ck_task_completed_at_not_before_started_at",
                "ck_task_conversation_id_not_blank",
                "ck_task_current_status_allowed",
                "ck_task_tool_binding_all_or_none",
                "ck_task_tool_id_not_blank",
                "ck_task_bound_tool_version_not_blank",
                "ck_task_bound_schema_hash_sha256",
                "ck_task_selected_result_id_not_blank",
                "ck_task_selected_result_requires_tool_run",
                "ck_task_selected_tool_run_id_not_blank",
                "ck_task_started_at_not_before_created_at",
                "ck_task_task_id_not_blank",
                "ck_task_task_type_allowed",
                "ck_task_updated_at_not_before_created_at",
            },
            "message": {
                "ck_message_actor_id_not_blank",
                "ck_message_content_text_not_blank",
                "ck_message_conversation_id_not_blank",
                "ck_message_generation_source_allowed",
                "ck_message_llm_call_id_not_blank",
                "ck_message_llm_source_role",
                "ck_message_message_id_not_blank",
                "ck_message_request_id_not_blank",
                "ck_message_role_allowed",
                "ck_message_task_id_not_blank",
                "ck_message_template_source_role",
                "ck_message_user_role_source",
                "ck_message_user_source_role",
            },
            "task_input_revision": {
                "ck_task_input_revision_ambiguous_fields_array",
                "ck_task_input_revision_candidate_tool_refs_array",
                "ck_task_input_revision_normalized_input_object",
                "ck_task_input_revision_raw_input_object",
                "ck_task_input_revision_request_id_not_blank",
                "ck_task_input_revision_revision_id_not_blank",
                "ck_task_input_revision_revision_positive",
                "ck_task_input_revision_source_llm_call_id_not_blank",
                "ck_task_input_revision_source_message_ids_nonempty",
                "ck_task_input_revision_task_id_not_blank",
                "ck_task_input_revision_validation_errors_array",
            },
            "llm_call": {
                "ck_llm_call_completed_not_before_started",
                "ck_llm_call_context_snapshot_object",
                "ck_llm_call_catalog_hash_sha256",
                "ck_llm_call_catalog_snapshot_refs_array",
                "ck_llm_call_conversation_id_not_blank",
                "ck_llm_call_duration_nonnegative",
                "ck_llm_call_error_code_not_blank",
                "ck_llm_call_failed_requires_error",
                "ck_llm_call_generation_parameters_object",
                "ck_llm_call_input_result_id_not_blank",
                "ck_llm_call_input_result_purpose",
                "ck_llm_call_llm_call_id_not_blank",
                "ck_llm_call_model_name_not_blank",
                "ck_llm_call_prompt_digest_sha256",
                "ck_llm_call_prompt_template_id_not_blank",
                "ck_llm_call_prompt_template_version_not_blank",
                "ck_llm_call_provider_not_blank",
                "ck_llm_call_provider_request_id_not_blank",
                "ck_llm_call_purpose_allowed",
                "ck_llm_call_request_id_not_blank",
                "ck_llm_call_safe_error_message_not_blank",
                "ck_llm_call_started_not_before_created",
                "ck_llm_call_status_allowed",
                "ck_llm_call_status_time_shape",
                "ck_llm_call_structured_output_summary_object",
                "ck_llm_call_succeeded_without_error",
                "ck_llm_call_task_id_not_blank",
                "ck_llm_call_tool_context_ref_object",
                "ck_llm_call_usage_object",
            },
            "tool_run": {
                "ck_tool_run_attempt_positive",
                "ck_tool_run_completed_not_before_started",
                "ck_tool_run_completed_subset",
                "ck_tool_run_diagnostics_array",
                "ck_tool_run_duration_nonnegative",
                "ck_tool_run_error_code_not_blank",
                "ck_tool_run_execution_input_object",
                "ck_tool_run_execution_policy_snapshot_allowed",
                "ck_tool_run_failed_shape",
                "ck_tool_run_failed_subset",
                "ck_tool_run_id_not_blank",
                "ck_tool_run_input_revision_no_positive",
                "ck_tool_run_model_bundle_not_blank",
                "ck_tool_run_normalized_input_snapshot_object",
                "ck_tool_run_output_sets_disjoint",
                "ck_tool_run_output_summary_object",
                "ck_tool_run_request_id_not_blank",
                "ck_tool_run_requested_outputs_nonempty",
                "ck_tool_run_revision_id_not_blank",
                "ck_tool_run_runtime_parameters_object",
                "ck_tool_run_safe_error_not_blank",
                "ck_tool_run_schema_hash_sha256",
                "ck_tool_run_started_not_before_created",
                "ck_tool_run_status_allowed",
                "ck_tool_run_status_time_shape",
                "ck_tool_run_task_id_not_blank",
                "ck_tool_run_tool_id_not_blank",
                "ck_tool_run_tool_version_not_blank",
            },
            "asset": {
                "ck_asset_actor_id_not_blank",
                "ck_asset_available_not_before_created",
                "ck_asset_bit_depth_positive",
                "ck_asset_encoding_rule_not_blank",
                "ck_asset_error_code_not_blank",
                "ck_asset_failed_not_before_created",
                "ck_asset_generated_source",
                "ck_asset_height_positive",
                "ck_asset_id_not_blank",
                "ck_asset_media_type_not_blank",
                "ck_asset_object_key_not_blank",
                "ck_asset_operation_id_not_blank",
                "ck_asset_orphan_details_object",
                "ck_asset_orphan_reason_not_blank",
                "ck_asset_orphaned_not_before_created",
                "ck_asset_pending_not_before_created",
                "ck_asset_role_allowed",
                "ck_asset_safe_error_not_blank",
                "ck_asset_sha256_format",
                "ck_asset_size_nonnegative",
                "ck_asset_status_allowed",
                    "ck_asset_status_shape",
                    "ck_asset_metadata_v1_storage_identity",
                    "ck_asset_storage_bucket_not_blank",
                    "ck_asset_storage_identity_version_allowed",
                    "ck_asset_storage_namespace_not_blank",
                "ck_asset_task_id_not_blank",
                "ck_asset_tool_run_id_not_blank",
                "ck_asset_type_sem_image",
                "ck_asset_width_positive",
            },
            "tool_result": {
                "ck_tool_result_actor_id_not_blank",
                "ck_tool_result_data_object",
                "ck_tool_result_error_object",
                "ck_tool_result_id_not_blank",
                "ck_tool_result_output_sets",
                "ck_tool_result_provenance_object",
                "ck_tool_result_requested_nonempty",
                "ck_tool_result_schema_hash_sha256",
                "ck_tool_result_status_allowed",
                "ck_tool_result_status_shape",
                "ck_tool_result_task_id_not_blank",
                "ck_tool_result_tool_id_not_blank",
                "ck_tool_result_tool_run_id_not_blank",
                "ck_tool_result_tool_version_not_blank",
                "ck_tool_result_warnings_array",
            },
        }
        for table_name, expected_names in expected_check_names.items():
            constraints = inspector.get_check_constraints(table_name)
            assert {constraint["name"] for constraint in constraints} == expected_names

        task_check_sql = " ".join(
            constraint["sqltext"]
            for constraint in inspector.get_check_constraints("task")
        )
        for value in {
            "PENDING",
            "RUNNING",
            "NEEDS_INPUT",
            "READY",
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
            "KNOWLEDGE_QA",
            "TOOL_EXECUTION",
        }:
            assert value in task_check_sql

        message_check_sql = " ".join(
            constraint["sqltext"]
            for constraint in inspector.get_check_constraints("message")
        )
        for value in {"USER", "ASSISTANT", "LLM", "TEMPLATE"}:
            assert value in message_check_sql

        assert {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("message")
        } == {"uq_message_llm_call_id": ["llm_call_id"]}
        assert {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints(
                "task_input_revision"
            )
        } == {
            "uq_task_input_revision_task_revision": ["task_id", "revision"]
        }
        assert {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("llm_call")
        } == {
            "ix_llm_call_task_request_created": [
                "task_id",
                "request_id",
                "created_at",
            ]
        }
        assert {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("tool_run")
        } == {"uq_tool_run_task_attempt": ["task_id", "attempt_no"]}
        assert {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("asset")
        } == {
            "uq_asset_object_key": ["object_key"],
            "uq_asset_operation_id": ["operation_id"],
        }
        asset_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("asset")
            if index.get("duplicates_constraint") is None
        }
        assert asset_indexes == {
            "ix_asset_status_pending": ["current_status", "pending_since"],
            "ix_asset_tool_run_created": [
                "producer_tool_run_id",
                "created_at",
                "asset_id",
            ],
        }
    finally:
        engine.dispose()

    command.downgrade(config, IMMEDIATE_PREVIOUS_REVISION)
    immediate_previous_engine = create_engine_from_settings(temporary_database)
    try:
        immediate_inspector = inspect(immediate_previous_engine)
        assert set(immediate_inspector.get_table_names(schema="public")) == M6_TABLES
        assert NEW_TASK_TIME_CHECKS <= _task_check_names(
            immediate_previous_engine
        )
        assert "fk_message_llm_call" in {
            constraint["name"]
            for constraint in immediate_inspector.get_foreign_keys("message")
        }
        assert "fk_task_input_revision_llm_call" in {
            constraint["name"]
            for constraint in immediate_inspector.get_foreign_keys(
                "task_input_revision"
            )
        }
        assert "fk_task_selected_tool_run" in {
            constraint["name"]
            for constraint in immediate_inspector.get_foreign_keys("task")
        }
        assert "asset" in immediate_inspector.get_table_names(schema="public")
        assert "tool_result" not in immediate_inspector.get_table_names(
            schema="public"
        )
    finally:
        immediate_previous_engine.dispose()
    assert _current_revision(temporary_database) == IMMEDIATE_PREVIOUS_REVISION

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION

    command.downgrade(config, PREVIOUS_REVISION)
    previous_engine = create_engine_from_settings(temporary_database)
    try:
        assert set(
            inspect(previous_engine).get_table_names(schema="public")
        ) == M3_TABLES
        assert NEW_TASK_TIME_CHECKS.isdisjoint(
            _task_check_names(previous_engine)
        )
    finally:
        previous_engine.dispose()
    assert _current_revision(temporary_database) == PREVIOUS_REVISION

    command.upgrade(config, "head")
    restored_engine = create_engine_from_settings(temporary_database)
    try:
        assert NEW_TASK_TIME_CHECKS <= _task_check_names(restored_engine)
    finally:
        restored_engine.dispose()
    assert _current_revision(temporary_database) == EXPECTED_REVISION


def test_asset_migration_preserves_existing_tables_and_rows(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M5_REVISION)
    engine = create_engine_from_settings(temporary_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actor (actor_id, actor_origin, created_at) "
                    "VALUES ('actor_preserved', 'LOCAL_ANONYMOUS', :created_at)"
                ),
                {"created_at": datetime(2026, 7, 19, tzinfo=timezone.utc)},
            )
            before_oids = dict(
                connection.execute(
                    text(
                        "SELECT relname, oid FROM pg_class "
                        "WHERE relname = ANY(:table_names)"
                    ),
                    {"table_names": sorted(M5_TABLES - {"alembic_version"})},
                ).all()
            )
    finally:
        engine.dispose()

    command.upgrade(config, IMMEDIATE_PREVIOUS_REVISION)
    upgraded_engine = create_engine_from_settings(temporary_database)
    try:
        with upgraded_engine.connect() as connection:
            after_oids = dict(
                connection.execute(
                    text(
                        "SELECT relname, oid FROM pg_class "
                        "WHERE relname = ANY(:table_names)"
                    ),
                    {"table_names": sorted(M5_TABLES - {"alembic_version"})},
                ).all()
            )
            assert after_oids == before_oids
            assert connection.scalar(
                text("SELECT count(*) FROM actor WHERE actor_id='actor_preserved'")
            ) == 1
            assert "tool_result" not in inspect(connection).get_table_names()
            assert "tool_run" in inspect(connection).get_table_names()
            assert "asset" in inspect(connection).get_table_names()
    finally:
        upgraded_engine.dispose()

    command.upgrade(config, "head")
    command.check(config)

    command.downgrade(config, BASE_REVISION)
    downgraded_engine = create_engine_from_settings(temporary_database)
    try:
        assert set(
            inspect(downgraded_engine).get_table_names(schema="public")
        ) == {"actor", "alembic_version"}
    finally:
        downgraded_engine.dispose()
    assert _current_revision(temporary_database) == BASE_REVISION

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION


def test_task_time_order_checks_reject_invalid_rows_and_database_recovers(
    migrated_database_engine: Engine,
) -> None:
    created_at = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    with migrated_database_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO actor (actor_id, actor_origin, created_at) "
                "VALUES (:actor_id, 'LOCAL_ANONYMOUS', :created_at)"
            ),
            {"actor_id": "actor_time_checks", "created_at": created_at},
        )
        connection.execute(
            text(
                "INSERT INTO conversation ("
                "conversation_id, actor_id, title, created_at, updated_at"
                ") VALUES ("
                ":conversation_id, :actor_id, NULL, :created_at, :updated_at"
                ")"
            ),
            {
                "conversation_id": "conversation_time_checks",
                "actor_id": "actor_time_checks",
                "created_at": created_at,
                "updated_at": created_at,
            },
        )

    invalid_cases = (
        (
            "task_started_before_created",
            datetime(2026, 7, 19, 9, 59, tzinfo=timezone.utc),
            created_at,
            None,
            "ck_task_started_at_not_before_created_at",
        ),
        (
            "task_completed_before_started",
            datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 19, 10, 30, tzinfo=timezone.utc),
            "ck_task_completed_at_not_before_started_at",
        ),
    )
    for task_id, started_at, updated_at, completed_at, constraint_name in (
        invalid_cases
    ):
        with migrated_database_engine.connect() as connection:
            transaction = connection.begin()
            try:
                with pytest.raises(IntegrityError, match=constraint_name):
                    _insert_task(
                        connection,
                        task_id=task_id,
                        created_at=created_at,
                        started_at=started_at,
                        updated_at=updated_at,
                        completed_at=completed_at,
                    )
            finally:
                transaction.rollback()

        with migrated_database_engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1

    with migrated_database_engine.begin() as connection:
        _insert_task(
            connection,
            task_id="task_valid_time_order",
            created_at=created_at,
            started_at=datetime(2026, 7, 19, 10, 30, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
        )

    with migrated_database_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM task")) == 1


def test_database_url_uses_structured_components_and_hides_password() -> None:
    password = "p@ss:/word?#with-special-characters"
    settings = AppSettings(
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="materialsagent",
        postgres_user="user@local",
        postgres_password=password,
    )

    url = build_postgres_url(settings)

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "user@local"
    assert url.password == password
    assert url.database == "materialsagent"
    assert password not in str(url)
    assert "***" in str(url)


def test_incomplete_database_configuration_raises_safe_error() -> None:
    settings = AppSettings(postgres_password="not-printed")

    try:
        build_postgres_url(settings)
    except ConfigurationError as error:
        message = str(error)
    else:
        raise AssertionError("Expected database configuration to be rejected.")

    assert message == "PostgreSQL configuration is incomplete."
    assert "not-printed" not in message


def test_m7_result_and_explanation_migration_round_trip(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [EXPECTED_REVISION]

    command.upgrade(config, "0006_asset")
    historical_engine = create_engine_from_settings(temporary_database)
    try:
        created_at = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
        with historical_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actor (actor_id, actor_origin, created_at) "
                    "VALUES ('actor_1', 'LOCAL_ANONYMOUS', :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO conversation (conversation_id, actor_id, title, "
                    "created_at, updated_at) VALUES ('conversation_1', 'actor_1', "
                    "NULL, :created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO task (task_id, conversation_id, actor_id, "
                    "task_type, current_status, selected_tool_run_id, "
                    "selected_result_id, created_at, started_at, updated_at, "
                    "completed_at, error_code, safe_error_message) VALUES "
                    "('task_1', 'conversation_1', 'actor_1', 'TOOL_EXECUTION', "
                    "'RUNNING', NULL, NULL, :created_at, :created_at, "
                    ":created_at, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO task_input_revision (task_input_revision_id, "
                    "task_id, request_id, source_llm_call_id, source_message_ids, "
                    "revision, raw_input, normalized_input, missing_fields, "
                    "ambiguous_fields, validation_errors, created_at) VALUES "
                    "('revision_1', 'task_1', 'request_1', NULL, "
                    "ARRAY['message_1'], 1, '{}'::jsonb, "
                    "'{\"material\":\"ZTA35G\"}'::jsonb, ARRAY[]::text[], "
                    "'[]'::jsonb, '[]'::jsonb, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO tool_run (tool_run_id, task_id, request_id, "
                    "task_input_revision_id, attempt_no, tool_id, tool_version, "
                    "schema_version, requested_outputs, completed_outputs, "
                    "failed_outputs, execution_input, actual_runtime_parameters, "
                    "diagnostics, output_summary, current_status, model_bundle_id, "
                    "created_at, started_at, completed_at, duration_ms, error_code, "
                    "safe_error_message) VALUES ('tool_run_1', 'task_1', "
                    "'request_1', 'revision_1', 1, 'zta35g_sem_virtual_lab', "
                    "'0.1.0', '1.0', ARRAY['sem_image'], ARRAY['sem_image'], "
                    "ARRAY[]::text[], '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, "
                    "'{}'::jsonb, 'SUCCEEDED', NULL, :created_at, :started_at, "
                    ":completed_at, 1000, NULL, NULL)"
                ),
                {
                    "created_at": created_at,
                    "started_at": created_at + timedelta(seconds=1),
                    "completed_at": created_at + timedelta(seconds=2),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO asset (asset_id, task_id, producer_tool_run_id, "
                    "actor_id, operation_id, current_status, asset_type, source_type, "
                    "role, object_key, media_type, width, height, bit_depth, sha256, "
                    "size_bytes, encoding_rule, pending_since, created_at, "
                    "available_at, failed_at, orphaned_at, error_code, "
                    "safe_error_message, orphan_reason, orphan_details) VALUES "
                    "('asset_1', 'task_1', 'tool_run_1', 'actor_1', "
                    "'operation_1', 'AVAILABLE', 'sem_image', 'GENERATED', "
                    "'requested_output', 'assets/test/asset_1.png', 'image/png', "
                    "512, 512, 8, :sha256, 1480, 'linear-png', :created_at, "
                    ":created_at, :available_at, NULL, NULL, NULL, NULL, NULL, NULL)"
                ),
                {
                    "sha256": "c" * 64,
                    "created_at": created_at,
                    "available_at": created_at + timedelta(seconds=3),
                },
            )
    finally:
        historical_engine.dispose()

    command.upgrade(config, "0007_tool_result_explanation")
    engine = create_engine_from_settings(temporary_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tool_result (result_id, task_id, tool_run_id, "
                    "actor_id, status, requested_outputs, completed_outputs, "
                    "failed_outputs, data, warnings, provenance, error, tool_id, "
                    "tool_version, schema_version, created_at) VALUES "
                    "('result_migration_1', 'task_1', 'tool_run_1', 'actor_1', "
                    "'SUCCEEDED', ARRAY['sem_image'], ARRAY['sem_image'], "
                    "ARRAY[]::text[], '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, NULL, "
                    "'zta35g_sem_virtual_lab', '0.1.0', '1.0', :created_at)"
                ),
                {"created_at": created_at + timedelta(seconds=4)},
            )
            connection.execute(
                text(
                    "INSERT INTO result_asset_link (result_id, asset_id, "
                    "artifact_order, created_at) VALUES ('result_migration_1', "
                    "'asset_1', 0, :created_at)"
                ),
                {"created_at": created_at + timedelta(seconds=4)},
            )
            connection.execute(
                text(
                    "INSERT INTO llm_call (llm_call_id, task_id, conversation_id, "
                    "request_id, purpose, input_result_id, provider, model_name, "
                    "prompt_template_id, prompt_template_version, prompt_digest, "
                    "generation_parameters, structured_output_summary, usage, "
                    "provider_request_id, status, created_at, started_at, "
                    "completed_at, duration_ms, error_code, safe_error_message) "
                    "VALUES ('llm_explanation_migration_1', 'task_1', "
                    "'conversation_1', 'request_explanation_1', "
                    "'TOOL_RESULT_EXPLANATION', 'result_migration_1', 'mock', "
                    "'mock-v1', 'tool-result-explanation', '1', :digest, "
                    "'{}'::jsonb, NULL, NULL, NULL, 'SUCCEEDED', :created_at, "
                    ":started_at, :completed_at, 1000, NULL, NULL)"
                ),
                {
                    "digest": "d" * 64,
                    "created_at": created_at + timedelta(seconds=4),
                    "started_at": created_at + timedelta(seconds=5),
                    "completed_at": created_at + timedelta(seconds=6),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO natural_language_explanation (explanation_id, "
                    "task_id, result_id, llm_call_id, attempt_no, status, language, "
                    "text, created_at, started_at, completed_at, duration_ms, "
                    "error_code, safe_error_message) VALUES "
                    "('explanation_migration_1', 'task_1', 'result_migration_1', "
                    "'llm_explanation_migration_1', 1, 'SUCCEEDED', 'zh-CN', "
                    "'Migration preserved explanation.', :created_at, :started_at, "
                    ":completed_at, 1000, NULL, NULL)"
                ),
                {
                    "created_at": created_at + timedelta(seconds=4),
                    "started_at": created_at + timedelta(seconds=5),
                    "completed_at": created_at + timedelta(seconds=6),
                },
            )
            connection.execute(
                text(
                    "UPDATE task SET selected_tool_run_id = 'tool_run_1', "
                    "selected_result_id = 'result_migration_1' "
                    "WHERE task_id = 'task_1'"
                )
            )

        inspector = inspect(engine)
        assert {
            "tool_result",
            "result_asset_link",
            "natural_language_explanation",
        } <= set(inspector.get_table_names(schema="public"))
        task_fks = {
            item["name"]: item["referred_table"]
            for item in inspector.get_foreign_keys("task")
        }
        llm_fks = {
            item["name"]: item["referred_table"]
            for item in inspector.get_foreign_keys("llm_call")
        }
        assert task_fks["fk_task_selected_result"] == "tool_result"
        assert llm_fks["fk_llm_call_input_result"] == "tool_result"
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints("tool_result")
        } == {"uq_tool_result_tool_run_id": ["tool_run_id"]}
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints("result_asset_link")
        } == {
            "uq_result_asset_link_result_order": [
                "result_id",
                "artifact_order",
            ]
        }
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_unique_constraints(
                "natural_language_explanation"
            )
        } == {
            "uq_explanation_llm_call_id": ["llm_call_id"],
            "uq_explanation_result_attempt": ["result_id", "attempt_no"],
        }
        explanation_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints(
                "natural_language_explanation"
            )
        }
        failed_shape = explanation_checks["ck_explanation_status_shape"]
        assert "safe_error_message IS NOT NULL" in failed_shape
        assert "length(text) <= 4096" in failed_shape
        llm_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("llm_call")
        }
        provider_request_check = llm_checks[
            "ck_llm_call_provider_request_id_not_blank"
        ]
        assert "length(provider_request_id) <= 256" in provider_request_check
        assert "idempotency_record" not in inspector.get_table_names()
        assert "event" not in inspector.get_table_names()
        with engine.connect() as connection:
            task_selected = connection.execute(
                text(
                    "SELECT selected_tool_run_id, selected_result_id "
                    "FROM task WHERE task_id = 'task_1'"
                )
            ).mappings().one()
            assert task_selected == {
                "selected_tool_run_id": "tool_run_1",
                "selected_result_id": "result_migration_1",
            }
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM tool_result "
                    "WHERE result_id = 'result_migration_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM result_asset_link "
                    "WHERE result_id = 'result_migration_1' "
                    "AND asset_id = 'asset_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM llm_call "
                    "WHERE llm_call_id = 'llm_explanation_migration_1' "
                    "AND purpose = 'TOOL_RESULT_EXPLANATION' "
                    "AND input_result_id = 'result_migration_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM natural_language_explanation "
                    "WHERE explanation_id = 'explanation_migration_1' "
                    "AND result_id = 'result_migration_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM tool_run "
                    "WHERE tool_run_id = 'tool_run_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM asset "
                    "WHERE asset_id = 'asset_1' "
                    "AND producer_tool_run_id = 'tool_run_1' "
                    "AND current_status = 'AVAILABLE'"
                )
            ) == 1
    finally:
        engine.dispose()

    command.downgrade(config, "0006_asset")
    downgraded = create_engine_from_settings(temporary_database)
    try:
        inspector = inspect(downgraded)
        assert "tool_result" not in inspector.get_table_names()
        assert "result_asset_link" not in inspector.get_table_names()
        assert "natural_language_explanation" not in inspector.get_table_names()
        assert "fk_task_selected_result" not in {
            item["name"] for item in inspector.get_foreign_keys("task")
        }
        assert "fk_llm_call_input_result" not in {
            item["name"] for item in inspector.get_foreign_keys("llm_call")
        }
        downgraded_llm_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("llm_call")
        }
        assert "length(provider_request_id) <= 256" not in (
            downgraded_llm_checks[
                "ck_llm_call_provider_request_id_not_blank"
            ]
        )
        with downgraded.connect() as connection:
            task_selected = connection.execute(
                text(
                    "SELECT selected_tool_run_id, selected_result_id "
                    "FROM task WHERE task_id = 'task_1'"
                )
            ).mappings().one()
            assert task_selected == {
                "selected_tool_run_id": "tool_run_1",
                "selected_result_id": None,
            }
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM llm_call "
                    "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
                )
            ) == 0
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM llm_call "
                    "WHERE input_result_id IS NOT NULL"
                )
            ) == 0
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM tool_run "
                    "WHERE tool_run_id = 'tool_run_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM asset "
                    "WHERE asset_id = 'asset_1'"
                )
            ) == 1
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION
    reupgraded = create_engine_from_settings(temporary_database)
    try:
        with reupgraded.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM tool_run "
                    "WHERE tool_run_id = 'tool_run_1'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM asset "
                    "WHERE asset_id = 'asset_1' "
                    "AND producer_tool_run_id = 'tool_run_1' "
                    "AND current_status = 'AVAILABLE'"
                )
            ) == 1
            assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
            assert connection.scalar(
                text("SELECT count(*) FROM natural_language_explanation")
            ) == 0
    finally:
        reupgraded.dispose()


def test_m8_idempotency_migration_downgrade_only_removes_m8_table(
    temporary_database: AppSettings,
) -> None:
    from materialsagent.domain.models.actor import Actor
    from materialsagent.domain.models.conversation import Conversation
    from materialsagent.domain.models.idempotency_record import (
        IdempotencyRecord,
    )
    from materialsagent.domain.models.message import Message
    from materialsagent.domain.models.task import Task
    from materialsagent.infrastructure.db.session import (
        create_session_factory,
    )
    from materialsagent.infrastructure.db.unit_of_work import (
        SQLAlchemyUnitOfWork,
    )

    config = _make_alembic_config(temporary_database)
    command.upgrade(config, "head")
    engine = create_engine_from_settings(temporary_database)
    created_at = datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc)
    try:
        with SQLAlchemyUnitOfWork(
            create_session_factory(engine)
        ) as unit_of_work:
            unit_of_work.actors.add(
                Actor.local_anonymous(
                    "actor_m8_round_trip",
                    created_at=created_at,
                )
            )
            unit_of_work.conversations.add(
                Conversation(
                    conversation_id="conversation_m8_round_trip",
                    actor_id="actor_m8_round_trip",
                    title=None,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            unit_of_work.tasks.add(
                Task.pending(
                    task_id="task_m8_round_trip",
                    conversation_id="conversation_m8_round_trip",
                    actor_id="actor_m8_round_trip",
                    created_at=created_at,
                )
            )
            unit_of_work.messages.add(
                Message.user(
                    message_id="message_m8_round_trip",
                    conversation_id="conversation_m8_round_trip",
                    task_id="task_m8_round_trip",
                    actor_id="actor_m8_round_trip",
                    request_id="request_m8_round_trip",
                    content_text="round trip",
                    created_at=created_at,
                )
            )
            unit_of_work.idempotency_records.add(
                IdempotencyRecord(
                    idempotency_record_id="idempotency_m8_round_trip",
                    actor_id="actor_m8_round_trip",
                    operation="TASK_CREATE",
                    idempotency_key="m8-round-trip",
                    request_digest="a" * 64,
                    first_request_id="request_m8_round_trip",
                    task_id="task_m8_round_trip",
                    message_id="message_m8_round_trip",
                    task_input_revision_id=None,
                    tool_run_id=None,
                    explanation_id=None,
                    created_at=created_at,
                    expires_at=None,
                )
            )
            unit_of_work.commit()
    finally:
        engine.dispose()

    command.downgrade(config, "0007_tool_result_explanation")
    downgraded = create_engine_from_settings(temporary_database)
    try:
        assert "idempotency_record" not in inspect(
            downgraded
        ).get_table_names()
        with downgraded.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM actor "
                    "WHERE actor_id = 'actor_m8_round_trip'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM task "
                    "WHERE task_id = 'task_m8_round_trip'"
                )
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM message "
                    "WHERE message_id = 'message_m8_round_trip'"
                )
            ) == 1
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    reupgraded = create_engine_from_settings(temporary_database)
    try:
        assert "idempotency_record" in inspect(
            reupgraded
        ).get_table_names()
        with reupgraded.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM idempotency_record")
            ) == 0
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM message "
                    "WHERE message_id = 'message_m8_round_trip'"
                )
            ) == 1
    finally:
        reupgraded.dispose()


def test_m9_timeline_indexes_round_trip_without_changing_rows(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M8_REVISION)
    engine = create_engine_from_settings(temporary_database)
    created_at = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actor (actor_id, actor_origin, created_at) "
                    "VALUES ('actor_m9_indexes', 'LOCAL_ANONYMOUS', "
                    ":created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO conversation ("
                    "conversation_id, actor_id, title, created_at, updated_at"
                    ") VALUES ("
                    "'conversation_m9_indexes', 'actor_m9_indexes', NULL, "
                    ":created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO task ("
                    "task_id, conversation_id, actor_id, task_type, "
                    "current_status, selected_tool_run_id, "
                    "selected_result_id, created_at, started_at, updated_at, "
                    "completed_at, error_code, safe_error_message"
                    ") VALUES ("
                    "'task_m9_indexes', 'conversation_m9_indexes', "
                    "'actor_m9_indexes', NULL, 'PENDING', NULL, NULL, "
                    ":created_at, NULL, :created_at, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO message ("
                    "message_id, conversation_id, task_id, actor_id, "
                    "request_id, role, generation_source, content_text, "
                    "structured_content, llm_call_id, created_at"
                    ") VALUES ("
                    "'message_m9_indexes', 'conversation_m9_indexes', "
                    "'task_m9_indexes', 'actor_m9_indexes', "
                    "'request_m9_indexes', 'USER', 'USER', 'index row', "
                    "NULL, NULL, :created_at)"
                ),
                {"created_at": created_at},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_engine_from_settings(temporary_database)
    try:
        inspector = inspect(upgraded)
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_indexes("message")
            if item.get("duplicates_constraint") is None
        } == {
            "ix_message_conversation_created": [
                "conversation_id",
                "created_at",
                "message_id",
            ]
        }
        assert {
            item["name"]: item["column_names"]
            for item in inspector.get_indexes("task")
            if item.get("duplicates_constraint") is None
        } == {
            "ix_task_conversation_created": [
                "conversation_id",
                "created_at",
                "task_id",
            ]
        }
        with upgraded.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM message "
                    "WHERE message_id = 'message_m9_indexes'"
                )
            ) == 1
    finally:
        upgraded.dispose()
    assert _current_revision(temporary_database) == EXPECTED_REVISION
    command.check(config)

    command.downgrade(config, M8_REVISION)
    downgraded = create_engine_from_settings(temporary_database)
    try:
        inspector = inspect(downgraded)
        assert "ix_message_conversation_created" not in {
            item["name"] for item in inspector.get_indexes("message")
        }
        assert "ix_task_conversation_created" not in {
            item["name"] for item in inspector.get_indexes("task")
        }
        with downgraded.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM task "
                    "WHERE task_id = 'task_m9_indexes'"
                )
            ) == 1
    finally:
        downgraded.dispose()
    assert _current_revision(temporary_database) == M8_REVISION

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION


def _seed_m10_run_for_m11(
    connection: Connection,
    *,
    suffix: str,
    tool_id: str,
    run_schema_version: str = "1.0",
    result_schema_version: str | None = None,
) -> None:
    created_at = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)
    schema_hash = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
    connection.execute(
        text(
            "INSERT INTO actor (actor_id, actor_origin, created_at) "
            "VALUES (:actor_id, 'LOCAL_ANONYMOUS', :created_at)"
        ),
        {"actor_id": f"actor_{suffix}", "created_at": created_at},
    )
    connection.execute(
        text(
            "INSERT INTO conversation (conversation_id, actor_id, title, "
            "created_at, updated_at) VALUES (:conversation_id, :actor_id, "
            "NULL, :created_at, :created_at)"
        ),
        {
            "conversation_id": f"conversation_{suffix}",
            "actor_id": f"actor_{suffix}",
            "created_at": created_at,
        },
    )
    connection.execute(
        text(
            "INSERT INTO task (task_id, conversation_id, actor_id, task_type, "
            "current_status, selected_tool_run_id, selected_result_id, created_at, "
            "started_at, updated_at, completed_at, error_code, safe_error_message, "
            "tool_id, bound_tool_version, bound_schema_hash) VALUES (:task_id, "
            ":conversation_id, :actor_id, 'TOOL_EXECUTION', 'PENDING', NULL, NULL, "
            ":created_at, NULL, :created_at, NULL, NULL, NULL, "
            "'zta35g_sem_virtual_lab', '1', :schema_hash)"
        ),
        {
            "task_id": f"task_{suffix}",
            "conversation_id": f"conversation_{suffix}",
            "actor_id": f"actor_{suffix}",
            "created_at": created_at,
            "schema_hash": schema_hash,
        },
    )
    connection.execute(
        text(
            "INSERT INTO task_input_revision (task_input_revision_id, task_id, "
            "request_id, source_llm_call_id, source_message_ids, revision, raw_input, "
            "normalized_input, missing_fields, ambiguous_fields, validation_errors, "
            "created_at, candidate_tool_refs) VALUES (:revision_id, :task_id, "
            ":request_id, NULL, ARRAY[CAST(:message_id AS text)], 1, '{}'::jsonb, "
            "'{\"material\":\"ZTA35G\"}'::jsonb, ARRAY[]::text[], '[]'::jsonb, "
            "'[]'::jsonb, :created_at, '[]'::jsonb)"
        ),
        {
            "revision_id": f"revision_{suffix}",
            "task_id": f"task_{suffix}",
            "request_id": f"request_{suffix}",
            "message_id": f"message_{suffix}",
            "created_at": created_at,
        },
    )
    connection.execute(
        text(
            "INSERT INTO tool_run (tool_run_id, task_id, request_id, "
            "task_input_revision_id, attempt_no, tool_id, tool_version, "
            "schema_version, requested_outputs, completed_outputs, failed_outputs, "
            "execution_input, actual_runtime_parameters, diagnostics, output_summary, "
            "current_status, model_bundle_id, created_at, started_at, completed_at, "
            "duration_ms, error_code, safe_error_message) VALUES (:run_id, :task_id, "
            ":request_id, :revision_id, 1, :tool_id, '0.1.0', :schema_version, "
            "ARRAY['sem_image'], ARRAY['sem_image'], ARRAY[]::text[], "
            "CAST(:execution_input AS jsonb), '{}'::jsonb, "
            "'[]'::jsonb, '{}'::jsonb, 'SUCCEEDED', NULL, :created_at, "
            ":started_at, :completed_at, 1000, NULL, NULL)"
        ),
        {
            "run_id": f"run_{suffix}",
            "task_id": f"task_{suffix}",
            "request_id": f"request_{suffix}",
            "revision_id": f"revision_{suffix}",
            "tool_id": tool_id,
            "schema_version": run_schema_version,
            "execution_input": '{"runtime_parameters":{"seed":101}}',
            "created_at": created_at,
            "started_at": created_at + timedelta(seconds=1),
            "completed_at": created_at + timedelta(seconds=2),
        },
    )
    if result_schema_version is not None:
        connection.execute(
            text(
                "INSERT INTO tool_result (result_id, task_id, tool_run_id, actor_id, "
                "status, requested_outputs, completed_outputs, failed_outputs, data, "
                "warnings, provenance, error, tool_id, tool_version, schema_version, "
                "created_at) VALUES (:result_id, :task_id, :run_id, :actor_id, "
                "'SUCCEEDED', ARRAY['sem_image'], ARRAY['sem_image'], "
                "ARRAY[]::text[], '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, NULL, "
                ":tool_id, '0.1.0', :schema_version, :created_at)"
            ),
            {
                "result_id": f"result_{suffix}",
                "task_id": f"task_{suffix}",
                "run_id": f"run_{suffix}",
                "actor_id": f"actor_{suffix}",
                "tool_id": tool_id,
                "schema_version": result_schema_version,
                "created_at": created_at + timedelta(seconds=2),
            },
        )


@pytest.mark.parametrize(
    (
        "suffix",
        "tool_id",
        "run_schema_version",
        "result_schema_version",
        "message",
    ),
    [
        (
            "m11_unknown",
            "unknown_tool",
            "1.0",
            None,
            "unknown Tool IDs",
        ),
        (
            "m11_unknown_schema",
            "zta35g_sem_virtual_lab",
            "2.0",
            None,
            "unknown historical ZTA schema versions",
        ),
        (
            "m11_conflict",
            "zta35g_sem_virtual_lab",
            "1.0",
            "2.0",
            "ToolRun and ToolResult provenance conflict",
        ),
    ],
)
def test_m11_aborts_on_unknown_or_conflicting_historical_provenance(
    temporary_database: AppSettings,
    suffix: str,
    tool_id: str,
    run_schema_version: str,
    result_schema_version: str | None,
    message: str,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M10_REVISION)
    engine = create_engine_from_settings(temporary_database)
    try:
        with engine.begin() as connection:
            _seed_m10_run_for_m11(
                connection,
                suffix=suffix,
                tool_id=tool_id,
                run_schema_version=run_schema_version,
                result_schema_version=result_schema_version,
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match=message):
        command.upgrade(config, "head")
    assert _current_revision(temporary_database) == M10_REVISION


def test_m11_allows_empty_object_execution_snapshots(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M10_REVISION)
    engine = create_engine_from_settings(temporary_database)
    try:
        with engine.begin() as connection:
            _seed_m10_run_for_m11(
                connection,
                suffix="m11_empty_snapshot",
                tool_id="zta35g_sem_virtual_lab",
            )
            connection.execute(
                text(
                    "UPDATE task_input_revision SET normalized_input = '{}'::jsonb "
                    "WHERE task_input_revision_id = "
                    "'revision_m11_empty_snapshot'"
                )
            )
            connection.execute(
                text(
                    "UPDATE tool_run SET execution_input = '{}'::jsonb "
                    "WHERE tool_run_id = 'run_m11_empty_snapshot'"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_engine_from_settings(temporary_database)
    try:
        with upgraded.connect() as connection:
            snapshots = connection.execute(
                text(
                    "SELECT normalized_input_snapshot, execution_input "
                    "FROM tool_run WHERE tool_run_id = "
                    "'run_m11_empty_snapshot'"
                )
            ).mappings().one()
            assert snapshots == {
                "normalized_input_snapshot": {},
                "execution_input": {},
            }
    finally:
        upgraded.dispose()


def test_m11_downgrade_refuses_unknown_tool_ids(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M10_REVISION)
    engine = create_engine_from_settings(temporary_database)
    try:
        with engine.begin() as connection:
            _seed_m10_run_for_m11(
                connection,
                suffix="m11_downgrade",
                tool_id="zta35g_sem_virtual_lab",
            )
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    upgraded = create_engine_from_settings(temporary_database)
    try:
        with upgraded.begin() as connection:
            connection.execute(
                text(
                    "UPDATE tool_run SET tool_id = 'unknown_tool' "
                    "WHERE tool_run_id = 'run_m11_downgrade'"
                )
            )
    finally:
        upgraded.dispose()

    with pytest.raises(RuntimeError, match="unknown Tool IDs"):
        command.downgrade(config, M10_REVISION)
    assert _current_revision(temporary_database) == EXPECTED_REVISION


def test_m10_backfills_only_known_zta_tasks_and_refuses_ready_downgrade(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M9_REVISION)
    engine = create_engine_from_settings(temporary_database)
    created_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actor (actor_id, actor_origin, created_at) "
                    "VALUES ('actor_m10', 'LOCAL_ANONYMOUS', :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO conversation ("
                    "conversation_id, actor_id, title, created_at, updated_at"
                    ") VALUES ('conversation_m10', 'actor_m10', NULL, "
                    ":created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            for task_id, task_type in (
                ("task_m10_tool", "TOOL_EXECUTION"),
                ("task_m10_knowledge", "KNOWLEDGE_QA"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO task ("
                        "task_id, conversation_id, actor_id, task_type, "
                        "current_status, selected_tool_run_id, "
                        "selected_result_id, created_at, started_at, "
                        "updated_at, completed_at, error_code, safe_error_message"
                        ") VALUES (:task_id, 'conversation_m10', 'actor_m10', "
                        ":task_type, 'PENDING', NULL, NULL, :created_at, NULL, "
                        ":created_at, NULL, NULL, NULL)"
                    ),
                    {"task_id": task_id, "task_type": task_type, "created_at": created_at},
                )
            connection.execute(
                text(
                    "INSERT INTO task_input_revision ("
                    "task_input_revision_id, task_id, request_id, "
                    "source_llm_call_id, source_message_ids, revision, raw_input, "
                    "normalized_input, missing_fields, ambiguous_fields, "
                    "validation_errors, created_at"
                    ") VALUES ('revision_m10', 'task_m10_tool', 'request_m10', "
                    "NULL, ARRAY['message_m10'], 1, '{}'::jsonb, "
                    "'{\"material\":\"ZTA35G\"}'::jsonb, "
                    "ARRAY[]::text[], '[]'::jsonb, '[]'::jsonb, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO tool_run ("
                    "tool_run_id, task_id, request_id, task_input_revision_id, "
                    "attempt_no, tool_id, tool_version, schema_version, "
                    "requested_outputs, completed_outputs, failed_outputs, "
                    "execution_input, actual_runtime_parameters, diagnostics, "
                    "output_summary, current_status, model_bundle_id, created_at, "
                    "started_at, completed_at, duration_ms, error_code, safe_error_message"
                    ") VALUES ('run_m10', 'task_m10_tool', 'request_m10', "
                    "'revision_m10', 1, 'zta35g_sem_virtual_lab', '0.1.0', '1.0', "
                    "ARRAY['sem_image'], ARRAY['sem_image'], ARRAY[]::text[], "
                    "CAST(:execution_input AS jsonb), "
                    "'{}'::jsonb, '[]'::jsonb, '{}'::jsonb, "
                    "'SUCCEEDED', NULL, :created_at, :started_at, :completed_at, "
                    "1000, NULL, NULL)"
                ),
                {
                    "created_at": created_at,
                    "started_at": created_at + timedelta(seconds=1),
                    "completed_at": created_at + timedelta(seconds=2),
                    "execution_input": '{"runtime_parameters":{"seed":101}}',
                },
            )
            connection.execute(
                text(
                    "INSERT INTO tool_result ("
                    "result_id, task_id, tool_run_id, actor_id, status, "
                    "requested_outputs, completed_outputs, failed_outputs, data, "
                    "warnings, provenance, error, tool_id, tool_version, "
                    "schema_version, created_at"
                    ") VALUES ('result_m10', 'task_m10_tool', 'run_m10', "
                    "'actor_m10', 'SUCCEEDED', ARRAY['sem_image'], "
                    "ARRAY['sem_image'], ARRAY[]::text[], '{}'::jsonb, "
                    "'[]'::jsonb, '{}'::jsonb, NULL, 'zta35g_sem_virtual_lab', "
                    "'0.1.0', '1.0', :created_at)"
                ),
                {"created_at": created_at + timedelta(seconds=2)},
            )
            connection.execute(
                text(
                    "INSERT INTO llm_call ("
                    "llm_call_id, task_id, conversation_id, request_id, purpose, "
                    "input_result_id, provider, model_name, prompt_template_id, "
                    "prompt_template_version, prompt_digest, generation_parameters, "
                    "structured_output_summary, usage, provider_request_id, status, "
                    "created_at, started_at, completed_at, duration_ms, error_code, "
                    "safe_error_message"
                    ") VALUES ('llm_m10', 'task_m10_tool', 'conversation_m10', "
                    "'request_m10', 'CHAT_ORCHESTRATION', NULL, 'mock', 'mock-v1', "
                    "NULL, NULL, NULL, '{}'::jsonb, "
                    "'{\"route\":\"TOOL_EXECUTION\",\"tool_id\":\"zta35g_sem_virtual_lab\"}'::jsonb, "
                    "NULL, NULL, 'PENDING', :created_at, NULL, NULL, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "UPDATE task SET selected_tool_run_id = 'run_m10', "
                    "selected_result_id = 'result_m10' WHERE task_id = 'task_m10_tool'"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_engine_from_settings(temporary_database)
    try:
        with upgraded.begin() as connection:
            binding = connection.execute(
                text(
                    "SELECT tool_id, bound_tool_version, bound_schema_hash "
                    "FROM task WHERE task_id = 'task_m10_tool'"
                )
            ).mappings().one()
            assert binding["tool_id"] == "zta35g_sem_virtual_lab"
            assert binding["bound_tool_version"] == "1"
            assert binding["bound_schema_hash"] == (
                "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
            )
            run_provenance = connection.execute(
                text(
                    "SELECT schema_hash, normalized_input_snapshot, "
                    "execution_policy_snapshot, input_revision_no "
                    "FROM tool_run WHERE tool_run_id = 'run_m10'"
                )
            ).mappings().one()
            assert run_provenance == {
                "schema_hash": (
                    "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
                ),
                "normalized_input_snapshot": {"material": "ZTA35G"},
                "execution_policy_snapshot": "ANY_TASK",
                "input_revision_no": 1,
            }
            assert connection.scalar(
                text(
                    "SELECT schema_hash FROM tool_result "
                    "WHERE result_id = 'result_m10'"
                )
            ) == run_provenance["schema_hash"]
            assert connection.scalar(
                text("SELECT count(*) FROM llm_call WHERE llm_call_id = 'llm_m10'")
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM tool_run WHERE tool_run_id = 'run_m10'")
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM tool_result WHERE result_id = 'result_m10'")
            ) == 1
            assert connection.execute(
                text(
                    "SELECT tool_id, bound_tool_version, bound_schema_hash "
                    "FROM task WHERE task_id = 'task_m10_knowledge'"
                )
            ).mappings().one() == {
                "tool_id": None,
                "bound_tool_version": None,
                "bound_schema_hash": None,
            }
    finally:
        upgraded.dispose()

    command.downgrade(config, M10_REVISION)
    ready_engine = create_engine_from_settings(temporary_database)
    try:
        with ready_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task SET current_status = 'READY' "
                    "WHERE task_id = 'task_m10_tool'"
                )
            )
    finally:
        ready_engine.dispose()

    with pytest.raises(RuntimeError, match="READY Tasks"):
        command.downgrade(config, M9_REVISION)

    assert _current_revision(temporary_database) == M10_REVISION
    legacy_engine = create_engine_from_settings(temporary_database)
    try:
        with legacy_engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT schema_version FROM tool_run "
                    "WHERE tool_run_id = 'run_m10'"
                )
            ) == "1.0"
            assert connection.scalar(
                text(
                    "SELECT schema_version FROM tool_result "
                    "WHERE result_id = 'result_m10'"
                )
            ) == "1.0"
    finally:
        legacy_engine.dispose()

    ready_engine = create_engine_from_settings(temporary_database)
    try:
        with ready_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task SET current_status = 'PENDING' "
                    "WHERE task_id = 'task_m10_tool'"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO llm_call ("
                    "llm_call_id, task_id, conversation_id, request_id, purpose, "
                    "input_result_id, provider, model_name, prompt_template_id, "
                    "prompt_template_version, prompt_digest, generation_parameters, "
                    "structured_output_summary, usage, provider_request_id, status, "
                    "created_at, started_at, completed_at, duration_ms, error_code, "
                    "safe_error_message"
                    ") VALUES ('llm_m10_extract', 'task_m10_tool', 'conversation_m10', "
                    "'request_m10_extract', 'TOOL_INPUT_EXTRACTION', NULL, 'mock', "
                    "'mock-v1', NULL, NULL, NULL, '{}'::jsonb, NULL, NULL, NULL, "
                    "'PENDING', :created_at, NULL, NULL, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
    finally:
        ready_engine.dispose()

    with pytest.raises(RuntimeError, match="TOOL_INPUT_EXTRACTION LLMCalls"):
        command.downgrade(config, M9_REVISION)

    extraction_engine = create_engine_from_settings(temporary_database)
    try:
        with extraction_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM llm_call WHERE llm_call_id = 'llm_m10_extract'")
            )
    finally:
        extraction_engine.dispose()

    command.downgrade(config, M9_REVISION)
    downgraded = create_engine_from_settings(temporary_database)
    try:
        assert {
            "tool_id",
            "bound_tool_version",
            "bound_schema_hash",
        }.isdisjoint(_column_map(inspect(downgraded), "task"))
    finally:
        downgraded.dispose()


def test_m10_aborts_on_conflicting_historical_tool_id(
    temporary_database: AppSettings,
) -> None:
    config = _make_alembic_config(temporary_database)
    command.upgrade(config, M9_REVISION)
    engine = create_engine_from_settings(temporary_database)
    created_at = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO actor (actor_id, actor_origin, created_at) "
                    "VALUES ('actor_m10_conflict', 'LOCAL_ANONYMOUS', :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO conversation ("
                    "conversation_id, actor_id, title, created_at, updated_at"
                    ") VALUES ('conversation_m10_conflict', 'actor_m10_conflict', "
                    "NULL, :created_at, :created_at)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO task ("
                    "task_id, conversation_id, actor_id, task_type, current_status, "
                    "selected_tool_run_id, selected_result_id, created_at, started_at, "
                    "updated_at, completed_at, error_code, safe_error_message"
                    ") VALUES ('task_m10_conflict', 'conversation_m10_conflict', "
                    "'actor_m10_conflict', 'TOOL_EXECUTION', 'PENDING', NULL, NULL, "
                    ":created_at, NULL, :created_at, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO llm_call ("
                    "llm_call_id, task_id, conversation_id, request_id, purpose, "
                    "input_result_id, provider, model_name, prompt_template_id, "
                    "prompt_template_version, prompt_digest, generation_parameters, "
                    "structured_output_summary, usage, provider_request_id, status, "
                    "created_at, started_at, completed_at, duration_ms, error_code, "
                    "safe_error_message"
                    ") VALUES ('llm_m10_conflict', 'task_m10_conflict', "
                    "'conversation_m10_conflict', 'request_m10_conflict', "
                    "'CHAT_ORCHESTRATION', NULL, 'mock', 'mock-v1', NULL, NULL, "
                    "NULL, '{}'::jsonb, "
                    "'{\"route\":\"TOOL_EXECUTION\",\"tool_id\":\"other_tool\"}'::jsonb, "
                    "NULL, NULL, 'PENDING', :created_at, NULL, NULL, NULL, NULL, NULL)"
                ),
                {"created_at": created_at},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="historical Tool IDs conflict"):
        command.upgrade(config, "head")
    assert _current_revision(temporary_database) == M9_REVISION
