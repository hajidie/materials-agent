from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import re
from typing import Any

from minio import Minio
from minio.error import S3Error
import urllib3

from materialsagent.domain.ports.storage import (
    StorageConflictError,
    StorageError,
    StorageObjectNotFoundError,
    StoredObjectMetadata,
    StorageUnavailableError,
    validate_object_key,
)
from materialsagent.infrastructure.config import AppSettings, parse_minio_config


CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


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
    ) -> StoredObjectMetadata:
        validate_object_key(object_key)
        if (
            not isinstance(payload, bytes)
            or not isinstance(content_type, str)
            or not content_type.strip()
        ):
            raise StorageError("Storage operation failed.")

        digest = sha256(payload).hexdigest()
        expected = StoredObjectMetadata(
            object_key=object_key,
            size_bytes=len(payload),
            sha256=digest,
            content_type=content_type,
        )

        existing = self.head(object_key)
        if existing is not None:
            if (
                existing.sha256 == expected.sha256
                and existing.size_bytes == expected.size_bytes
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
                    "sha256": digest,
                    "size-bytes": str(len(payload)),
                },
            )
        except StorageError:
            raise
        except Exception:
            raise StorageUnavailableError(
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

    def get(self, object_key: str) -> bytes:
        validate_object_key(object_key)
        response: Any | None = None
        try:
            response = self._client.get_object(self._bucket, object_key)
            return response.read()
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
    metadata = {
        str(key).lower(): str(value)
        for key, value in dict(getattr(stat, "metadata", {}) or {}).items()
    }
    digest = metadata.get("x-amz-meta-sha256") or metadata.get("sha256")
    stored_size = metadata.get("x-amz-meta-size-bytes") or metadata.get(
        "size-bytes"
    )
    size_bytes = int(getattr(stat, "size"))
    content_type = getattr(stat, "content_type", None) or metadata.get(
        "x-amz-meta-content-type"
    )
    if (
        digest is None
        or _SHA256_PATTERN.fullmatch(digest) is None
        or stored_size is None
        or int(stored_size) != size_bytes
        or not content_type
    ):
        raise StorageError("Storage metadata is invalid.")
    return StoredObjectMetadata(
        object_key=object_key,
        size_bytes=size_bytes,
        sha256=digest,
        content_type=str(content_type),
    )


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
