from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import re
import secrets

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
from minio import Minio
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
import urllib3

from materialsagent.application.bootstrap import ensure_local_actor
from materialsagent.infrastructure.config import (
    AppSettings,
    load_settings,
    parse_minio_config,
    parse_zta35g_runtime_config,
)
from materialsagent.infrastructure.db.session import (
    build_postgres_url,
    create_engine_from_settings,
    create_session_factory,
)
from materialsagent.infrastructure.db.unit_of_work import (
    SQLAlchemyUnitOfWork,
)
from materialsagent.main import create_app


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
DATABASE_PATTERN = re.compile(r"materialsagent_e2e_[0-9a-f]{16}\Z")
BUCKET_PATTERN = re.compile(r"materialsagent-e2e-[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class E2EHarness:
    client: TestClient
    engine: Engine
    settings: AppSettings
    repo_root: str


def _alembic_config(settings: AppSettings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["settings"] = settings
    return config


def _admin_engine(settings: AppSettings) -> Engine:
    return create_engine(
        build_postgres_url(settings, database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )


def _assert_local_dependencies(settings: AppSettings) -> None:
    if settings.app_env != "local":
        pytest.fail("M11-A E2E requires APP_ENV=local.")
    if settings.m5_dev_routes_enabled:
        pytest.fail("M11-A E2E requires M5_DEV_ROUTES_ENABLED=false.")

    build_postgres_url(settings)
    if (
        settings.postgres_host != "127.0.0.1"
        or settings.postgres_port != 5432
    ):
        pytest.fail(
            "M11-A E2E requires POSTGRES_HOST/POSTGRES_PORT to use the "
            "fixed loopback PostgreSQL service."
        )

    minio = parse_minio_config(settings)
    if minio.endpoint != "127.0.0.1:9000" or minio.secure:
        pytest.fail(
            "M11-A E2E requires MINIO_ENDPOINT/MINIO_SECURE to use the "
            "fixed loopback MinIO service."
        )

    runtime = parse_zta35g_runtime_config(settings)
    if runtime is None or runtime.base_url != "http://127.0.0.1:8100":
        pytest.fail(
            "M11-A E2E requires ZTA35G_RUNTIME_URL to use the fixed "
            "loopback Mock Runtime."
        )
    if (
        settings.local_actor_id is None
        or not settings.local_actor_id.strip()
    ):
        pytest.fail("M11-A E2E requires non-empty LOCAL_ACTOR_ID.")
    if settings.timeline_cursor_signing_key is None:
        pytest.fail(
            "M11-A E2E requires a TIMELINE_CURSOR_SIGNING_KEY accepted "
            "by AppSettings."
        )


def _assert_mock_runtime(settings: AppSettings) -> None:
    runtime = parse_zta35g_runtime_config(settings)
    assert runtime is not None
    headers = {
        "X-ZTA35G-Runtime-Token": runtime.token.get_secret_value(),
    }
    try:
        with httpx.Client(timeout=2.0) as client:
            live = client.get(
                f"{runtime.base_url}/internal/v1/health/live",
                headers=headers,
            )
            ready = client.get(
                f"{runtime.base_url}/internal/v1/health/ready",
                headers=headers,
            )
        live.raise_for_status()
        ready.raise_for_status()
        live_data = live.json()
        ready_data = ready.json()
    except Exception:
        pytest.fail(
            "M11-A Mock Runtime is not ready. "
            "Run scripts/dev/start-mock-stack.ps1 first."
        )

    if not (
        live_data.get("runtime_contract_version") == "1.0"
        and live_data.get("status") == "LIVE"
        and ready_data.get("runtime_contract_version") == "1.0"
        and ready_data.get("status") == "READY"
        and ready_data.get("model_loaded") is True
        and ready_data.get("device") == {
            "status": "AVAILABLE",
            "kind": "cpu",
        }
        and ready_data.get("supported_tool")
        == {
            "tool_id": "zta35g_sem_virtual_lab",
            "tool_version": "0.1.0",
            "schema_version": "1.0",
        }
        and ready_data.get("model_bundle_id") == "mock-zta35g-bundle"
    ):
        pytest.fail(
            "M11-A E2E found a Runtime that is not the accepted Mock Runtime."
        )


def _create_temporary_bucket(client: Minio, bucket: str) -> bool:
    if BUCKET_PATTERN.fullmatch(bucket) is None:
        pytest.fail("M11-A E2E refused an invalid temporary bucket name.")
    if client.bucket_exists(bucket):
        pytest.fail("M11-A E2E temporary bucket already exists.")
    client.make_bucket(bucket)
    return True


def _remove_temporary_bucket(client: Minio, bucket: str) -> None:
    if BUCKET_PATTERN.fullmatch(bucket) is None:
        pytest.fail("M11-A E2E refused to clean an invalid temporary bucket.")
    if not client.bucket_exists(bucket):
        return
    for stored_object in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, stored_object.object_name)
    client.remove_bucket(bucket)


@pytest.fixture
def temporary_bucket_creator():
    return _create_temporary_bucket


@pytest.fixture(scope="session")
def e2e_harness() -> Iterator[E2EHarness]:
    base_settings = load_settings()
    _assert_local_dependencies(base_settings)
    _assert_mock_runtime(base_settings)

    suffix = secrets.token_hex(8)
    database_name = f"materialsagent_e2e_{suffix}"
    bucket_name = f"materialsagent-e2e-{suffix}"
    actor_id = f"actor_m11a_e2e_{suffix}"
    assert DATABASE_PATTERN.fullmatch(database_name)
    assert BUCKET_PATTERN.fullmatch(bucket_name)

    admin_engine = _admin_engine(base_settings)
    inspection_engine: Engine | None = None
    database_created = False
    bucket_created = False

    temporary_settings = base_settings.model_copy(
        update={
            "app_env": "test",
            "local_actor_id": actor_id,
            "postgres_db": database_name,
            "minio_bucket": bucket_name,
            "zta35g_runtime_url": "http://127.0.0.1:8100",
            "timeline_cursor_signing_key": SecretStr(
                "m11a-e2e-timeline-signing-key-at-least-32-bytes"
            ),
        }
    )
    minio_config = parse_minio_config(temporary_settings)
    cleanup_pool = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=2.0, read=5.0),
        retries=False,
    )
    cleanup_client = Minio(
        minio_config.endpoint,
        access_key=minio_config.access_key,
        secret_key=minio_config.secret_key,
        secure=minio_config.secure,
        http_client=cleanup_pool,
    )

    try:
        bucket_created = _create_temporary_bucket(
            cleanup_client,
            bucket_name,
        )

        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        database_created = True

        command.upgrade(_alembic_config(temporary_settings), "head")

        inspection_engine = create_engine_from_settings(temporary_settings)
        session_factory = create_session_factory(inspection_engine)
        ensure_local_actor(
            temporary_settings,
            lambda: SQLAlchemyUnitOfWork(session_factory),
        )

        with TestClient(
            create_app(settings=temporary_settings),
            raise_server_exceptions=True,
        ) as client:
            yield E2EHarness(
                client=client,
                engine=inspection_engine,
                settings=temporary_settings,
                repo_root=str(REPO_ROOT),
            )
    finally:
        if inspection_engine is not None:
            inspection_engine.dispose()
        try:
            if bucket_created:
                _remove_temporary_bucket(cleanup_client, bucket_name)
        finally:
            try:
                cleanup_pool.clear()
            finally:
                try:
                    if database_created:
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
                finally:
                    admin_engine.dispose()
