from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import Final


PENDING: Final = "PENDING"
RUNNING: Final = "RUNNING"
SUCCEEDED: Final = "SUCCEEDED"
PARTIALLY_SUCCEEDED: Final = "PARTIALLY_SUCCEEDED"
FAILED: Final = "FAILED"
TOOL_RUN_STATUSES: Final = frozenset(
    {PENDING, RUNNING, SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED}
)
MAX_SAFE_JSON_BYTES: Final = 16384


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text.")


def _require_utc(value: datetime, field_name: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


def _safe_json_object(
    value: dict[str, object] | None,
    field_name: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be null or a JSON object.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must contain safe JSON values.") from None
    if len(encoded) > MAX_SAFE_JSON_BYTES:
        raise ValueError(f"{field_name} exceeds the safe size limit.")
    return value


@dataclass(slots=True)
class ToolRun:
    tool_run_id: str
    task_id: str
    request_id: str
    task_input_revision_id: str
    attempt_no: int
    tool_id: str
    tool_version: str
    schema_version: str
    requested_outputs: list[str]
    completed_outputs: list[str]
    failed_outputs: list[str]
    execution_input: dict[str, object]
    actual_runtime_parameters: dict[str, object] | None
    diagnostics: list[dict[str, object]]
    output_summary: dict[str, object] | None
    current_status: str
    model_bundle_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "tool_run_id",
            "task_id",
            "request_id",
            "task_input_revision_id",
            "tool_id",
            "tool_version",
            "schema_version",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.attempt_no, int) or isinstance(
            self.attempt_no, bool
        ) or self.attempt_no <= 0:
            raise ValueError("attempt_no must be a positive integer.")
        if (
            not isinstance(self.requested_outputs, list)
            or not self.requested_outputs
            or any(not isinstance(item, str) or not item for item in self.requested_outputs)
            or len(set(self.requested_outputs)) != len(self.requested_outputs)
        ):
            raise ValueError("requested_outputs must be a nonempty unique text array.")
        for field_name in ("completed_outputs", "failed_outputs"):
            value = getattr(self, field_name)
            if not isinstance(value, list) or len(set(value)) != len(value):
                raise ValueError(f"{field_name} must be a unique text array.")
            if not set(value) <= set(self.requested_outputs):
                raise ValueError(f"{field_name} must be a requested output subset.")
        if set(self.completed_outputs) & set(self.failed_outputs):
            raise ValueError("completed_outputs and failed_outputs must be disjoint.")
        if not isinstance(self.execution_input, dict):
            raise ValueError("execution_input must be a JSON object.")
        _safe_json_object(self.execution_input, "execution_input")
        _safe_json_object(
            self.actual_runtime_parameters,
            "actual_runtime_parameters",
        )
        if not isinstance(self.diagnostics, list) or not all(
            isinstance(item, dict) for item in self.diagnostics
        ):
            raise ValueError("diagnostics must be a JSON object array.")
        _safe_json_object({"items": self.diagnostics}, "diagnostics")
        _safe_json_object(self.output_summary, "output_summary")
        if self.current_status not in TOOL_RUN_STATUSES:
            raise ValueError("current_status is not an allowed ToolRun status.")
        if self.model_bundle_id is not None:
            _require_text(self.model_bundle_id, "model_bundle_id")
        _require_utc(self.created_at, "created_at")
        if self.started_at is not None:
            _require_utc(self.started_at, "started_at")
            if self.started_at < self.created_at:
                raise ValueError("started_at must not be earlier than created_at.")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
            if self.started_at is None or self.completed_at < self.started_at:
                raise ValueError("completed_at must not be earlier than started_at.")
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a nonnegative integer or null.")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")
        if self.safe_error_message is not None:
            _require_text(self.safe_error_message, "safe_error_message")
            if len(self.safe_error_message) > 256 or not self.safe_error_message.isprintable():
                raise ValueError("safe_error_message must be bounded safe text.")
        if self.current_status == PENDING and any(
            value is not None
            for value in (
                self.started_at,
                self.completed_at,
                self.duration_ms,
                self.actual_runtime_parameters,
                self.output_summary,
                self.error_code,
                self.safe_error_message,
            )
        ):
            raise ValueError("PENDING cannot contain execution outcome fields.")
        if self.current_status == RUNNING and self.started_at is None:
            raise ValueError("RUNNING requires started_at.")
        if self.current_status in {SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED} and (
            self.started_at is None
            or self.completed_at is None
            or self.duration_ms is None
        ):
            raise ValueError("Terminal ToolRun requires timing fields.")
        if self.current_status == FAILED and (
            self.completed_outputs or not self.error_code
        ):
            raise ValueError("FAILED requires zero completed outputs and an error code.")

    @classmethod
    def pending(
        cls,
        *,
        tool_run_id: str,
        task_id: str,
        request_id: str,
        task_input_revision_id: str,
        attempt_no: int,
        tool_id: str,
        tool_version: str,
        schema_version: str,
        execution_input: dict[str, object],
        requested_outputs: list[str],
        created_at: datetime,
    ) -> "ToolRun":
        return cls(
            tool_run_id=tool_run_id,
            task_id=task_id,
            request_id=request_id,
            task_input_revision_id=task_input_revision_id,
            attempt_no=attempt_no,
            tool_id=tool_id,
            tool_version=tool_version,
            schema_version=schema_version,
            requested_outputs=list(requested_outputs),
            completed_outputs=[],
            failed_outputs=[],
            execution_input=execution_input,
            actual_runtime_parameters=None,
            diagnostics=[],
            output_summary=None,
            current_status=PENDING,
            model_bundle_id=None,
            created_at=created_at,
            started_at=None,
            completed_at=None,
            duration_ms=None,
            error_code=None,
            safe_error_message=None,
        )

    def start(self, *, started_at: datetime) -> "ToolRun":
        if self.current_status != PENDING:
            raise ValueError("Only PENDING ToolRun can start.")
        return replace(self, current_status=RUNNING, started_at=started_at)

    def record_runtime_output(
        self,
        *,
        actual_runtime_parameters: dict[str, object],
        diagnostics: list[dict[str, object]],
        output_summary: dict[str, object],
        model_bundle_id: str | None,
    ) -> "ToolRun":
        if self.current_status != RUNNING:
            raise ValueError("Runtime output requires RUNNING ToolRun.")
        return replace(
            self,
            actual_runtime_parameters=actual_runtime_parameters,
            diagnostics=diagnostics,
            output_summary=output_summary,
            model_bundle_id=model_bundle_id,
        )

    def fail(
        self,
        *,
        failed_at: datetime,
        error_code: str,
        safe_error_message: str,
        diagnostics: list[dict[str, object]] | None = None,
        actual_runtime_parameters: dict[str, object] | None = None,
    ) -> "ToolRun":
        if self.current_status != RUNNING or self.started_at is None:
            raise ValueError("Only RUNNING ToolRun can fail.")
        duration_ms = max(
            0,
            int((failed_at - self.started_at).total_seconds() * 1000),
        )
        return replace(
            self,
            current_status=FAILED,
            completed_outputs=[],
            failed_outputs=list(self.requested_outputs),
            actual_runtime_parameters=actual_runtime_parameters,
            diagnostics=list(diagnostics or []),
            completed_at=failed_at,
            duration_ms=duration_ms,
            error_code=error_code,
            safe_error_message=safe_error_message,
        )

    def complete_from_result(
        self,
        *,
        completed_outputs: list[str],
        failed_outputs: list[str],
        completed_at: datetime,
        error_code: str | None,
        safe_error_message: str | None,
    ) -> "ToolRun":
        if self.current_status != RUNNING or self.started_at is None:
            raise ValueError("Only RUNNING ToolRun can complete from a result.")
        completed = list(completed_outputs)
        failed = list(failed_outputs)
        if (
            set(completed) & set(failed)
            or set(completed) | set(failed) != set(self.requested_outputs)
        ):
            raise ValueError("Terminal output sets must exactly cover requested outputs.")
        status = (
            SUCCEEDED
            if len(completed) == len(self.requested_outputs)
            else PARTIALLY_SUCCEEDED
            if completed
            else FAILED
        )
        if status == SUCCEEDED and (
            error_code is not None or safe_error_message is not None
        ):
            raise ValueError("SUCCEEDED ToolRun cannot contain an error.")
        if status != SUCCEEDED and (
            error_code is None or safe_error_message is None
        ):
            raise ValueError("Non-success ToolRun requires a controlled error.")
        duration_ms = max(
            0,
            int((completed_at - self.started_at).total_seconds() * 1000),
        )
        return replace(
            self,
            current_status=status,
            completed_outputs=completed,
            failed_outputs=failed,
            completed_at=completed_at,
            duration_ms=duration_ms,
            error_code=error_code,
            safe_error_message=safe_error_message,
        )
