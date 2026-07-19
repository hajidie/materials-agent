from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from datetime import datetime, timezone
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
EXPECTED_REVISION = "0003_task_time_order"
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
        assert set(inspector.get_table_names(schema="public")) == M3_TABLES
        version_columns = _column_map(inspector, "alembic_version")
        assert version_columns["version_num"]["type"].length >= len(
            EXPECTED_REVISION
        )

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
                "created_at",
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

        expected_foreign_keys = {
            "conversation": {"fk_conversation_actor": "actor"},
            "task": {
                "fk_task_actor": "actor",
                "fk_task_conversation": "conversation",
            },
            "message": {
                "fk_message_actor": "actor",
                "fk_message_conversation": "conversation",
                "fk_message_task": "task",
            },
            "task_input_revision": {
                "fk_task_input_revision_task": "task",
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
            assert all(
                constraint["options"].get("ondelete") == "RESTRICT"
                for constraint in foreign_keys.values()
            )

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
    finally:
        engine.dispose()

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
