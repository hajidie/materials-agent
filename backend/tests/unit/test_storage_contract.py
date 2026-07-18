from __future__ import annotations

from hashlib import sha256
import ast
from pathlib import Path
from typing import cast

import pytest


INVALID_OBJECT_KEYS = [
    "",
    "   ",
    "/leading.bin",
    "trailing.bin/",
    "windows\\path.bin",
    "double//slash.bin",
    ".",
    "..",
    "safe/./name.bin",
    "safe/../name.bin",
    "control\x00name.bin",
    "control\nname.bin",
    "x" * 1025,
]


class MemoryStorageService:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, object]] = {}

    def put(self, object_key: str, payload: bytes, content_type: str):
        from materialsagent.domain.ports.storage import (
            StorageConflictError,
            StoredObjectMetadata,
            validate_object_key,
        )

        validate_object_key(object_key)
        digest = sha256(payload).hexdigest()
        existing = self._objects.get(object_key)
        if existing is not None:
            existing_payload, metadata = existing
            if len(existing_payload) == len(payload) and sha256(
                existing_payload
            ).hexdigest() == digest:
                return metadata
            raise StorageConflictError("Object storage conflict.")

        metadata = StoredObjectMetadata(
            object_key=object_key,
            size_bytes=len(payload),
            sha256=digest,
            content_type=content_type,
        )
        self._objects[object_key] = (payload, metadata)
        return metadata

    def head(self, object_key: str):
        from materialsagent.domain.ports.storage import validate_object_key

        validate_object_key(object_key)
        existing = self._objects.get(object_key)
        return None if existing is None else existing[1]

    def get(self, object_key: str) -> bytes:
        from materialsagent.domain.ports.storage import (
            StorageObjectNotFoundError,
            validate_object_key,
        )

        validate_object_key(object_key)
        existing = self._objects.get(object_key)
        if existing is None:
            raise StorageObjectNotFoundError("Stored object not found.")
        return existing[0]

    def delete(self, object_key: str) -> None:
        from materialsagent.domain.ports.storage import validate_object_key

        validate_object_key(object_key)
        self._objects.pop(object_key, None)


@pytest.mark.parametrize("object_key", INVALID_OBJECT_KEYS)
def test_invalid_object_keys_are_rejected(object_key: str) -> None:
    from materialsagent.domain.ports.storage import (
        InvalidObjectKeyError,
        validate_object_key,
    )

    with pytest.raises(
        InvalidObjectKeyError,
        match=r"^Invalid object key\.$",
    ):
        validate_object_key(object_key)


@pytest.mark.parametrize(
    "object_key",
    [
        "m2/acceptance/opaque-id.bin",
        "assets/opaque-id.png",
        "x" * 1024,
    ],
)
def test_safe_internal_object_keys_are_accepted(object_key: str) -> None:
    from materialsagent.domain.ports.storage import validate_object_key

    assert validate_object_key(object_key) == object_key


def test_storage_port_does_not_import_minio_sdk() -> None:
    from materialsagent.domain.ports import storage

    tree = ast.parse(Path(storage.__file__).read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert "minio" not in imported_roots


def test_same_key_and_same_payload_is_an_idempotent_replay() -> None:
    storage = MemoryStorageService()

    first = storage.put("m2/acceptance/replay.bin", b"same", "application/octet-stream")
    replay = storage.put("m2/acceptance/replay.bin", b"same", "application/octet-stream")

    assert replay == first
    assert storage.get(first.object_key) == b"same"


def test_same_key_and_different_payload_is_a_conflict_without_overwrite() -> None:
    from materialsagent.domain.ports.storage import StorageConflictError

    storage = MemoryStorageService()
    key = "m2/acceptance/conflict.bin"
    storage.put(key, b"original", "application/octet-stream")

    with pytest.raises(
        StorageConflictError,
        match=r"^Object storage conflict\.$",
    ):
        storage.put(key, b"different", "application/octet-stream")

    assert storage.get(key) == b"original"


def test_head_missing_get_missing_and_repeated_delete_are_explicit() -> None:
    from materialsagent.domain.ports.storage import StorageObjectNotFoundError

    storage = MemoryStorageService()
    key = "m2/acceptance/missing.bin"

    assert storage.head(key) is None
    with pytest.raises(
        StorageObjectNotFoundError,
        match=r"^Stored object not found\.$",
    ):
        storage.get(key)

    storage.delete(key)
    storage.delete(key)


def test_minio_adapter_rejects_invalid_key_before_network_call() -> None:
    from materialsagent.domain.ports.storage import InvalidObjectKeyError
    from materialsagent.infrastructure.storage.minio import MinioStorageService

    class NetworkForbiddenClient:
        def __getattr__(self, name: str):
            raise AssertionError(f"network method accessed: {name}")

    storage = MinioStorageService(
        client=NetworkForbiddenClient(),
        bucket="test-bucket",
    )

    with pytest.raises(InvalidObjectKeyError):
        storage.get("../unsafe.bin")


@pytest.mark.parametrize("invalid_content_type", [None, 123, "   "])
def test_minio_put_rejects_invalid_content_type_before_network_call(
    invalid_content_type: object,
) -> None:
    from materialsagent.domain.ports.storage import StorageError
    from materialsagent.infrastructure.storage.minio import MinioStorageService

    class NetworkForbiddenClient:
        def __init__(self) -> None:
            self.access_count = 0

        def __getattr__(self, name: str):
            self.access_count += 1
            raise AssertionError(f"network method accessed: {name}")

    client = NetworkForbiddenClient()
    storage = MinioStorageService(client=client, bucket="test-bucket")

    with pytest.raises(StorageError) as exc_info:
        storage.put(
            "m2/acceptance/content-type.bin",
            b"payload",
            cast(str, invalid_content_type),
        )

    assert str(exc_info.value) == "Storage operation failed."
    assert not isinstance(exc_info.value, AttributeError)
    assert client.access_count == 0


def test_bucket_bootstrap_creates_only_when_missing_and_is_idempotent() -> None:
    from materialsagent.application.bootstrap import ensure_object_storage_bucket

    class FakeBucketAdmin:
        def __init__(self) -> None:
            self.exists = False
            self.create_count = 0

        def bucket_exists(self) -> bool:
            return self.exists

        def create_bucket(self) -> None:
            self.create_count += 1
            self.exists = True

    storage = FakeBucketAdmin()

    ensure_object_storage_bucket(storage)
    ensure_object_storage_bucket(storage)

    assert storage.exists is True
    assert storage.create_count == 1


def test_bucket_bootstrap_failure_is_a_safe_storage_error() -> None:
    from materialsagent.application.bootstrap import ensure_object_storage_bucket
    from materialsagent.domain.ports.storage import StorageUnavailableError

    class FailingBucketAdmin:
        def bucket_exists(self) -> bool:
            raise StorageUnavailableError("Object storage unavailable.")

        def create_bucket(self) -> None:
            raise AssertionError("must not create after a failed check")

    with pytest.raises(
        StorageUnavailableError,
        match=r"^Object storage unavailable\.$",
    ):
        ensure_object_storage_bucket(FailingBucketAdmin())


def test_get_closes_and_releases_http_response() -> None:
    from materialsagent.infrastructure.storage.minio import MinioStorageService

    events: list[str] = []

    class FakeResponse:
        def read(self) -> bytes:
            events.append("read")
            return b"payload"

        def close(self) -> None:
            events.append("close")

        def release_conn(self) -> None:
            events.append("release")

    class FakeClient:
        def get_object(self, bucket: str, object_key: str) -> FakeResponse:
            events.append("get")
            return FakeResponse()

    storage = MinioStorageService(client=FakeClient(), bucket="test-bucket")

    assert storage.get("m2/acceptance/response.bin") == b"payload"
    assert events == ["get", "read", "close", "release"]


def test_storage_error_messages_are_stable_and_sanitized() -> None:
    from materialsagent.domain.ports.storage import (
        InvalidObjectKeyError,
        StorageConflictError,
        StorageError,
        StorageObjectNotFoundError,
        StorageUnavailableError,
    )

    errors = [
        StorageError("Storage operation failed."),
        StorageUnavailableError("Object storage unavailable."),
        StorageConflictError("Object storage conflict."),
        StorageObjectNotFoundError("Stored object not found."),
        InvalidObjectKeyError("Invalid object key."),
    ]

    assert [str(error) for error in errors] == [
        "Storage operation failed.",
        "Object storage unavailable.",
        "Object storage conflict.",
        "Stored object not found.",
        "Invalid object key.",
    ]
