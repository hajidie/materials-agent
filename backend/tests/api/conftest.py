from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine

from materialsagent.application.readiness import ReadinessService
from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.infrastructure.config import AppSettings, load_settings
from materialsagent.infrastructure.db.conversation_task import (
    ConversationRow,
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
    create_session_factory,
)
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
TEST_DATABASE_PATTERN = re.compile(r"materialsagent_test_[0-9a-f]{16}\Z")
BASE_TIME = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)


def opaque(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _alembic_config(settings: AppSettings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _admin_engine(settings: AppSettings, database: str) -> Engine:
    return create_engine(
        build_postgres_url(settings, database=database),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )


@dataclass(slots=True)
class APITestHarness:
    settings: AppSettings
    engine: Engine

    def unit_of_work_factory(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(create_session_factory(self.engine))

    def persist_actor(self, actor_id: str) -> Actor:
        actor = Actor.local_anonymous(actor_id, created_at=BASE_TIME)
        with self.unit_of_work_factory() as unit_of_work:
            unit_of_work.actors.add(actor)
            unit_of_work.commit()
        return actor

    def persist_conversation(
        self,
        actor_id: str,
        *,
        conversation_id: str | None = None,
        title: str | None = None,
        created_at: datetime = BASE_TIME,
        updated_at: datetime = BASE_TIME,
    ) -> Conversation:
        conversation = Conversation(
            conversation_id=conversation_id or opaque("conv"),
            actor_id=actor_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
        )
        with self.unit_of_work_factory() as unit_of_work:
            unit_of_work.conversations.add(conversation)
            unit_of_work.commit()
        return conversation

    def persist_message_task(
        self,
        conversation: Conversation,
        *,
        content_text: str,
        created_at: datetime,
        message_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[Message, Task]:
        task = Task.pending(
            task_id=task_id or opaque("task"),
            conversation_id=conversation.conversation_id,
            actor_id=conversation.actor_id,
            created_at=created_at,
        )
        message = Message.user(
            message_id=message_id or opaque("msg"),
            conversation_id=conversation.conversation_id,
            task_id=task.task_id,
            actor_id=conversation.actor_id,
            request_id=opaque("req"),
            content_text=content_text,
            created_at=created_at,
        )
        with self.unit_of_work_factory() as unit_of_work:
            unit_of_work.tasks.add(task)
            unit_of_work.messages.add(message)
            unit_of_work.commit()
        return message, task

    def create_client(
        self,
        actor_id: str,
        **app_overrides: Any,
    ) -> TestClient:
        from materialsagent.application.context import ActorContext
        from materialsagent.application.tasks import TaskQueryService
        from materialsagent.application.timeline import (
            TimelineQueryService,
        )
        from materialsagent.application.timeline_cursor import (
            TimelineCursorCodec,
        )
        from materialsagent.infrastructure.db.timeline_query import (
            SQLAlchemyTimelineQueryRepository,
        )
        from materialsagent.main import create_app
        from pydantic import SecretStr

        raise_server_exceptions = bool(
            app_overrides.pop("raise_server_exceptions", False)
        )
        configure_timeline = bool(
            app_overrides.pop("configure_timeline", True)
        )
        query_repository = SQLAlchemyTimelineQueryRepository(self.engine)
        options: dict[str, Any] = {
            "settings": self.settings,
            "readiness_service": ReadinessService(
                postgresql_probe=lambda: True,
                object_storage_probe=lambda: True,
            ),
            "unit_of_work_factory": self.unit_of_work_factory,
            "actor_context": ActorContext(actor_id=actor_id, user_id=None),
            "clock": lambda: BASE_TIME.replace(hour=1),
            "m7_tool_chain_enabled": False,
            "task_query_service": TaskQueryService(query_repository),
        }
        if configure_timeline:
            options["timeline_query_service"] = TimelineQueryService(
                query_repository,
                TimelineCursorCodec(
                    SecretStr(
                        "api-test-timeline-signing-key-at-least-32-bytes"
                    )
                ),
            )
        options.update(app_overrides)
        return TestClient(
            create_app(**options),
            raise_server_exceptions=raise_server_exceptions,
        )

    def counts(self) -> dict[str, int]:
        tables = {
            "conversation": ConversationRow,
            "message": MessageRow,
            "task": TaskRow,
            "task_input_revision": TaskInputRevisionRow,
        }
        with self.engine.connect() as connection:
            return {
                name: connection.scalar(select(func.count()).select_from(row))
                for name, row in tables.items()
            }

    def conversation_row(self, conversation_id: str) -> ConversationRow | None:
        session_factory = create_session_factory(self.engine)
        with session_factory() as session:
            return session.get(ConversationRow, conversation_id)

    def message_rows(self) -> list[MessageRow]:
        session_factory = create_session_factory(self.engine)
        with session_factory() as session:
            return list(session.scalars(select(MessageRow)).all())

    def task_rows(self) -> list[TaskRow]:
        session_factory = create_session_factory(self.engine)
        with session_factory() as session:
            return list(session.scalars(select(TaskRow)).all())


@pytest.fixture(scope="session")
def api_postgres_settings() -> AppSettings:
    settings = load_settings()
    build_postgres_url(settings)
    return settings


@pytest.fixture
def api_harness(
    api_postgres_settings: AppSettings,
) -> Iterator[APITestHarness]:
    database_name = f"materialsagent_test_{secrets.token_hex(8)}"
    assert TEST_DATABASE_PATTERN.fullmatch(database_name)
    admin_engine = _admin_engine(api_postgres_settings, "postgres")
    database_engine: Engine | None = None
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        database_settings = api_postgres_settings.model_copy(
            update={
                "postgres_db": database_name,
                "llm_adapter": "mock",
            }
        )
        command.upgrade(_alembic_config(database_settings), "head")
        database_engine = create_engine_from_settings(database_settings)
        yield APITestHarness(database_settings, database_engine)
    finally:
        if database_engine is not None:
            database_engine.dispose()
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
