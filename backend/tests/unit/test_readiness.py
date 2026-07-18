from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    (
        "postgresql_available",
        "object_storage_available",
        "expected_status",
        "expected_http_status",
        "expected_component_statuses",
    ),
    [
        (True, True, "READY", 200, ["AVAILABLE", "AVAILABLE"]),
        (True, False, "DEGRADED", 200, ["AVAILABLE", "UNAVAILABLE"]),
        (False, True, "NOT_READY", 503, ["UNAVAILABLE", "AVAILABLE"]),
        (False, False, "NOT_READY", 503, ["UNAVAILABLE", "UNAVAILABLE"]),
    ],
)
def test_readiness_aggregates_exact_component_order_and_status(
    postgresql_available: bool,
    object_storage_available: bool,
    expected_status: str,
    expected_http_status: int,
    expected_component_statuses: list[str],
) -> None:
    from materialsagent.application.readiness import ReadinessService

    service = ReadinessService(
        postgresql_probe=lambda: postgresql_available,
        object_storage_probe=lambda: object_storage_available,
    )

    snapshot = service.check()

    assert snapshot.status == expected_status
    assert snapshot.http_status == expected_http_status
    assert [component.name for component in snapshot.components] == [
        "postgresql",
        "object_storage",
    ]
    assert [component.status for component in snapshot.components] == (
        expected_component_statuses
    )


def test_unexpected_probe_exceptions_only_mark_component_unavailable() -> None:
    from materialsagent.application.readiness import ReadinessService

    def failed_probe() -> bool:
        raise RuntimeError("secret endpoint and stack detail")

    snapshot = ReadinessService(
        postgresql_probe=failed_probe,
        object_storage_probe=lambda: True,
    ).check()

    assert snapshot.status == "NOT_READY"
    assert snapshot.http_status == 503
    assert [component.status for component in snapshot.components] == [
        "UNAVAILABLE",
        "AVAILABLE",
    ]


def test_postgresql_probe_uses_short_timeout_select_one_and_disposes(
    monkeypatch,
) -> None:
    from materialsagent.application import readiness
    from materialsagent.infrastructure.config import load_settings

    events: list[object] = []

    class FakeEngine:
        def dispose(self) -> None:
            events.append("disposed")

    fake_engine = FakeEngine()

    def fake_create_engine(settings, *, connect_timeout_seconds, poolclass):
        events.append((connect_timeout_seconds, poolclass.__name__))
        return fake_engine

    def fake_check_database_connection(engine) -> None:
        assert engine is fake_engine
        events.append("select-1")

    monkeypatch.setattr(readiness, "create_engine_from_settings", fake_create_engine)
    monkeypatch.setattr(
        readiness,
        "check_database_connection",
        fake_check_database_connection,
    )

    probe = readiness.build_postgresql_probe(load_settings({}))

    assert probe() is True
    assert events == [(1, "NullPool"), "select-1", "disposed"]


def test_object_storage_probe_checks_bucket_without_creating_objects(
    monkeypatch,
) -> None:
    from materialsagent.application import readiness
    from materialsagent.infrastructure.config import load_settings

    events: list[str] = []

    class FakeStorage:
        def bucket_exists(self) -> bool:
            events.append("bucket-exists")
            return True

        def create_bucket(self) -> None:
            raise AssertionError("ready must not create a bucket")

        def put(self, *args, **kwargs):
            raise AssertionError("ready must not write an object")

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(
        readiness,
        "create_minio_storage",
        lambda settings: FakeStorage(),
    )

    probe = readiness.build_object_storage_probe(load_settings({}))

    assert probe() is True
    assert events == ["bucket-exists", "closed"]


@pytest.mark.parametrize(
    "module_name",
    [
        "materialsagent.infrastructure.config",
        "materialsagent.domain.ports.storage",
        "materialsagent.infrastructure.storage.minio",
        "materialsagent.application.bootstrap",
        "materialsagent.application.readiness",
        "materialsagent.main",
    ],
)
def test_module_imports_have_no_network_or_persistence_side_effects(
    module_name: str,
) -> None:
    backend_src = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import socket\n"
        "def blocked(*args, **kwargs):\n"
        "    raise AssertionError('network access during import')\n"
        "socket.socket.connect = blocked\n"
        f"import {module_name}\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(backend_src)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr
