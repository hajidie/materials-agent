from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re
import secrets

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from materialsagent.infrastructure.config import AppSettings, load_settings
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
)


BACKEND_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
TEST_DATABASE_PATTERN = re.compile(r"materialsagent_test_[0-9a-f]{16}\Z")


def make_alembic_config(settings: AppSettings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _database_engine(settings: AppSettings, database: str) -> Engine:
    return create_engine(
        build_postgres_url(settings, database=database),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )


@pytest.fixture(scope="session")
def postgres_settings() -> AppSettings:
    settings = load_settings()
    build_postgres_url(settings)
    return settings


@pytest.fixture
def temporary_database(
    postgres_settings: AppSettings,
) -> Iterator[AppSettings]:
    database_name = f"materialsagent_test_{secrets.token_hex(8)}"
    assert TEST_DATABASE_PATTERN.fullmatch(database_name)

    admin_engine = _database_engine(postgres_settings, "postgres")
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

        yield postgres_settings.model_copy(
            update={"postgres_db": database_name},
        )
    finally:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :database_name "
                    "AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database_name}"'
            )
        admin_engine.dispose()


@pytest.fixture
def migrated_database_settings(
    temporary_database: AppSettings,
) -> AppSettings:
    command.upgrade(make_alembic_config(temporary_database), "head")
    return temporary_database


@pytest.fixture
def migrated_database_engine(
    migrated_database_settings: AppSettings,
) -> Iterator[Engine]:
    engine = create_engine_from_settings(migrated_database_settings)
    try:
        yield engine
    finally:
        engine.dispose()
