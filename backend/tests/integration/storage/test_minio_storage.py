from __future__ import annotations

from hashlib import sha256

import pytest

from materialsagent.application.bootstrap import ensure_object_storage_bucket
from materialsagent.domain.ports.storage import (
    StorageConflictError,
    StorageUnavailableError,
)
from materialsagent.infrastructure.config import AppSettings
from materialsagent.infrastructure.storage.minio import (
    MinioStorageService,
    create_minio_storage,
)


def test_configured_bucket_bootstrap_is_repeatable(
    minio_storage: MinioStorageService,
) -> None:
    ensure_object_storage_bucket(minio_storage)
    ensure_object_storage_bucket(minio_storage)

    assert minio_storage.bucket_exists() is True


def test_put_head_get_delete_and_repeated_delete(
    minio_storage: MinioStorageService,
    unique_object_key: str,
) -> None:
    payload = b"m2-minio-integration-payload"
    expected_digest = sha256(payload).hexdigest()

    stored = minio_storage.put(
        unique_object_key,
        payload,
        "application/octet-stream",
        {"asset-id": "asset_integration", "tool-version": "1.0.0"},
    )
    headed = minio_storage.head(unique_object_key)

    assert stored.object_key == unique_object_key
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == expected_digest
    assert stored.content_type == "application/octet-stream"
    assert stored.metadata == {
        "asset-id": "asset_integration",
        "tool-version": "1.0.0",
    }
    assert headed == stored
    assert minio_storage.get(unique_object_key, max_bytes=len(payload)) == payload

    minio_storage.delete(unique_object_key)
    minio_storage.delete(unique_object_key)
    assert minio_storage.head(unique_object_key) is None


def test_same_content_replay_returns_existing_metadata(
    minio_storage: MinioStorageService,
    unique_object_key: str,
) -> None:
    payload = b"safe-replay"

    first = minio_storage.put(
        unique_object_key,
        payload,
        "application/octet-stream",
    )
    replay = minio_storage.put(
        unique_object_key,
        payload,
        "application/octet-stream",
    )

    assert replay == first
    assert minio_storage.get(unique_object_key, max_bytes=len(payload)) == payload


def test_different_content_conflicts_and_preserves_original(
    minio_storage: MinioStorageService,
    unique_object_key: str,
) -> None:
    original = b"original-object-content"
    minio_storage.put(
        unique_object_key,
        original,
        "application/octet-stream",
    )

    with pytest.raises(
        StorageConflictError,
        match=r"^Object storage conflict\.$",
    ):
        minio_storage.put(
            unique_object_key,
            b"different-object-content",
            "application/octet-stream",
        )

    assert minio_storage.get(unique_object_key, max_bytes=len(original)) == original


def test_missing_head_is_none(
    minio_storage: MinioStorageService,
    unique_object_key: str,
) -> None:
    assert minio_storage.head(unique_object_key) is None


def test_unreachable_port_maps_to_safe_unavailable_error(
    minio_settings: AppSettings,
) -> None:
    storage = create_minio_storage(
        minio_settings.model_copy(
            update={
                "minio_endpoint": "http://127.0.0.1:1",
                "minio_secure": False,
            }
        )
    )
    try:
        with pytest.raises(StorageUnavailableError) as exc_info:
            storage.head("m2-test/unreachable.bin")
    finally:
        storage.close()

    message = str(exc_info.value)
    assert message == "Object storage unavailable."
    assert "127.0.0.1" not in message
    assert "m2-test" not in message
    assert minio_settings.minio_bucket not in message
    assert minio_settings.minio_secret_key not in message


def test_invalid_authentication_maps_to_safe_unavailable_error(
    minio_settings: AppSettings,
) -> None:
    storage = create_minio_storage(
        minio_settings.model_copy(
            update={
                "minio_access_key": "invalid-integration-access-key",
                "minio_secret_key": "invalid-integration-secret-key",
            }
        )
    )
    try:
        with pytest.raises(StorageUnavailableError) as exc_info:
            storage.bucket_exists()
    finally:
        storage.close()

    message = str(exc_info.value)
    assert message == "Object storage unavailable."
    assert "invalid-integration-access-key" not in message
    assert "invalid-integration-secret-key" not in message
    assert minio_settings.minio_bucket not in message
