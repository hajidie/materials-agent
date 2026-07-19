from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from materialsagent.infrastructure.config import AppSettings, load_settings
from materialsagent.infrastructure.db.actor import ActorRow
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import (
    ConversationRow,
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
)


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_ = (ActorRow, ConversationRow, MessageRow, TaskInputRevisionRow, TaskRow)
target_metadata = Base.metadata


def _settings() -> AppSettings:
    configured = config.attributes.get("settings")
    if configured is not None:
        if not isinstance(configured, AppSettings):
            raise TypeError("Alembic settings attribute must be AppSettings.")
        return configured
    return load_settings()


def run_migrations_offline() -> None:
    context.configure(
        url=build_postgres_url(_settings()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine_from_settings(
        _settings(),
        poolclass=pool.NullPool,
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
