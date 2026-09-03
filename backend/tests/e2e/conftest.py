from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
from minio import Minio
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
import urllib3
import uvicorn


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
MOCK_RUNTIME_SRC = REPO_ROOT / "mock-runtime" / "src"
sys.path.insert(0, str(MOCK_RUNTIME_SRC))

from materialsagent.application.bootstrap import ensure_local_actor
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
)
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
from materialsagent_mock_runtime.main import (
    MODEL_BUNDLE_ID,
    RUNTIME_CONTRACT_VERSION,
    SCHEMA_VERSION,
    TOKEN_HEADER,
    TOOL_ID,
    TOOL_VERSION,
    ExecuteRequest,
    MockRuntimeSettings,
    _deterministic_npy,
    _validate_request,
    create_app as create_mock_runtime_app,
)


ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
DATABASE_PATTERN = re.compile(r"materialsagent_e2e_[0-9a-f]{16}\Z")
BUCKET_PATTERN = re.compile(r"materialsagent-e2e-[0-9a-f]{16}\Z")


@dataclass(frozen=True, slots=True)
class E2EHarness:
    client: TestClient
    engine: Engine
    settings: AppSettings
    repo_root: str


@dataclass(slots=True)
class RuntimeHTTPHarness:
    mode: str
    base_url: str
    token: str
    app: FastAPI | None
    entered: threading.Event
    release: threading.Event

    @property
    def execution_count(self) -> int:
        if self.app is None:
            return 0
        return int(self.app.state.runtime_state.execution_count)


@dataclass(slots=True)
class E2EAppFactory:
    engine: Engine
    settings: AppSettings
    repo_root: str
    minio_client: Minio
    bucket_name: str

    @contextmanager
    def client(
        self,
        *,
        runtime: RuntimeHTTPHarness | None = None,
        runtime_url: str | None = None,
        runtime_token: str | None = None,
        runtime_timeout_seconds: float | None = None,
        chat_responder: Callable[[Any], Mapping[str, object]] | None = None,
        explanation: Any | None = None,
        settings_overrides: Mapping[str, object] | None = None,
        raise_server_exceptions: bool = False,
    ) -> Iterator[TestClient]:
        updates = dict(settings_overrides or {})
        if runtime is not None:
            updates.update(
                {
                    "zta35g_runtime_url": runtime.base_url,
                    "zta35g_runtime_token": SecretStr(runtime.token),
                }
            )
        elif runtime_url is not None or runtime_token is not None:
            updates.update(
                {
                    "zta35g_runtime_url": runtime_url,
                    "zta35g_runtime_token": (
                        None
                        if runtime_token is None
                        else SecretStr(runtime_token)
                    ),
                }
            )
        if runtime_timeout_seconds is not None:
            updates["zta35g_runtime_timeout_seconds"] = (
                runtime_timeout_seconds
            )
        settings = self.settings.model_copy(update=updates)
        options: dict[str, object] = {"settings": settings}
        if chat_responder is not None:
            options["chat_orchestration_port"] = (
                MockChatOrchestrationAdapter(chat_responder)
            )
        if explanation is not None:
            options["explanation_port"] = explanation
        with TestClient(
            create_app(**options),
            raise_server_exceptions=raise_server_exceptions,
        ) as client:
            yield client

    def minio_object_count(self) -> int:
        return sum(
            1
            for _item in self.minio_client.list_objects(
                self.bucket_name,
                recursive=True,
            )
        )


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


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _partial_success_runtime_app(token: str) -> FastAPI:
    image_npy = _deterministic_npy()
    image_base64 = base64.b64encode(image_npy).decode("ascii")
    image_sha256 = sha256(image_npy).hexdigest()
    state = SimpleNamespace(execution_count=0)
    app = FastAPI()
    app.state.runtime_state = state

    def authorize(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER)
        if supplied is None or not secrets.compare_digest(supplied, token):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/internal/v1/health/live")
    def live(request: Request) -> dict[str, object]:
        authorize(request)
        return {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "status": "LIVE",
            "process_started_at": _utc_text(),
            "checked_at": _utc_text(),
        }

    @app.get("/internal/v1/health/ready")
    def ready(request: Request) -> dict[str, object]:
        authorize(request)
        return {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "status": "READY",
            "model_files": {"status": "AVAILABLE"},
            "model_loaded": True,
            "device": {"status": "AVAILABLE", "kind": "cpu"},
            "can_accept_execution": True,
            "busy": False,
            "supported_tool": {
                "tool_id": TOOL_ID,
                "tool_version": TOOL_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
            "model_bundle_id": MODEL_BUNDLE_ID,
            "checked_at": _utc_text(),
            "error": None,
        }

    @app.post("/internal/v1/execute")
    def execute(
        payload: ExecuteRequest,
        request: Request,
    ) -> dict[str, object]:
        authorize(request)
        _validate_request(payload)
        if payload.requested_outputs != [
            "sem_image",
            "mechanical_properties",
        ]:
            raise AssertionError(
                "The M11-B partial-success protocol app only accepts the "
                "two-output acceptance request."
            )
        state.execution_count += 1
        timestamp = _utc_text()
        return {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "request_id": payload.request_id,
            "task_id": payload.task_id,
            "tool_run_id": payload.tool_run_id,
            "tool_id": TOOL_ID,
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "status": "PARTIALLY_SUCCEEDED",
            "requested_outputs": list(payload.requested_outputs),
            "completed_outputs": ["sem_image"],
            "failed_outputs": ["mechanical_properties"],
            "data": {},
            "images": [
                {
                    "image_role": "generated_sem",
                    "requested_output": True,
                    "dtype": "float32",
                    "numpy_dtype": "<f4",
                    "shape": [512, 512],
                    "channel_layout": "GRAYSCALE_2D",
                    "value_range": [-1.0, 1.0],
                    "encoding": "base64+npy",
                    "byte_order": "little",
                    "array_order": "C",
                    "sha256": image_sha256,
                    "data_base64": image_base64,
                }
            ],
            "warnings": [],
            "diagnostics": [
                {
                    "step": "sem_generation",
                    "status": "SUCCEEDED",
                    "started_at": timestamp,
                    "completed_at": timestamp,
                    "duration_ms": 0,
                    "error_code": None,
                    "safe_error_message": None,
                },
                {
                    "step": "mechanical_property_prediction",
                    "status": "FAILED",
                    "started_at": timestamp,
                    "completed_at": timestamp,
                    "duration_ms": 0,
                    "error_code": (
                        "MECHANICAL_PROPERTY_PREDICTION_FAILED"
                    ),
                    "safe_error_message": (
                        "Mechanical property prediction failed."
                    ),
                },
            ],
            "actual_runtime_parameters": (
                payload.runtime_parameters.model_dump()
            ),
            "model_bundle_id": MODEL_BUNDLE_ID,
            "error": {
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "Mechanical property prediction failed.",
                "retryable": False,
                "failed_step": "mechanical_property_prediction",
                "details": {},
            },
        }

    return app


def _assert_port_released(port: int) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                break
        finally:
            probe.close()
        sleep(0.01)
    else:
        pytest.fail("M11-B test Runtime port was not released.")

    rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rebound.bind(("127.0.0.1", port))
    except OSError:
        pytest.fail("M11-B test Runtime socket was not released.")
    finally:
        rebound.close()


@contextmanager
def _runtime_http_server(mode: str) -> Iterator[RuntimeHTTPHarness]:
    supported = {
        "success",
        "fail_once_then_success",
        "timeout",
        "busy",
        "partial_success",
        "unavailable",
    }
    if mode not in supported:
        raise ValueError("Unsupported M11-B Runtime test mode.")

    token = secrets.token_urlsafe(48)
    entered = threading.Event()
    release = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"

    if mode == "unavailable":
        listener.close()
        try:
            yield RuntimeHTTPHarness(
                mode=mode,
                base_url=base_url,
                token=token,
                app=None,
                entered=entered,
                release=release,
            )
        finally:
            _assert_port_released(port)
        return

    hook_calls = 0

    def execution_hook() -> None:
        nonlocal hook_calls
        hook_calls += 1
        if mode == "fail_once_then_success" and hook_calls == 1:
            raise RuntimeError("controlled test Runtime failure")
        if mode in {"timeout", "busy"}:
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("controlled test Runtime release timed out")

    app = (
        _partial_success_runtime_app(token)
        if mode == "partial_success"
        else create_mock_runtime_app(
            MockRuntimeSettings(token=token, port=port),
            execution_hook=execution_hook,
        )
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="m11b-test-runtime",
        daemon=False,
    )
    thread.start()
    deadline = monotonic() + 5
    while not server.started and thread.is_alive() and monotonic() < deadline:
        sleep(0.01)
    if not server.started or not thread.is_alive():
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        pytest.fail("M11-B test Runtime failed to start.")

    harness = RuntimeHTTPHarness(
        mode=mode,
        base_url=base_url,
        token=token,
        app=app,
        entered=entered,
        release=release,
    )
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(
                f"{base_url}/internal/v1/health/ready",
                headers={TOKEN_HEADER: token},
            )
        if response.status_code != 200:
            pytest.fail("M11-B test Runtime readiness failed.")
        yield harness
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            pytest.fail("M11-B test Runtime thread did not stop.")
        _assert_port_released(port)


@pytest.fixture
def runtime_http_factory():
    return _runtime_http_server


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
def e2e_app_factory() -> Iterator[E2EAppFactory]:
    base_settings = load_settings()
    _assert_local_dependencies(base_settings)

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
            "llm_adapter": "mock",
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

        yield E2EAppFactory(
            engine=inspection_engine,
            settings=temporary_settings,
            repo_root=str(REPO_ROOT),
            minio_client=cleanup_client,
            bucket_name=bucket_name,
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


@pytest.fixture(scope="session")
def e2e_harness(
    e2e_app_factory: E2EAppFactory,
) -> Iterator[E2EHarness]:
    _assert_mock_runtime(e2e_app_factory.settings)
    with e2e_app_factory.client(
        raise_server_exceptions=True,
    ) as client:
        yield E2EHarness(
            client=client,
            engine=e2e_app_factory.engine,
            settings=e2e_app_factory.settings,
            repo_root=e2e_app_factory.repo_root,
        )
