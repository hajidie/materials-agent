from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class ToolClientError(RuntimeError):
    """Sanitized Tool Client failure."""


class ToolClientTimeoutError(ToolClientError):
    def __init__(self) -> None:
        super().__init__("Tool Runtime timed out.")


class ToolClientUnavailableError(ToolClientError):
    def __init__(self) -> None:
        super().__init__("Tool Runtime is unavailable.")


class ToolClientProtocolError(ToolClientError):
    def __init__(self) -> None:
        super().__init__("Tool Runtime returned an invalid response.")


class ToolClientRuntimeError(ToolClientError):
    def __init__(self, *, code: str, safe_message: str, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    tool_id: str
    tool_version: str
    schema_version: str
    display_name: str
    description: str
    material_scope: str
    enabled: bool
    supported_outputs: tuple[str, ...]
    supported_asset_types: tuple[str, ...]
    execution_mode: str
    requires_gpu: bool
    input_fields: tuple[dict[str, object], ...]
    output_summary: tuple[dict[str, object], ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolExecutionInput:
    process_parameters: dict[str, int | float]
    requested_outputs: tuple[str, ...]
    runtime_parameters: dict[str, int | float]

    def to_json(self) -> dict[str, object]:
        return {
            "process_parameters": dict(self.process_parameters),
            "requested_outputs": list(self.requested_outputs),
            "runtime_parameters": dict(self.runtime_parameters),
        }


@dataclass(frozen=True, slots=True)
class ToolRequestContext:
    request_id: str
    conversation_id: str
    task_id: str
    tool_run_id: str
    actor_id: str
    user_id: str | None
    requested_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "conversation_id",
            "task_id",
            "tool_run_id",
            "actor_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank text.")
        if self.user_id is not None and (
            not isinstance(self.user_id, str) or not self.user_id.strip()
        ):
            raise ValueError("user_id must be null or non-blank text.")
        if (
            self.requested_at.tzinfo is None
            or self.requested_at.utcoffset() is None
            or self.requested_at.utcoffset()
            != timezone.utc.utcoffset(self.requested_at)
        ):
            raise ValueError("requested_at must be timezone-aware UTC.")


@dataclass(frozen=True, slots=True)
class ToolImagePayload:
    image_role: str
    requested_output: bool
    dtype: str
    numpy_dtype: str
    shape: tuple[int, int]
    channel_layout: str
    value_range: tuple[float, float]
    encoding: str
    byte_order: str
    array_order: str
    sha256: str | None
    data_base64: str


@dataclass(frozen=True, slots=True)
class ToolExecutionOutput:
    status: str
    requested_outputs: tuple[str, ...]
    completed_outputs: tuple[str, ...]
    failed_outputs: tuple[str, ...]
    data: dict[str, object]
    images: tuple[ToolImagePayload, ...]
    warnings: tuple[dict[str, object], ...]
    diagnostics: tuple[dict[str, object], ...]
    actual_runtime_parameters: dict[str, int | float]
    model_bundle_id: str | None
    error: dict[str, object] | None

    def safe_summary(self) -> dict[str, object]:
        return {
            "runtime_status": self.status,
            "requested_outputs": list(self.requested_outputs),
            "completed_outputs": list(self.completed_outputs),
            "failed_outputs": list(self.failed_outputs),
            "data_fields": sorted(self.data),
            "image_count": len(self.images),
            "image_roles": [image.image_role for image in self.images],
            "images": [
                {
                    "image_role": image.image_role,
                    "requested_output": image.requested_output,
                    "sha256": image.sha256,
                    "encoding": image.encoding,
                    "shape": list(image.shape),
                }
                for image in self.images
            ],
            "warning_count": len(self.warnings),
            "error": None if self.error is None else dict(self.error),
        }


class ToolClientPort(Protocol):
    def execute(
        self,
        metadata: ToolMetadata,
        validated_input: ToolExecutionInput,
        request_context: ToolRequestContext,
    ) -> ToolExecutionOutput: ...

    def readiness(self, metadata: ToolMetadata) -> str: ...


class MaterialTool(Protocol):
    metadata: ToolMetadata

    def validate_input(
        self,
        normalized_input: dict[str, object],
        *,
        seed: int,
    ) -> ToolExecutionInput: ...

    def execute(
        self,
        validated_input: ToolExecutionInput,
        request_context: ToolRequestContext,
    ) -> ToolExecutionOutput: ...

    def health_check(self) -> str: ...
