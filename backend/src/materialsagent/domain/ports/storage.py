from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import unicodedata


MAX_OBJECT_KEY_LENGTH = 1024


class StorageError(RuntimeError):
    """Safe object storage failure without provider details."""


class StorageUnavailableError(StorageError):
    """The configured object storage dependency is unavailable."""


class StorageConflictError(StorageError):
    """An object key is already bound to different content."""


class StorageObjectNotFoundError(StorageError):
    """The requested stored object does not exist."""


class InvalidObjectKeyError(StorageError):
    """The supplied internal object key is unsafe."""


@dataclass(frozen=True, slots=True)
class StoredObjectMetadata:
    object_key: str
    size_bytes: int
    sha256: str
    content_type: str


class StorageService(Protocol):
    def put(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
    ) -> StoredObjectMetadata: ...

    def head(self, object_key: str) -> StoredObjectMetadata | None: ...

    def get(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...


def validate_object_key(object_key: str) -> str:
    if (
        not isinstance(object_key, str)
        or not object_key.strip()
        or len(object_key) > MAX_OBJECT_KEY_LENGTH
        or object_key.startswith("/")
        or object_key.endswith("/")
        or "\\" in object_key
        or "//" in object_key
        or any(segment in {".", ".."} for segment in object_key.split("/"))
        or any(
            unicodedata.category(character) == "Cc"
            for character in object_key
        )
    ):
        raise InvalidObjectKeyError("Invalid object key.")
    return object_key
