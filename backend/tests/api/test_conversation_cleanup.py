from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from materialsagent.application.conversation_cleanup import ConversationCleanupService
from materialsagent.domain.models.asset import LEGACY_DB_KEY, METADATA_V1
from materialsagent.domain.models.conversation_object_cleanup import (
    PENDING,
    SAFETY_BLOCKED,
    ConversationObjectCleanup,
)
from materialsagent.domain.ports.storage import StoredObjectMetadata


NOW = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)


class FakeStorage:
    def __init__(self, metadata: dict[str, str] | None) -> None:
        self.metadata = metadata
        self.deleted: list[str] = []

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        if self.metadata is None:
            return None
        return StoredObjectMetadata(
            object_key=object_key,
            size_bytes=1,
            sha256="a" * 64,
            content_type="image/png",
            metadata=dict(self.metadata),
        )

    def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)

    def put(self, *_args, **_kwargs):
        raise AssertionError("cleanup must not write objects")

    def get(self, *_args, **_kwargs):
        raise AssertionError("cleanup must not read object bytes")


def cleanup(
    *,
    suffix: str,
    identity_version: str = METADATA_V1,
    object_key: str | None = None,
    namespace: str = "minio+http://storage:9000",
) -> ConversationObjectCleanup:
    asset_id = f"asset_{suffix}"
    return ConversationObjectCleanup(
        cleanup_id=f"cleanup_{suffix}",
        actor_id="actor_local",
        conversation_id="conversation_deleted",
        asset_id=asset_id,
        operation_id=f"operation_{suffix}",
        producer_tool_run_id=f"run_{suffix}",
        object_key=object_key or f"assets/test/{asset_id}.png",
        bucket="test-bucket",
        storage_namespace=namespace,
        identity_version=identity_version,
        status=PENDING,
        attempts=0,
        created_at=NOW,
        last_attempt_at=None,
        completed_at=None,
        safety_error_code=None,
    )


def service(api_harness, storage: FakeStorage) -> ConversationCleanupService:
    return ConversationCleanupService(
        api_harness.unit_of_work_factory,
        storage,
        environment="test",
        bucket="test-bucket",
        storage_namespace="minio+http://storage:9000",
        process_cutoff=NOW,
        clock=lambda: NOW,
    )


def persist(api_harness, record: ConversationObjectCleanup) -> None:
    with api_harness.unit_of_work_factory() as unit_of_work:
        unit_of_work.conversation_object_cleanups.add(record)
        unit_of_work.commit()


@pytest.mark.parametrize("identity_version", [METADATA_V1, LEGACY_DB_KEY])
def test_cleanup_deletes_only_exact_metadata_identity(
    api_harness,
    identity_version: str,
) -> None:
    record = cleanup(suffix=identity_version.lower(), identity_version=identity_version)
    storage = FakeStorage(
        {
            "asset-id": record.asset_id,
            "operation-id": record.operation_id,
            "producer-tool-run-id": record.producer_tool_run_id,
        }
    )
    persist(api_harness, record)

    summary = service(api_harness, storage).drain(limit=100)

    assert summary.completed == 1
    assert storage.deleted == [record.object_key]


def test_legacy_cleanup_accepts_completely_absent_identity_metadata(
    api_harness,
) -> None:
    record = cleanup(suffix="legacy", identity_version=LEGACY_DB_KEY)
    storage = FakeStorage({})
    persist(api_harness, record)

    summary = service(api_harness, storage).drain()

    assert summary.completed == 1
    assert storage.deleted == [record.object_key]


def test_cleanup_uses_the_snapshotted_valid_environment_segment(
    api_harness,
) -> None:
    record = cleanup(
        suffix="historical",
        object_key="assets/legacy-env/asset_historical.png",
    )
    storage = FakeStorage(
        {
            "asset-id": record.asset_id,
            "operation-id": record.operation_id,
            "producer-tool-run-id": record.producer_tool_run_id,
        }
    )
    persist(api_harness, record)

    summary = service(api_harness, storage).drain()

    assert summary.completed == 1
    assert storage.deleted == [record.object_key]


@pytest.mark.parametrize(
    "record,metadata",
    [
        (cleanup(suffix="partial", identity_version=LEGACY_DB_KEY), {"asset-id": "asset_partial"}),
        (cleanup(suffix="mismatch"), {"asset-id": "other", "operation-id": "operation_mismatch", "producer-tool-run-id": "run_mismatch"}),
        (cleanup(suffix="badkey", object_key="assets/test/another.png"), {}),
        (cleanup(suffix="badenv", object_key="assets/TEST/asset_badenv.png"), {}),
    ],
)
def test_cleanup_safety_blocks_partial_mismatched_or_wrong_key(
    api_harness,
    record: ConversationObjectCleanup,
    metadata: dict[str, str],
) -> None:
    storage = FakeStorage(metadata)
    persist(api_harness, record)
    cleanup_service = service(api_harness, storage)

    first = cleanup_service.drain()
    second = cleanup_service.drain()

    assert first.safety_blocked == 1
    assert second.attempted == 0
    assert storage.deleted == []
    with api_harness.unit_of_work_factory() as unit_of_work:
        persisted = unit_of_work.conversation_object_cleanups.get(record.cleanup_id)
    assert persisted is not None
    assert persisted.status == SAFETY_BLOCKED


def test_cleanup_keeps_wrong_namespace_pending_without_storage_access(
    api_harness,
) -> None:
    record = cleanup(
        suffix="namespace",
        namespace="minio+http://old-storage:9000",
    )
    storage = FakeStorage(None)
    persist(api_harness, record)

    summary = service(api_harness, storage).drain()

    assert summary.pending == 1
    assert storage.deleted == []


def test_cleanup_hard_caps_each_drain_at_one_hundred(api_harness) -> None:
    storage = FakeStorage(None)
    for index in range(101):
        persist(api_harness, cleanup(suffix=f"bounded_{index:03d}"))

    summary = service(api_harness, storage).drain(limit=1000)

    assert summary.attempted == 100
    assert summary.completed == 100
