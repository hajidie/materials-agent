from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import secrets
import json

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationError,
    ApplicationInternalError,
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.tools import StaticToolRegistry, UnknownToolError
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.tool_execution import (
    ToolClientError,
    ToolClientProtocolError,
    ToolClientRuntimeError,
    ToolClientTimeoutError,
    ToolClientUnavailableError,
    ToolExecutionInput,
    ToolExecutionOutput,
    ToolRequestContext,
)
from materialsagent.domain.ports.unit_of_work import UnitOfWorkFactory


class ToolExecutionOutcomeError(ApplicationError):
    """A persisted ToolRun failure projected to a controlled HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        task_id: str,
        tool_run_id: str,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=status_code,
            task_id=task_id,
        )
        self.tool_run_id = tool_run_id


@dataclass(frozen=True, slots=True)
class ToolExecutionReceipt:
    """Internal-only receipt retaining the one in-memory Runtime output."""

    tool_run: ToolRun
    output: ToolExecutionOutput
    output_fingerprint: str = ""

    def __post_init__(self) -> None:
        fingerprint = _tool_output_fingerprint(self.output)
        if self.output_fingerprint and self.output_fingerprint != fingerprint:
            raise ValueError("ToolExecutionReceipt fingerprint mismatch.")
        object.__setattr__(self, "output_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class _PreparedToolAttempt:
    tool_run: ToolRun
    validated_input: ToolExecutionInput
    conversation_id: str
    initial_chain_activated: bool


def _tool_output_fingerprint(output: ToolExecutionOutput) -> str:
    value = {
        "status": output.status,
        "requested_outputs": list(output.requested_outputs),
        "completed_outputs": list(output.completed_outputs),
        "failed_outputs": list(output.failed_outputs),
        "data": output.data,
        "images": [
            {
                field: getattr(image, field)
                for field in image.__dataclass_fields__
            }
            for image in output.images
        ],
        "warnings": list(output.warnings),
        "diagnostics": list(output.diagnostics),
        "actual_runtime_parameters": output.actual_runtime_parameters,
        "model_bundle_id": output.model_bundle_id,
        "error": output.error,
    }
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Tool execution output is not fingerprintable.") from None
    return sha256(encoded).hexdigest()


def _default_seed() -> int:
    return secrets.randbelow(2**31)


def _safe_runtime_error(error: ToolClientError) -> tuple[str, str, int]:
    if isinstance(error, ToolClientTimeoutError):
        return "RUNTIME_TIMEOUT", "Tool Runtime timed out.", 504
    if isinstance(error, ToolClientUnavailableError):
        return "RUNTIME_UNAVAILABLE", "Tool Runtime is unavailable.", 503
    if isinstance(error, ToolClientProtocolError):
        return "RUNTIME_PROTOCOL_ERROR", "Tool Runtime returned an invalid response.", 502
    if isinstance(error, ToolClientRuntimeError):
        projections = {
            "INVALID_RUNTIME_REQUEST": (
                "Tool Runtime rejected the execution request.",
                502,
            ),
            "UNSUPPORTED_TOOL": (
                "Tool Runtime does not support the requested tool.",
                502,
            ),
            "SCHEMA_VERSION_MISMATCH": (
                "Tool Runtime schema version is incompatible.",
                502,
            ),
            "TOOL_VERSION_MISMATCH": (
                "Tool Runtime tool version is incompatible.",
                502,
            ),
            "RUNTIME_NOT_READY": ("Tool Runtime is not ready.", 503),
            "RUNTIME_BUSY": ("Tool Runtime is busy.", 503),
            "MODEL_LOAD_FAILED": ("Tool Runtime model is unavailable.", 503),
            "SEM_GENERATION_FAILED": ("SEM generation failed.", 502),
            "MECHANICAL_PROPERTY_PREDICTION_FAILED": (
                "Mechanical property prediction failed.",
                502,
            ),
            "INVALID_MODEL_OUTPUT": (
                "Tool Runtime returned invalid model output.",
                502,
            ),
            "INTERNAL_RUNTIME_ERROR": ("Tool Runtime failed internally.", 502),
        }
        projection = projections.get(error.code)
        if projection is None:
            return (
                "RUNTIME_PROTOCOL_ERROR",
                "Tool Runtime returned an invalid response.",
                502,
            )
        safe_message, status_code = projection
        return error.code, safe_message, status_code
    return "TOOL_EXECUTION_FAILED", "Tool execution failed.", 502


def normalize_tool_output_summary(
    output: ToolExecutionOutput,
) -> dict[str, object]:
    summary = output.safe_summary()
    if output.error is None:
        return summary
    code = output.error.get("code")
    safe_message = output.error.get("safe_message")
    retryable = output.error.get("retryable")
    if (
        not isinstance(code, str)
        or not isinstance(safe_message, str)
        or not isinstance(retryable, bool)
    ):
        normalized = ToolClientProtocolError()
    else:
        normalized = ToolClientRuntimeError(
            code=code,
            safe_message=safe_message,
            retryable=retryable,
        )
    public_code, public_message, _status_code = _safe_runtime_error(normalized)
    summary["error"] = {
        "code": public_code,
        "safe_message": public_message,
        "retryable": retryable if isinstance(retryable, bool) else False,
    }
    return summary


def _is_equivalent_failed_fact(
    tool_run: ToolRun | None,
    *,
    code: str,
    safe_message: str,
    output: ToolExecutionOutput | None,
) -> bool:
    if tool_run is None or tool_run.current_status != "FAILED":
        return False
    expected_diagnostics = (
        [dict(item) for item in output.diagnostics] if output is not None else []
    )
    expected_runtime_parameters = (
        dict(output.actual_runtime_parameters) if output is not None else None
    )
    return (
        tool_run.completed_outputs == []
        and tool_run.failed_outputs == tool_run.requested_outputs
        and tool_run.error_code == code
        and tool_run.safe_error_message == safe_message
        and tool_run.diagnostics == expected_diagnostics
        and tool_run.actual_runtime_parameters == expected_runtime_parameters
    )


class ToolExecutionService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        registry: StaticToolRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        seed_factory: Callable[[], int] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: f"tool_run_{uuid4().hex}")
        self._seed_factory = seed_factory or _default_seed

    def execute_revision(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> ToolRun:
        return self.execute_revision_with_output(
            actor_context,
            task_id=task_id,
            task_input_revision_id=task_input_revision_id,
            request_id=request_id,
        ).tool_run

    def execute_revision_with_output(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> ToolExecutionReceipt:
        prepared = self._prepare_initial_attempt(
            actor_context,
            task_id=task_id,
            task_input_revision_id=task_input_revision_id,
            request_id=request_id,
        )
        try:
            output = self._invoke_runtime(
                actor_context,
                prepared=prepared,
                request_id=request_id,
            )
        except ToolClientError as error:
            self._raise_persisted_runtime_failure(
                prepared,
                error=error,
            )

        runtime_error = self._runtime_output_error(
            output,
            initial_chain_activated=prepared.initial_chain_activated,
        )
        if runtime_error is not None:
            self._raise_persisted_runtime_failure(
                prepared,
                error=runtime_error,
                output=output,
            )
        return self._persist_runtime_success(prepared, output)

    def _prepare_initial_attempt(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> _PreparedToolAttempt:
        (
            pending,
            validated_input,
            conversation_id,
            initial_chain_activated,
        ) = self._create_initial_pending_attempt(
            actor_context,
            task_id=task_id,
            task_input_revision_id=task_input_revision_id,
            request_id=request_id,
        )
        running = self._start_pending_attempt(
            pending.tool_run_id,
            task_id=task_id,
        )
        return _PreparedToolAttempt(
            tool_run=running,
            validated_input=validated_input,
            conversation_id=conversation_id,
            initial_chain_activated=initial_chain_activated,
        )

    def _create_initial_pending_attempt(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> tuple[ToolRun, ToolExecutionInput, str, bool]:
        tool_run_id = self._id_factory()
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(task_id, actor_context.actor_id)
                revision = unit_of_work.task_input_revisions.get(
                    task_input_revision_id
                )
                if (
                    task is None
                    or revision is None
                    or revision.task_id != task.task_id
                    or revision.normalized_input is None
                    or revision.missing_fields
                    or revision.ambiguous_fields
                    or revision.validation_errors
                    or revision.source_llm_call_id is None
                ):
                    raise ResourceNotFoundError(task_id=task_id)
                activated_chain = (
                    task.task_type == "TOOL_EXECUTION"
                    and task.current_status == "RUNNING"
                    and task.error_code is None
                    and task.safe_error_message is None
                    and task.selected_tool_run_id is None
                    and task.selected_result_id is None
                )
                legacy_activation = (
                    task.task_type == "TOOL_EXECUTION"
                    and task.current_status == "FAILED"
                    and task.error_code == "TOOL_UNAVAILABLE"
                    and task.selected_tool_run_id is None
                    and task.selected_result_id is None
                )
                if not (activated_chain or legacy_activation):
                    raise ApplicationConflictError(task_id=task_id)
                llm_call = unit_of_work.llm_calls.get(
                    revision.source_llm_call_id
                )
                summary = (
                    llm_call.structured_output_summary
                    if llm_call is not None
                    else None
                )
                if (
                    llm_call is None
                    or llm_call.task_id != task_id
                    or llm_call.status != "SUCCEEDED"
                    or not isinstance(summary, Mapping)
                    or summary.get("route") != "TOOL_EXECUTION"
                    or not isinstance(summary.get("tool_id"), str)
                ):
                    raise ResourceNotFoundError(task_id=task_id)
                try:
                    registration = self._registry.resolve(summary["tool_id"])
                except UnknownToolError:
                    raise ResourceNotFoundError(task_id=task_id) from None
                existing = unit_of_work.tool_runs.list_for_task(task_id)
                attempt_no = max((item.attempt_no for item in existing), default=0) + 1
                used_seeds = {
                    runtime_parameters["seed"]
                    for item in existing
                    if isinstance(item.execution_input.get("runtime_parameters"), dict)
                    for runtime_parameters in [
                        item.execution_input["runtime_parameters"]
                    ]
                    if isinstance(runtime_parameters.get("seed"), int)
                }
                for _ in range(16):
                    seed = self._seed_factory()
                    if (
                        isinstance(seed, int)
                        and not isinstance(seed, bool)
                        and seed >= 0
                        and seed not in used_seeds
                    ):
                        break
                else:
                    raise ApplicationConflictError(task_id=task_id)
                try:
                    validated_input = registration.tool.validate_input(
                        revision.normalized_input,
                        seed=seed,
                    )
                except ValueError:
                    raise ApplicationValidationError(task_id=task_id) from None
                pending = ToolRun.pending(
                    tool_run_id=tool_run_id,
                    task_id=task_id,
                    request_id=request_id,
                    task_input_revision_id=task_input_revision_id,
                    attempt_no=attempt_no,
                    tool_id=registration.metadata.tool_id,
                    tool_version=registration.metadata.tool_version,
                    schema_version=registration.metadata.schema_version,
                    execution_input=validated_input.to_json(),
                    requested_outputs=list(validated_input.requested_outputs),
                    created_at=self._clock(),
                )
                unit_of_work.tool_runs.add(pending)
                unit_of_work.commit()
                return (
                    pending,
                    validated_input,
                    task.conversation_id,
                    activated_chain,
                )
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _start_pending_attempt(
        self,
        tool_run_id: str,
        *,
        task_id: str,
    ) -> ToolRun:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                pending = unit_of_work.tool_runs.get(tool_run_id)
                if pending is None:
                    raise ResourceNotFoundError(task_id=task_id)
                running = pending.start(started_at=self._clock())
                if unit_of_work.tool_runs.update(
                    running,
                    expected_status="PENDING",
                ) is None:
                    raise ResourceNotFoundError(task_id=task_id)
                unit_of_work.commit()
                return running
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _invoke_runtime(
        self,
        actor_context: ActorContext,
        *,
        prepared: _PreparedToolAttempt,
        request_id: str,
    ) -> ToolExecutionOutput:
        registration = self._registry.resolve(prepared.tool_run.tool_id)
        context = ToolRequestContext(
            request_id=request_id,
            conversation_id=prepared.conversation_id,
            task_id=prepared.tool_run.task_id,
            tool_run_id=prepared.tool_run.tool_run_id,
            actor_id=actor_context.actor_id,
            user_id=actor_context.user_id,
            requested_at=prepared.tool_run.started_at,
        )
        return registration.tool.execute(prepared.validated_input, context)

    @staticmethod
    def _runtime_output_error(
        output: ToolExecutionOutput,
        *,
        initial_chain_activated: bool,
    ) -> ToolClientError | None:
        if output.status not in {
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
        } or (
            output.status == "FAILED" and not initial_chain_activated
        ):
            error = output.error or {}
            if (
                isinstance(error.get("code"), str)
                and isinstance(error.get("safe_message"), str)
                and isinstance(error.get("retryable"), bool)
            ):
                runtime_error: ToolClientError = ToolClientRuntimeError(
                    code=error["code"],
                    safe_message=error["safe_message"],
                    retryable=error["retryable"],
                )
            else:
                runtime_error = ToolClientProtocolError()
            return runtime_error
        return None

    def _persist_runtime_success(
        self,
        prepared: _PreparedToolAttempt,
        output: ToolExecutionOutput,
    ) -> ToolExecutionReceipt:
        task_id = prepared.tool_run.task_id
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get(
                    prepared.tool_run.tool_run_id
                )
                if current is None:
                    raise ResourceNotFoundError(task_id=task_id)
                recorded = current.record_runtime_output(
                    actual_runtime_parameters=dict(output.actual_runtime_parameters),
                    diagnostics=[dict(item) for item in output.diagnostics],
                    output_summary=normalize_tool_output_summary(output),
                    model_bundle_id=output.model_bundle_id,
                )
                if unit_of_work.tool_runs.update(
                    recorded,
                    expected_status="RUNNING",
                ) is None:
                    raise ResourceNotFoundError(task_id=task_id)
                unit_of_work.commit()
                return ToolExecutionReceipt(tool_run=recorded, output=output)
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _raise_persisted_runtime_failure(
        self,
        prepared: _PreparedToolAttempt,
        *,
        error: ToolClientError,
        output: ToolExecutionOutput | None = None,
    ) -> None:
        code, safe_message, status_code = _safe_runtime_error(error)
        self._persist_runtime_failure(
            prepared.tool_run.tool_run_id,
            task_id=prepared.tool_run.task_id,
            code=code,
            safe_message=safe_message,
            output=output,
        )
        raise ToolExecutionOutcomeError(
            safe_message,
            code=code,
            status_code=status_code,
            task_id=prepared.tool_run.task_id,
            tool_run_id=prepared.tool_run.tool_run_id,
        )

    def _persist_runtime_failure(
        self,
        tool_run_id: str,
        *,
        task_id: str,
        code: str,
        safe_message: str,
        output: ToolExecutionOutput | None = None,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get(tool_run_id)
                if current is None:
                    raise ApplicationInternalError(task_id=task_id)
                if current.current_status != "RUNNING":
                    if _is_equivalent_failed_fact(
                        current,
                        code=code,
                        safe_message=safe_message,
                        output=output,
                    ):
                        return
                    raise ApplicationConflictError(task_id=task_id)
                failed = current.fail(
                    failed_at=self._clock(),
                    error_code=code,
                    safe_error_message=safe_message,
                    diagnostics=(
                        [dict(item) for item in output.diagnostics]
                        if output is not None
                        else []
                    ),
                    actual_runtime_parameters=(
                        dict(output.actual_runtime_parameters)
                        if output is not None
                        else None
                    ),
                )
                updated = unit_of_work.tool_runs.update(
                    failed,
                    expected_status="RUNNING",
                )
                if updated is None:
                    observed = unit_of_work.tool_runs.get(tool_run_id)
                    if _is_equivalent_failed_fact(
                        observed,
                        code=code,
                        safe_message=safe_message,
                        output=output,
                    ):
                        return
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None


class ToolRunQueryService:
    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def get(self, actor_context: ActorContext, tool_run_id: str) -> ToolRun:
        tool_run, _asset_ids = self.get_with_asset_ids(
            actor_context,
            tool_run_id,
        )
        return tool_run

    def get_with_asset_ids(
        self,
        actor_context: ActorContext,
        tool_run_id: str,
    ) -> tuple[ToolRun, list[str]]:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor_context.actor_id,
                )
                assets = (
                    unit_of_work.assets.list_for_tool_run(tool_run_id)
                    if tool_run is not None
                    else []
                )
        except Exception as error:
            raise from_persistence_error(error) from None
        if tool_run is None:
            raise ResourceNotFoundError()
        return tool_run, [asset.asset_id for asset in assets]
