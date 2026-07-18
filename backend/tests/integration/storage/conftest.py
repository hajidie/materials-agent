from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from uuid import uuid4

import pytest

from materialsagent.application.bootstrap import ensure_object_storage_bucket
from materialsagent.domain.ports.storage import StorageError
from materialsagent.infrastructure.config import AppSettings, load_settings
from materialsagent.infrastructure.storage.minio import (
    MinioStorageService,
    create_minio_storage,
)


@pytest.fixture(scope="session")
def minio_settings() -> AppSettings:
    return load_settings()


@pytest.fixture(scope="session")
def minio_storage(
    minio_settings: AppSettings,
) -> Iterator[MinioStorageService]:
    storage = create_minio_storage(minio_settings)
    ensure_object_storage_bucket(storage)
    try:
        yield storage
    finally:
        storage.close()


@pytest.fixture
def unique_object_key(
    minio_storage: MinioStorageService,
) -> Iterator[str]:
    object_key = f"m2-test/{uuid4().hex}.bin"
    try:
        yield object_key
    finally:
        with suppress(StorageError):
            minio_storage.delete(object_key)
