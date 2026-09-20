"""Storage values only: no client, business model, or framework dependencies."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import re


@dataclass(frozen=True)
class ObjectStorageRef:
    object_id: str
    store_id: str
    bucket: str
    storage_key: str
    sha256: str
    size_bytes: int
    media_type: str
    version_id: str | None = None
    contract_version: str = "object-storage-ref-v1"

    def __post_init__(self):
        if self.contract_version != "object-storage-ref-v1":
            raise ValueError("Unsupported object contract")
        for value in (self.object_id, self.store_id, self.bucket, self.storage_key):
            if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid object identity")
        if (len(self.storage_key) > 1024 or "\\" in self.storage_key
                or any(p in ("", ".", "..") for p in self.storage_key.split("/"))):
            raise ValueError("Invalid storage key")
        if self.version_id is not None and (not isinstance(self.version_id, str) or not self.version_id
                or len(self.version_id) > 1024 or any(ord(c) < 32 for c in self.version_id)):
            raise ValueError("Invalid object version")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Invalid SHA-256")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Invalid size")
        if not isinstance(self.media_type, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", self.media_type):
            raise ValueError("Invalid media type")

    def to_dict(self):
        return asdict(self)

    def verify(self, payload: bytes):
        if len(payload) != self.size_bytes or sha256(payload).hexdigest() != self.sha256:
            raise ValueError("Object integrity mismatch")
