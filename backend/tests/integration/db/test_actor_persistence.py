from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from materialsagent.application.bootstrap import ensure_local_actor
from materialsagent.domain.models.actor import Actor, LOCAL_ANONYMOUS
from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceError,
)
from materialsagent.infrastructure.config import AppSettings
from materialsagent.infrastructure.db.actor import ActorRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork


REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_SRC = REPO_ROOT / "backend" / "src"


def _uow_factory(engine: Engine) -> Any:
    session_factory = create_session_factory(engine)
    return lambda: SQLAlchemyUnitOfWork(session_factory)


def _actor_count(engine: Engine, actor_id: str | None = None) -> int:
    statement = select(func.count()).select_from(ActorRow)
    if actor_id is not None:
        statement = statement.where(ActorRow.actor_id == actor_id)
    with engine.connect() as connection:
        return int(connection.scalar(statement) or 0)


def test_actor_round_trip_uses_utc_and_nullable_link_fields(
    migrated_database_engine: Engine,
) -> None:
    actor_id = f"local_actor_{uuid4().hex}"
    actor = Actor(
        actor_id=actor_id,
        user_id=None,
        actor_origin=LOCAL_ANONYMOUS,
        created_at=datetime.now(timezone.utc),
        linked_at=None,
    )
    factory = _uow_factory(migrated_database_engine)

    with factory() as unit_of_work:
        unit_of_work.actors.add(actor)
        unit_of_work.commit()

    with factory() as unit_of_work:
        loaded = unit_of_work.actors.get(actor_id)

    assert loaded is not None
    assert loaded.actor_id == actor_id
    assert loaded.user_id is None
    assert loaded.linked_at is None
    assert loaded.actor_origin == LOCAL_ANONYMOUS
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timezone.utc.utcoffset(
        loaded.created_at
    )


def test_bootstrap_is_idempotent_and_preserves_created_at(
    migrated_database_settings: AppSettings,
    migrated_database_engine: Engine,
) -> None:
    actor_id = f"stable_local_actor_{uuid4().hex}"
    settings = migrated_database_settings.model_copy(
        update={"local_actor_id": actor_id}
    )
    factory = _uow_factory(migrated_database_engine)

    first = ensure_local_actor(settings, factory)
    second = ensure_local_actor(settings, factory)
    third = ensure_local_actor(settings, factory)

    assert first.actor_id == second.actor_id == third.actor_id == actor_id
    assert first.created_at == second.created_at == third.created_at
    assert _actor_count(migrated_database_engine, actor_id) == 1


class _BarrierRepository:
    def __init__(self, repository: Any, barrier: Barrier) -> None:
        self._repository = repository
        self._barrier = barrier

    def get(self, actor_id: str) -> Actor | None:
        actor = self._repository.get(actor_id)
        if actor is None:
            self._barrier.wait(timeout=10)
        return actor

    def add(self, actor: Actor) -> None:
        self._repository.add(actor)


class _BarrierUnitOfWork:
    def __init__(self, inner: SQLAlchemyUnitOfWork, barrier: Barrier) -> None:
        self._inner = inner
        self._barrier = barrier

    def __enter__(self) -> _BarrierUnitOfWork:
        self._inner.__enter__()
        self.actors = _BarrierRepository(self._inner.actors, self._barrier)
        return self

    def __exit__(self, *args: object) -> None:
        self._inner.__exit__(*args)

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()


def test_bootstrap_resolves_a_real_primary_key_race(
    migrated_database_settings: AppSettings,
    migrated_database_engine: Engine,
) -> None:
    actor_id = f"racing_local_actor_{uuid4().hex}"
    settings = migrated_database_settings.model_copy(
        update={"local_actor_id": actor_id}
    )
    session_factory = create_session_factory(migrated_database_engine)
    barrier = Barrier(2)

    def factory() -> _BarrierUnitOfWork:
        return _BarrierUnitOfWork(
            SQLAlchemyUnitOfWork(session_factory),
            barrier,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ensure_local_actor, settings, factory)
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in futures]

    assert [actor.actor_id for actor in results] == [actor_id, actor_id]
    assert results[0].created_at == results[1].created_at
    assert _actor_count(migrated_database_engine, actor_id) == 1


@pytest.mark.parametrize("actor_id", ["", "   "])
def test_actor_rejects_empty_actor_id(actor_id: str) -> None:
    with pytest.raises(ValueError, match="actor_id"):
        Actor(
            actor_id=actor_id,
            user_id=None,
            actor_origin=LOCAL_ANONYMOUS,
            created_at=datetime.now(timezone.utc),
            linked_at=None,
        )


@pytest.mark.parametrize("actor_origin", ["", "ADMIN", "LOCAL_USER"])
def test_actor_rejects_empty_or_unknown_origin(actor_origin: str) -> None:
    with pytest.raises(ValueError, match="actor_origin"):
        Actor(
            actor_id="valid-actor",
            user_id=None,
            actor_origin=actor_origin,
            created_at=datetime.now(timezone.utc),
            linked_at=None,
        )


def test_bootstrap_does_not_accept_client_user_id(
    migrated_database_settings: AppSettings,
    migrated_database_engine: Engine,
) -> None:
    factory = _uow_factory(migrated_database_engine)

    with pytest.raises(TypeError):
        ensure_local_actor(
            migrated_database_settings,
            factory,
            user_id="client-selected-user",
        )


def test_unit_of_work_rolls_back_closes_and_allows_later_transactions(
    migrated_database_engine: Engine,
) -> None:
    rolled_back_actor_id = f"rolled_back_{uuid4().hex}"
    surviving_actor_id = f"surviving_{uuid4().hex}"
    factory = _uow_factory(migrated_database_engine)
    failed_unit_of_work: SQLAlchemyUnitOfWork | None = None

    with pytest.raises(RuntimeError, match="forced repository failure"):
        with factory() as unit_of_work:
            failed_unit_of_work = unit_of_work
            unit_of_work.actors.add(
                Actor.local_anonymous(rolled_back_actor_id)
            )
            raise RuntimeError("forced repository failure")

    assert failed_unit_of_work is not None
    assert failed_unit_of_work.session is None
    assert _actor_count(migrated_database_engine, rolled_back_actor_id) == 0

    with factory() as unit_of_work:
        unit_of_work.actors.add(Actor.local_anonymous(surviving_actor_id))
        unit_of_work.commit()

    assert _actor_count(migrated_database_engine, surviving_actor_id) == 1


class _CommitAndFirstRollbackFailingSession:
    def __init__(
        self,
        *,
        commit_error: SQLAlchemyError,
        rollback_error: SQLAlchemyError,
    ) -> None:
        self._commit_error = commit_error
        self._rollback_error = rollback_error
        self.rollback_calls = 0
        self.closed = False

    def commit(self) -> None:
        raise self._commit_error

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_calls == 1:
            raise self._rollback_error

    def close(self) -> None:
        self.closed = True


def test_commit_integrity_error_with_dbapi_rollback_failure_is_safe() -> None:
    fake_sql = "INSERT fake SQL with secret parameter"
    fake_password = "fake-password-must-not-leak"
    fake_driver_text = "raw driver rollback failure"
    session = _CommitAndFirstRollbackFailingSession(
        commit_error=IntegrityError(
            fake_sql,
            {"password": fake_password},
            Exception("raw integrity driver failure"),
        ),
        rollback_error=OperationalError(
            "ROLLBACK fake SQL",
            {"password": fake_password},
            Exception(fake_driver_text),
        ),
    )
    unit_of_work = SQLAlchemyUnitOfWork(lambda: session)

    with pytest.raises(DatabaseUnavailableError) as exc_info:
        with unit_of_work:
            unit_of_work.commit()

    assert type(exc_info.value) is DatabaseUnavailableError
    assert str(exc_info.value) == "Database unavailable."
    assert fake_sql not in str(exc_info.value)
    assert fake_password not in str(exc_info.value)
    assert fake_driver_text not in str(exc_info.value)
    assert session.rollback_calls == 2
    assert session.closed is True
    assert unit_of_work.session is None


def test_commit_dbapi_error_with_sqlalchemy_rollback_failure_is_safe() -> None:
    fake_internal_text = "raw internal rollback details"
    session = _CommitAndFirstRollbackFailingSession(
        commit_error=OperationalError(
            "UPDATE fake SQL",
            {"secret": "fake-secret"},
            Exception("raw database driver failure"),
        ),
        rollback_error=SQLAlchemyError(fake_internal_text),
    )
    unit_of_work = SQLAlchemyUnitOfWork(lambda: session)

    with pytest.raises(PersistenceError) as exc_info:
        with unit_of_work:
            unit_of_work.commit()

    assert type(exc_info.value) is PersistenceError
    assert str(exc_info.value) == "Persistence operation failed."
    assert fake_internal_text not in str(exc_info.value)
    assert "fake-secret" not in str(exc_info.value)
    assert "raw database driver failure" not in str(exc_info.value)
    assert session.rollback_calls == 2
    assert session.closed is True
    assert unit_of_work.session is None


def _subprocess_environment(**overrides: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(overrides)
    environment["PYTHONPATH"] = str(BACKEND_SRC)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def test_unavailable_postgresql_error_is_classified_and_sanitized() -> None:
    password = "subprocess-password-must-not-leak"
    script = """
from materialsagent.infrastructure.config import load_settings
from materialsagent.domain.ports.unit_of_work import DatabaseUnavailableError
from materialsagent.infrastructure.db.session import (
    check_database_connection,
    create_engine_from_settings,
)

settings = load_settings()
engine = create_engine_from_settings(settings, connect_timeout_seconds=1)
try:
    check_database_connection(engine)
except DatabaseUnavailableError as error:
    print(f"{type(error).__name__}:{error}")
else:
    raise SystemExit(9)
finally:
    engine.dispose()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=_subprocess_environment(
            POSTGRES_HOST="127.0.0.1",
            POSTGRES_PORT="1",
            POSTGRES_DB="materialsagent",
            POSTGRES_USER="materialsagent",
            POSTGRES_PASSWORD=password,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert output.strip() == "DatabaseUnavailableError:Database unavailable."
    assert password not in output
    assert "postgresql+psycopg://" not in output
    assert ".env" not in output
    assert str(REPO_ROOT) not in output
    assert "SELECT" not in output
    assert "Traceback" not in output


def test_imports_do_not_connect_migrate_or_bootstrap() -> None:
    password = "unused-import-password"
    script = """
import materialsagent.infrastructure.config
import materialsagent.infrastructure.db.session
import materialsagent.application.bootstrap
print("IMPORT_OK")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=_subprocess_environment(
            POSTGRES_HOST="127.0.0.1",
            POSTGRES_PORT="1",
            POSTGRES_DB="must_not_be_touched",
            POSTGRES_USER="materialsagent",
            POSTGRES_PASSWORD=password,
            LOCAL_ACTOR_ID="must-not-be-created",
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert output.strip() == "IMPORT_OK"
    assert password not in output
    assert "Traceback" not in output
