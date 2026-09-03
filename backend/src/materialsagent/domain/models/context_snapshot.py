from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from hashlib import sha256
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from materialsagent.domain.ports.conversation_context import (
    ContextBuildResult,
    ContextSource,
)


MAX_CONTEXT_SNAPSHOT_BYTES = 128 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ContextSourceSnapshot(_StrictModel):
    source_type: Literal[
        "MESSAGE",
        "TASK_INPUT_REVISION",
        "TOOL_RUN",
        "TOOL_RESULT",
        "EXPLANATION",
    ]
    task_id: str
    message_ids: Annotated[tuple[str, ...], Field(strict=False)]
    context_ref: str | None = None
    task_input_revision_id: str | None = None
    tool_run_id: str | None = None
    tool_result_id: str | None = None
    explanation_id: str | None = None
    projection_type: str | None = None
    projection_version: str | None = None

    @model_validator(mode="after")
    def validate_source_identity(self) -> ContextSourceSnapshot:
        if not self.task_id.strip():
            raise ValueError("Context source task_id must be non-blank.")
        if (
            not self.message_ids
            or len(self.message_ids) != len(set(self.message_ids))
            or any(not item.strip() for item in self.message_ids)
        ):
            raise ValueError("Context source message_ids are invalid.")
        if self.context_ref is not None and re.fullmatch(
            r"ctx_ref_[0-9]{4,}", self.context_ref
        ) is None:
            raise ValueError("Context source local reference is invalid.")
        if self.context_ref is not None and self.source_type != "TASK_INPUT_REVISION":
            raise ValueError("Only Task input facts may carry a local reference.")
        source_identifiers = {
            "TASK_INPUT_REVISION": self.task_input_revision_id,
            "TOOL_RUN": self.tool_run_id,
            "TOOL_RESULT": self.tool_result_id,
            "EXPLANATION": self.explanation_id,
        }
        expected_identifier = source_identifiers.get(self.source_type)
        if self.source_type != "MESSAGE" and (
            expected_identifier is None or not expected_identifier.strip()
        ):
            raise ValueError("Context source persistent identity is missing.")
        active_identifiers = [
            value for value in source_identifiers.values() if value is not None
        ]
        if len(active_identifiers) != (0 if self.source_type == "MESSAGE" else 1):
            raise ValueError("Context source persistent identity is inconsistent.")
        projection_expected = self.source_type in {"TOOL_RUN", "TOOL_RESULT"}
        projection_present = (
            self.projection_type is not None
            or self.projection_version is not None
        )
        if projection_expected != projection_present:
            raise ValueError("Context source projection metadata is inconsistent.")
        if projection_expected and (
            self.projection_type is None
            or not self.projection_type.strip()
            or self.projection_version is None
            or not self.projection_version.strip()
        ):
            raise ValueError("Context source projection metadata is invalid.")
        return self


class ContextSourceCounts(_StrictModel):
    message: NonNegativeInt
    task_input_revision: NonNegativeInt
    tool_run: NonNegativeInt
    tool_result: NonNegativeInt
    explanation: NonNegativeInt


class ContextSnapshotV1(_StrictModel):
    schema_version: Literal[1]
    mode: Literal["FULL", "AGGREGATED"]
    strategy: Literal["RECENT_COMPLETE_TURNS_V1"]
    purpose: Literal["CHAT_ORCHESTRATION", "TOOL_INPUT_EXTRACTION"]
    model_name: str
    prompt_limit_tokens: NonNegativeInt
    history_token_budget: NonNegativeInt
    safety_margin_tokens: NonNegativeInt
    context_window_tokens: NonNegativeInt
    max_output_tokens: NonNegativeInt
    effective_prompt_budget: int
    base_prompt_tokens: NonNegativeInt
    history_tokens: NonNegativeInt
    final_prompt_tokens: NonNegativeInt
    candidate_turn_count: NonNegativeInt
    selected_turn_count: NonNegativeInt
    omitted_turn_count: NonNegativeInt
    selected_source_count: NonNegativeInt
    selected_sources: Annotated[
        tuple[ContextSourceSnapshot, ...],
        Field(strict=False),
    ]
    first_source: ContextSourceSnapshot | None
    last_source: ContextSourceSnapshot | None
    source_type_counts: ContextSourceCounts
    ordered_source_digest: Digest
    context_digest: Digest
    prompt_digest: Digest
    audit_metadata_truncated: bool
    truncation_reason: Literal["SNAPSHOT_SIZE_LIMIT"] | None
    omitted_source_ref_count: NonNegativeInt

    @model_validator(mode="after")
    def validate_invariants(self) -> ContextSnapshotV1:
        if not self.model_name.strip():
            raise ValueError("model_name must be non-blank.")
        expected_effective = min(
            self.prompt_limit_tokens,
            self.context_window_tokens
            - self.max_output_tokens
            - self.safety_margin_tokens,
        )
        if self.effective_prompt_budget != expected_effective:
            raise ValueError("effective_prompt_budget is inconsistent.")
        if self.candidate_turn_count != (
            self.selected_turn_count + self.omitted_turn_count
        ):
            raise ValueError("Context turn counts are inconsistent.")
        if self.final_prompt_tokens != (
            self.base_prompt_tokens + self.history_tokens
        ):
            raise ValueError("Prompt token counts are inconsistent.")
        if self.selected_source_count != sum(
            (
                self.source_type_counts.message,
                self.source_type_counts.task_input_revision,
                self.source_type_counts.tool_run,
                self.source_type_counts.tool_result,
                self.source_type_counts.explanation,
            )
        ):
            raise ValueError("Context source counts are inconsistent.")
        if (
            self.source_type_counts.message != self.selected_turn_count
            or self.selected_source_count < self.selected_turn_count
        ):
            raise ValueError("Context turn/source relationships are inconsistent.")
        if self.mode == "FULL":
            if (
                self.audit_metadata_truncated
                or self.truncation_reason is not None
                or self.omitted_source_ref_count != 0
                or len(self.selected_sources) != self.selected_source_count
                or self.first_source is not None
                or self.last_source is not None
            ):
                raise ValueError("FULL Context Snapshot metadata is inconsistent.")
            expected_source_digest = sha256(
                canonical_snapshot_bytes(
                    [item.model_dump(mode="json") for item in self.selected_sources]
                )
            ).hexdigest()
            if self.ordered_source_digest != expected_source_digest:
                raise ValueError("ordered_source_digest is inconsistent.")
        elif (
            not self.audit_metadata_truncated
            or self.truncation_reason != "SNAPSHOT_SIZE_LIMIT"
            or self.selected_sources
            or self.omitted_source_ref_count != self.selected_source_count
            or (self.selected_source_count == 0)
            != (self.first_source is None and self.last_source is None)
        ):
            raise ValueError("AGGREGATED Context Snapshot metadata is inconsistent.")
        encoded = canonical_snapshot_bytes(self.model_dump(mode="json"))
        if len(encoded) > MAX_CONTEXT_SNAPSHOT_BYTES:
            raise ValueError("Context Snapshot exceeds the audit size limit.")
        return self


def canonical_snapshot_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Context Snapshot objects must use text keys.")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _source_payload(source: ContextSource) -> dict[str, object]:
    return {
        "source_type": source.source_type,
        "task_id": source.task_id,
        "message_ids": source.message_ids,
        "context_ref": source.context_ref,
        "task_input_revision_id": source.task_input_revision_id,
        "tool_run_id": source.tool_run_id,
        "tool_result_id": source.tool_result_id,
        "explanation_id": source.explanation_id,
        "projection_type": source.projection_type,
        "projection_version": source.projection_version,
    }


def build_context_snapshot(
    result: ContextBuildResult,
    *,
    purpose: Literal["CHAT_ORCHESTRATION", "TOOL_INPUT_EXTRACTION"],
    model_name: str,
    prompt_digest: str,
) -> dict[str, object]:
    source_payloads = [
        ContextSourceSnapshot.model_validate(_source_payload(item)).model_dump(
            mode="json"
        )
        for item in result.selected_sources
    ]
    source_counts = Counter(item.source_type for item in result.selected_sources)
    ordered_source_digest = sha256(
        canonical_snapshot_bytes(source_payloads)
    ).hexdigest()
    common: dict[str, object] = {
        "schema_version": 1,
        "strategy": "RECENT_COMPLETE_TURNS_V1",
        "purpose": purpose,
        "model_name": model_name,
        "prompt_limit_tokens": result.budget.prompt_limit_tokens,
        "history_token_budget": result.budget.history_token_budget,
        "safety_margin_tokens": result.budget.safety_margin_tokens,
        "context_window_tokens": result.budget.context_window_tokens,
        "max_output_tokens": result.budget.max_output_tokens,
        "effective_prompt_budget": result.budget.effective_prompt_budget,
        "base_prompt_tokens": result.base_prompt_tokens,
        "history_tokens": result.history_tokens,
        "final_prompt_tokens": result.final_prompt_tokens,
        "candidate_turn_count": result.candidate_turn_count,
        "selected_turn_count": result.selected_turn_count,
        "omitted_turn_count": result.omitted_turn_count,
        "selected_source_count": len(source_payloads),
        "source_type_counts": {
            "message": source_counts["MESSAGE"],
            "task_input_revision": source_counts["TASK_INPUT_REVISION"],
            "tool_run": source_counts["TOOL_RUN"],
            "tool_result": source_counts["TOOL_RESULT"],
            "explanation": source_counts["EXPLANATION"],
        },
        "ordered_source_digest": ordered_source_digest,
        "context_digest": result.context_digest,
        "prompt_digest": prompt_digest,
    }
    full = {
        **common,
        "mode": "FULL",
        "selected_sources": source_payloads,
        "first_source": None,
        "last_source": None,
        "audit_metadata_truncated": False,
        "truncation_reason": None,
        "omitted_source_ref_count": 0,
    }
    if len(canonical_snapshot_bytes(full)) <= MAX_CONTEXT_SNAPSHOT_BYTES:
        return ContextSnapshotV1.model_validate(full).model_dump(mode="json")
    aggregated = {
        **common,
        "mode": "AGGREGATED",
        "selected_sources": [],
        "first_source": source_payloads[0] if source_payloads else None,
        "last_source": source_payloads[-1] if source_payloads else None,
        "audit_metadata_truncated": True,
        "truncation_reason": "SNAPSHOT_SIZE_LIMIT",
        "omitted_source_ref_count": len(source_payloads),
    }
    return ContextSnapshotV1.model_validate(aggregated).model_dump(mode="json")


def validate_context_snapshot(value: object) -> dict[str, object]:
    return ContextSnapshotV1.model_validate(
        _plain_json(value)
    ).model_dump(mode="json")
