from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pathlib import Path
from sqlalchemy import inspect

from materialsagent.infrastructure.config import (
    AppSettings,
    ConfigurationError,
)
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
)

EXPECTED_REVISION = "0001_create_actor"
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


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


def test_actor_migration_round_trip_has_one_head_and_exact_schema(
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
        assert set(inspector.get_table_names(schema="public")) == {
            "actor",
            "alembic_version",
        }

        columns = {
            column["name"]: column
            for column in inspector.get_columns("actor")
        }
        assert set(columns) == {
            "actor_id",
            "user_id",
            "actor_origin",
            "created_at",
            "linked_at",
        }
        assert columns["actor_id"]["nullable"] is False
        assert columns["user_id"]["nullable"] is True
        assert columns["actor_origin"]["nullable"] is False
        assert columns["created_at"]["nullable"] is False
        assert columns["linked_at"]["nullable"] is True
        assert columns["created_at"]["type"].timezone is True
        assert columns["linked_at"]["type"].timezone is True

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
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    downgraded_engine = create_engine_from_settings(temporary_database)
    try:
        assert "actor" not in inspect(downgraded_engine).get_table_names(
            schema="public"
        )
    finally:
        downgraded_engine.dispose()

    command.upgrade(config, "head")
    assert _current_revision(temporary_database) == EXPECTED_REVISION


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
