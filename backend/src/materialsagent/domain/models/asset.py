from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re
from typing import Final

from materialsagent.domain.ports.storage import (
    InvalidObjectKeyError,
    validate_object_key,
)


PENDING: Final = "PENDING"
AVAILABLE: Final = "AVAILABLE"
FAILED: Final = "FAILED"
ORPHANED: Final = "ORPHANED"
ASSET_STATUSES: Final = frozenset({PENDING, AVAILABLE, FAILED, ORPHANED})
ASSET_ROLES: Final = frozenset(
    {"requested_output", "intermediate", "supporting"}
)
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
MAX_SAFE_JSON_BYTES: Final = 4096


def _require_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")
    return value


def _require_utc(value: datetime | None, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    return value


def _safe_details(value: dict[str, object] | None) -> None:
    if value is None or not isinstance(value, dict):
        raise ValueError("orphan_details must be a JSON object.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("orphan_details must contain safe JSON values.") from None
    if len(encoded) > MAX_SAFE_JSON_BYTES:
        raise ValueError("orphan_details exceeds the safe size limit.")


@dataclass(slots=True)
class Asset:
    asset_id: str
    task_id: str
    producer_tool_run_id: str | None
    actor_id: str
    operation_id: str
    current_status: str
    asset_type: str
    source_type: str
    role: str
    object_key: str
    media_type: str | None
    width: int | None
    height: int | None
    bit_depth: int | None
    sha256: str | None
    size_bytes: int | None
    encoding_rule: str | None
    pending_since: datetime
    created_at: datetime
    available_at: datetime | None
    failed_at: datetime | None
    orphaned_at: datetime | None
    error_code: str | None
    safe_error_message: str | None
    orphan_reason: str | None
    orphan_details: dict[str, object] | None

    def __post_init__(self) -> None:
        for field_name in (
            "asset_id",
            "task_id",
            "actor_id",
            "operation_id",
            "asset_type",
            "source_type",
            "role",
            "object_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.current_status not in ASSET_STATUSES:
            raise ValueError("current_status is not an allowed Asset status.")
        if self.asset_type != "sem_image" or self.source_type != "GENERATED":
            raise ValueError("Only generated SEM image assets are supported.")
        _require_text(self.producer_tool_run_id, "producer_tool_run_id")
        if self.role not in ASSET_ROLES:
            raise ValueError("role is not an allowed Asset role.")
        try:
            validate_object_key(self.object_key)
        except InvalidObjectKeyError:
            raise ValueError("object_key is unsafe.") from None
        created_at = _require_utc(self.created_at, "created_at")
        pending_since = _require_utc(self.pending_since, "pending_since")
        if pending_since < created_at:
            raise ValueError("pending_since must not be earlier than created_at.")
        for field_name in ("available_at", "failed_at", "orphaned_at"):
            value = getattr(self, field_name)
            if value is not None and _require_utc(value, field_name) < created_at:
                raise ValueError(f"{field_name} must not be earlier than created_at.")
        for field_name in ("width", "height", "bit_depth"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive integer or null.")
        if self.size_bytes is not None and (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a nonnegative integer or null.")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str)
            or SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            raise ValueError("sha256 must be a lowercase digest or null.")
        for field_name in (
            "media_type",
            "encoding_rule",
            "error_code",
            "safe_error_message",
            "orphan_reason",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if self.safe_error_message is not None and (
            len(self.safe_error_message) > 256
            or not self.safe_error_message.isprintable()
        ):
            raise ValueError("safe_error_message must be bounded safe text.")
        if self.orphan_details is not None:
            _safe_details(self.orphan_details)

        final_metadata = (
            self.media_type,
            self.width,
            self.height,
            self.bit_depth,
            self.sha256,
            self.size_bytes,
            self.encoding_rule,
        )
        if self.current_status == PENDING and (
            any(value is not None for value in final_metadata)
            or any(
                value is not None
                for value in (
                    self.available_at,
                    self.failed_at,
                    self.orphaned_at,
                    self.error_code,
                    self.safe_error_message,
                    self.orphan_reason,
                    self.orphan_details,
                )
            )
        ):
            raise ValueError("PENDING cannot contain final outcome fields.")
        if self.current_status == AVAILABLE and (
            any(value is None for value in final_metadata)
            or self.available_at is None
            or any(
                value is not None
                for value in (
                    self.failed_at,
                    self.orphaned_at,
                    self.error_code,
                    self.safe_error_message,
                    self.orphan_reason,
                    self.orphan_details,
                )
            )
        ):
            raise ValueError("AVAILABLE requires complete final metadata only.")
        if self.current_status == FAILED and (
            self.failed_at is None
            or self.error_code is None
            or self.safe_error_message is None
            or any(value is not None for value in final_metadata)
            or any(
                value is not None
                for value in (
                    self.available_at,
                    self.orphaned_at,
                    self.orphan_reason,
                    self.orphan_details,
                )
            )
        ):
            raise ValueError("FAILED requires only a committed safe failure.")
        if self.current_status == ORPHANED and (
            self.orphaned_at is None
            or self.orphan_reason is None
            or self.orphan_details is None
            or not (
                (
                    all(value is None for value in final_metadata)
                    and self.available_at is None
                )
                or (
                    all(value is not None for value in final_metadata)
                    and self.available_at is not None
                )
            )
            or any(
                value is not None
                for value in (
                    self.failed_at,
                    self.error_code,
                    self.safe_error_message,
                )
            )
        ):
            raise ValueError("ORPHANED requires safe orphan facts.")

    @classmethod
    def pending(
        cls,
        *,
        asset_id: str,
        task_id: str,
        producer_tool_run_id: str,
        actor_id: str,
        operation_id: str,
        role: str,
        object_key: str,
        pending_since: datetime,
        created_at: datetime,
    ) -> "Asset":
        return cls(
            asset_id=asset_id,
            task_id=task_id,
            producer_tool_run_id=producer_tool_run_id,
            actor_id=actor_id,
            operation_id=operation_id,
            current_status=PENDING,
            asset_type="sem_image",
            source_type="GENERATED",
            role=role,
            object_key=object_key,
            media_type=None,
            width=None,
            height=None,
            bit_depth=None,
            sha256=None,
            size_bytes=None,
            encoding_rule=None,
            pending_since=pending_since,
            created_at=created_at,
            available_at=None,
            failed_at=None,
            orphaned_at=None,
            error_code=None,
            safe_error_message=None,
            orphan_reason=None,
            orphan_details=None,
        )

    def mark_available(
        self,
        *,
        sha256: str,
        size_bytes: int,
        width: int,
        height: int,
        bit_depth: int,
        media_type: str,
        encoding_rule: str,
        available_at: datetime,
    ) -> "Asset":
        if self.current_status not in {PENDING, ORPHANED}:
            raise ValueError("Asset cannot transition to AVAILABLE.")
        return replace(
            self,
            current_status=AVAILABLE,
            media_type=media_type,
            width=width,
            height=height,
            bit_depth=bit_depth,
            sha256=sha256,
            size_bytes=size_bytes,
            encoding_rule=encoding_rule,
            available_at=available_at,
            failed_at=None,
            orphaned_at=None,
            error_code=None,
            safe_error_message=None,
            orphan_reason=None,
            orphan_details=None,
        )

    def mark_failed(
        self,
        *,
        error_code: str,
        safe_error_message: str,
        failed_at: datetime,
    ) -> "Asset":
        if self.current_status not in {PENDING, ORPHANED}:
            raise ValueError("Asset cannot transition to FAILED.")
        return replace(
            self,
            current_status=FAILED,
            media_type=None,
            width=None,
            height=None,
            bit_depth=None,
            sha256=None,
            size_bytes=None,
            encoding_rule=None,
            available_at=None,
            failed_at=failed_at,
            orphaned_at=None,
            error_code=error_code,
            safe_error_message=safe_error_message,
            orphan_reason=None,
            orphan_details=None,
        )

    def mark_orphaned(
        self,
        *,
        orphan_reason: str,
        orphan_details: dict[str, object],
        orphaned_at: datetime,
    ) -> "Asset":
        if self.current_status not in {PENDING, AVAILABLE}:
            raise ValueError("Asset cannot transition to ORPHANED.")
        return replace(
            self,
            current_status=ORPHANED,
            failed_at=None,
            orphaned_at=orphaned_at,
            error_code=None,
            safe_error_message=None,
            orphan_reason=orphan_reason,
            orphan_details=dict(orphan_details),
        )
