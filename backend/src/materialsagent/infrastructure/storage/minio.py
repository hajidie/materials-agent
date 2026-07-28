from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import re
from typing import Any, Mapping
import unicodedata

from minio import Minio
from minio.error import S3Error
import urllib3

from materialsagent.domain.ports.storage import (
    MAX_STORAGE_GET_BYTES,
    StorageConflictError,
    StorageError,
    StorageIntegrityError,
    StorageObjectNotFoundError,
    StoredObjectMetadata,
    StorageUnavailableError,
    StorageWriteOutcomeUnknownError,
    validate_object_key,
)
from materialsagent.infrastructure.config import AppSettings, parse_minio_config


CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_METADATA_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_CONTENT_TYPE_PATTERN = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z"
)
_RESERVED_METADATA_KEYS = frozenset({"sha256", "size-bytes"})


class MinioStorageService:
    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        http_client: urllib3.PoolManager | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._http_client = http_client

    def put(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObjectMetadata:
        validate_object_key(object_key)
        if (
            not isinstance(payload, bytes)
            or not isinstance(content_type, str)
            or _CONTENT_TYPE_PATTERN.fullmatch(content_type) is None
        ):
            raise StorageError("Storage operation failed.")

        normalized_metadata = _normalize_metadata(metadata)
        digest = sha256(payload).hexdigest()
        expected = StoredObjectMetadata(
            object_key=object_key,
            size_bytes=len(payload),
            sha256=digest,
            content_type=content_type,
            metadata=normalized_metadata,
        )

        existing = self.head(object_key)
        if existing is not None:
            if (
                existing.sha256 == expected.sha256
                and existing.size_bytes == expected.size_bytes
                and existing.content_type == expected.content_type
                and existing.metadata == expected.metadata
            ):
                return existing
            raise StorageConflictError("Object storage conflict.")

        try:
            self._client.put_object(
                self._bucket,
                object_key,
                BytesIO(payload),
                len(payload),
                content_type=content_type,
                metadata={
                    **normalized_metadata,
                    "sha256": digest,
                    "size-bytes": str(len(payload)),
                },
            )
        except StorageUnavailableError:
            raise StorageWriteOutcomeUnknownError(
                "Object storage unavailable."
            ) from None
        except StorageError:
            raise
        except Exception:
            raise StorageWriteOutcomeUnknownError(
                "Object storage unavailable."
            ) from None
        return expected

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        validate_object_key(object_key)
        try:
            stat = self._client.stat_object(self._bucket, object_key)
            return _stored_metadata(object_key, stat)
        except S3Error as error:
            if error.code in _NOT_FOUND_CODES:
                return None
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None
        except StorageError:
            raise
        except Exception:
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None

    def get(self, object_key: str, *, max_bytes: int) -> bytes:
        validate_object_key(object_key)
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
            or max_bytes > MAX_STORAGE_GET_BYTES
        ):
            raise StorageError("Storage operation failed.")
        response: Any | None = None
        try:
            response = self._client.get_object(self._bucket, object_key)
            payload = response.read(max_bytes + 1)
            if not isinstance(payload, bytes) or len(payload) > max_bytes:
                raise StorageIntegrityError(
                    "Stored object integrity check failed."
                )
            return payload
        except S3Error as error:
            if error.code in _NOT_FOUND_CODES:
                raise StorageObjectNotFoundError(
                    "Stored object not found."
                ) from None
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None
        except StorageError:
            raise
        except Exception:
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None
        finally:
            _close_response(response)

    def delete(self, object_key: str) -> None:
        validate_object_key(object_key)
        try:
            self._client.remove_object(self._bucket, object_key)
        except Exception:
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None

    def bucket_exists(self) -> bool:
        try:
            return bool(self._client.bucket_exists(self._bucket))
        except Exception:
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None

    def create_bucket(self) -> None:
        try:
            self._client.make_bucket(self._bucket)
        except S3Error as error:
            if error.code in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                return
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None
        except Exception:
            raise StorageUnavailableError(
                "Object storage unavailable."
            ) from None

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.clear()


def create_minio_storage(settings: AppSettings) -> MinioStorageService:
    config = parse_minio_config(settings)
    http_client = urllib3.PoolManager(
        timeout=urllib3.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        ),
        retries=False,
    )
    client = Minio(
        config.endpoint,
        access_key=config.access_key,
        secret_key=config.secret_key,
        secure=config.secure,
        http_client=http_client,
    )
    return MinioStorageService(
        client=client,
        bucket=config.bucket,
        http_client=http_client,
    )


def _stored_metadata(object_key: str, stat: Any) -> StoredObjectMetadata:
    raw_metadata = getattr(stat, "metadata", {}) or {}
    if not isinstance(raw_metadata, Mapping):
        raise StorageIntegrityError("Stored object integrity check failed.")
    metadata: dict[str, str] = {}
    header_content_type: str | None = None
    for key, value in raw_metadata.items():
        if not isinstance(key, str):
            raise StorageIntegrityError(
                "Stored object integrity check failed."
            )
        normalized_key = key.lower()
        if normalized_key == "content-type":
            if not isinstance(value, str):
                raise StorageIntegrityError(
                    "Stored object integrity check failed."
                )
            if header_content_type is not None and header_content_type != value:
                raise StorageIntegrityError(
                    "Stored object integrity check failed."
                )
            header_content_type = value
            continue
        if not (
            normalized_key.startswith("x-amz-meta-")
            or normalized_key in _RESERVED_METADATA_KEYS
        ):
            continue
        if not isinstance(value, str) or normalized_key in metadata:
            raise StorageIntegrityError(
                "Stored object integrity check failed."
            )
        metadata[normalized_key] = value
    digest = metadata.get("x-amz-meta-sha256") or metadata.get("sha256")
    stored_size = metadata.get("x-amz-meta-size-bytes") or metadata.get(
        "size-bytes"
    )
    size_bytes = getattr(stat, "size", None)
    content_type = getattr(stat, "content_type", None) or header_content_type
    if (
        digest is None
        or _SHA256_PATTERN.fullmatch(digest) is None
        or stored_size is None
        or not stored_size.isdecimal()
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
        or int(stored_size) != size_bytes
        or not isinstance(content_type, str)
        or _CONTENT_TYPE_PATTERN.fullmatch(content_type) is None
    ):
        raise StorageIntegrityError("Stored object integrity check failed.")
    custom_metadata = {
        key.removeprefix("x-amz-meta-"): value
        for key, value in metadata.items()
        if key.startswith("x-amz-meta-")
        and key.removeprefix("x-amz-meta-") not in _RESERVED_METADATA_KEYS
    }
    try:
        custom_metadata = _normalize_metadata(custom_metadata)
    except StorageError:
        raise StorageIntegrityError(
            "Stored object integrity check failed."
        ) from None
    return StoredObjectMetadata(
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=digest,
        content_type=str(content_type),
        metadata=custom_metadata,
    )


def _normalize_metadata(
    metadata: Mapping[str, str] | None,
) -> dict[str, str]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise StorageError("Storage operation failed.")
    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        if (
            not isinstance(key, str)
            or _METADATA_KEY_PATTERN.fullmatch(key) is None
            or key in _RESERVED_METADATA_KEYS
            or not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(
                unicodedata.category(character) == "Cc"
                for character in value
            )
        ):
            raise StorageError("Storage operation failed.")
        normalized[key] = value
    return normalized


def _close_response(response: Any | None) -> None:
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass
    try:
        response.release_conn()
    except Exception:
        pass
