from __future__ import annotations

from typing import Any

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from materialsagent.domain.ports.unit_of_work import DatabaseUnavailableError
from materialsagent.infrastructure.config import AppSettings, ConfigurationError


def _required_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value


def build_postgres_url(
    settings: AppSettings,
    *,
    database: str | None = None,
) -> URL:
    host = _required_text(settings.postgres_host)
    database_name = _required_text(database or settings.postgres_db)
    username = _required_text(settings.postgres_user)
    password = settings.postgres_password

    if (
        host is None
        or database_name is None
        or username is None
        or password is None
        or not password
    ):
        raise ConfigurationError(
            "PostgreSQL configuration is incomplete."
        )

    return URL.create(
        drivername="postgresql+psycopg",
        username=username,
        password=password,
        host=host,
        port=settings.postgres_port,
        database=database_name,
    )


def create_engine_from_settings(
    settings: AppSettings,
    *,
    connect_timeout_seconds: int | None = None,
    poolclass: type[Any] | None = None,
) -> Engine:
    engine_options: dict[str, Any] = {
        "hide_parameters": True,
        "pool_pre_ping": True,
    }
    if connect_timeout_seconds is not None:
        engine_options["connect_args"] = {
            "connect_timeout": connect_timeout_seconds,
        }
    if poolclass is not None:
        engine_options["poolclass"] = poolclass

    return create_engine(build_postgres_url(settings), **engine_options)


def create_session_factory(
    engine: Engine,
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def check_database_connection(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except SQLAlchemyError:
        raise DatabaseUnavailableError("Database unavailable.") from None
