from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from collections.abc import Mapping
from types import MappingProxyType

from materialsagent.domain.ports.tool_registry import ToolRef


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class TaskInputRevision:
    task_input_revision_id: str
    task_id: str
    request_id: str
    source_llm_call_id: str | None
    source_message_ids: list[str]
    revision: int
    raw_input: dict[str, object]
    normalized_input: dict[str, object] | None
    missing_fields: list[str]
    ambiguous_fields: list[dict[str, object]]
    validation_errors: list[dict[str, object]]
    created_at: datetime
    candidate_tool_refs: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank(
            self.task_input_revision_id,
            "task_input_revision_id",
        )
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.request_id, "request_id")
        if self.source_llm_call_id is not None:
            _require_non_blank(self.source_llm_call_id, "source_llm_call_id")
        if not isinstance(self.source_message_ids, list) or not self.source_message_ids:
            raise ValueError("source_message_ids must contain at least one ID.")
        for source_message_id in self.source_message_ids:
            _require_non_blank(source_message_id, "source_message_ids item")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision <= 0
        ):
            raise ValueError("revision must be a positive integer.")
        if not isinstance(self.raw_input, dict):
            raise ValueError("raw_input must be a JSON object.")
        if self.normalized_input is not None and not isinstance(
            self.normalized_input,
            dict,
        ):
            raise ValueError("normalized_input must be null or a JSON object.")
        if not isinstance(self.missing_fields, list) or not all(
            isinstance(field, str) for field in self.missing_fields
        ):
            raise ValueError("missing_fields must be a string array.")
        if not isinstance(self.ambiguous_fields, list) or not all(
            isinstance(field, dict) for field in self.ambiguous_fields
        ):
            raise ValueError("ambiguous_fields must be a structured array.")
        if not isinstance(self.validation_errors, list) or not all(
            isinstance(error, dict) for error in self.validation_errors
        ):
            raise ValueError("validation_errors must be a structured array.")
        object.__setattr__(
            self,
            "candidate_tool_refs",
            _controlled_candidate_tool_refs(self.candidate_tool_refs),
        )
        _require_utc(self.created_at, "created_at")

    @property
    def is_complete(self) -> bool:
        return (
            self.normalized_input is not None
            and not self.missing_fields
            and not self.ambiguous_fields
            and not self.validation_errors
        )


def _controlled_candidate_tool_refs(
    value: object,
) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("candidate_tool_refs must be a Tool reference array.")
    if len(value) not in {0, 2, 3, 4, 5}:
        raise ValueError("candidate_tool_refs must be empty or contain two to five references.")
    refs: list[Mapping[str, str]] = []
    tool_ids: set[str] = set()
    for item in value:
        if isinstance(item, ToolRef):
            candidate = {
                "tool_id": item.tool_id,
                "version": item.version,
                "schema_hash": item.schema_hash,
            }
        elif isinstance(item, Mapping):
            candidate = dict(item)
        else:
            raise ValueError("candidate_tool_refs must contain Tool references.")
        if set(candidate) != {"tool_id", "version", "schema_hash"} or not all(
            type(candidate[key]) is str for key in candidate
        ):
            raise ValueError("candidate_tool_refs must contain controlled Tool references.")
        if not candidate["tool_id"].strip() or not candidate["version"].strip():
            raise ValueError("candidate_tool_refs must contain non-blank Tool references.")
        if SHA256_PATTERN.fullmatch(candidate["schema_hash"]) is None:
            raise ValueError("candidate_tool_refs must contain SHA-256 schema hashes.")
        if candidate["tool_id"] in tool_ids:
            raise ValueError("candidate_tool_refs must not repeat a Tool.")
        tool_ids.add(candidate["tool_id"])
        refs.append(MappingProxyType(candidate))
    return tuple(refs)
