from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_utc(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class ResultAssetLink:
    result_id: str
    asset_id: str
    artifact_order: int
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.result_id, "result_id")
        _require_text(self.asset_id, "asset_id")
        if (
            not isinstance(self.artifact_order, int)
            or isinstance(self.artifact_order, bool)
            or self.artifact_order < 0
        ):
            raise ValueError("artifact_order must be a nonnegative integer.")
        _require_utc(self.created_at, "created_at")
