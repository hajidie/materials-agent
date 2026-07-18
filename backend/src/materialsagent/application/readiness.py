from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.pool import NullPool

from materialsagent.infrastructure.config import AppSettings
from materialsagent.infrastructure.db.session import (
    check_database_connection,
    create_engine_from_settings,
)
from materialsagent.infrastructure.storage.minio import create_minio_storage


ComponentName = Literal["postgresql", "object_storage"]
ComponentStatus = Literal["AVAILABLE", "UNAVAILABLE"]
ReadinessStatus = Literal["READY", "DEGRADED", "NOT_READY"]
Probe = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ReadinessComponent:
    name: ComponentName
    status: ComponentStatus


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    status: ReadinessStatus
    components: tuple[ReadinessComponent, ReadinessComponent]
    http_status: int


class ReadinessService:
    def __init__(
        self,
        *,
        postgresql_probe: Probe,
        object_storage_probe: Probe,
    ) -> None:
        self._postgresql_probe = postgresql_probe
        self._object_storage_probe = object_storage_probe

    def check(self) -> ReadinessSnapshot:
        postgresql_available = _probe_available(self._postgresql_probe)
        object_storage_available = _probe_available(
            self._object_storage_probe
        )
        components = (
            ReadinessComponent(
                name="postgresql",
                status=(
                    "AVAILABLE" if postgresql_available else "UNAVAILABLE"
                ),
            ),
            ReadinessComponent(
                name="object_storage",
                status=(
                    "AVAILABLE" if object_storage_available else "UNAVAILABLE"
                ),
            ),
        )

        if not postgresql_available:
            return ReadinessSnapshot(
                status="NOT_READY",
                components=components,
                http_status=503,
            )
        if not object_storage_available:
            return ReadinessSnapshot(
                status="DEGRADED",
                components=components,
                http_status=200,
            )
        return ReadinessSnapshot(
            status="READY",
            components=components,
            http_status=200,
        )


def build_readiness_service(settings: AppSettings) -> ReadinessService:
    return ReadinessService(
        postgresql_probe=build_postgresql_probe(settings),
        object_storage_probe=build_object_storage_probe(settings),
    )


def build_postgresql_probe(settings: AppSettings) -> Probe:
    def probe() -> bool:
        engine = create_engine_from_settings(
            settings,
            connect_timeout_seconds=1,
            poolclass=NullPool,
        )
        try:
            check_database_connection(engine)
            return True
        finally:
            engine.dispose()

    return probe


def build_object_storage_probe(settings: AppSettings) -> Probe:
    def probe() -> bool:
        storage = create_minio_storage(settings)
        try:
            return storage.bucket_exists()
        finally:
            storage.close()

    return probe


def _probe_available(probe: Probe) -> bool:
    try:
        return probe() is True
    except Exception:
        return False
