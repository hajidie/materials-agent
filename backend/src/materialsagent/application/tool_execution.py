from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
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
    IdempotencyConflictError,
    TaskNotRetryableError,
    ToolExecutionNotAllowedError,
    ToolSchemaDriftError,
    from_persistence_error,
)
from materialsagent.application.idempotency import (
    IdempotencyOutcome,
    TOOL_RETRY,
    recovered_idempotency_outcome,
)
from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.application.tool_registry import (
    ToolAuthorizationDenialReason,
    ToolAuthorizationError,
    ToolRegistry,
    UnknownToolError,
)
from materialsagent.domain.models.task import Task
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
from materialsagent.domain.ports.tool_registry import (
    AuthorizationDecision,
    ToolAction,
    ToolDefinition,
    ToolRef,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


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


@dataclass(frozen=True, slots=True)
class ReservedToolRetry:
    tool_run: ToolRun
    idempotency_outcome: IdempotencyOutcome

    @property
    def idempotency_replayed(self) -> bool:
        return self.idempotency_outcome.replayed


@dataclass(frozen=True, slots=True)
class _StartedToolAttempt:
    tool_run: ToolRun
    invoke_runtime: bool


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
        registry: ToolRegistry,
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

    def reserve_retry_attempt(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        request_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ReservedToolRetry:
        tool_run_id = self._id_factory()
        timestamp = self._clock()
        record_id = f"idem_{uuid4().hex}"
        try:
            with self._unit_of_work_factory() as unit_of_work:
                existing_record = (
                    unit_of_work.idempotency_records.get_by_scope(
                        actor_context.actor_id,
                        TOOL_RETRY,
                        idempotency_key,
                    )
                )
                if existing_record is not None:
                    return self._load_reserved_retry(
                        unit_of_work,
                        actor_context,
                        task_id=task_id,
                        record=existing_record,
                        request_digest=request_digest,
                        idempotency_outcome=IdempotencyOutcome.REPLAY,
                    )
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor_context.actor_id,
                )
                if task is None:
                    raise ResourceNotFoundError(task_id=task_id)
                existing_record = (
                    unit_of_work.idempotency_records.get_by_scope(
                        actor_context.actor_id,
                        TOOL_RETRY,
                        idempotency_key,
                    )
                )
                if existing_record is not None:
                    return self._load_reserved_retry(
                        unit_of_work,
                        actor_context,
                        task_id=task_id,
                        record=existing_record,
                        request_digest=request_digest,
                        idempotency_outcome=IdempotencyOutcome.REPLAY,
                    )
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task_id
                )
                runs = unit_of_work.tool_runs.list_for_task(task_id)
                if (
                    task.task_type != "TOOL_EXECUTION"
                    or task.current_status
                    not in {"FAILED", "PARTIALLY_SUCCEEDED"}
                    or not revisions
                    or any(
                        run.current_status in {"PENDING", "RUNNING"}
                        for run in runs
                    )
                ):
                    raise TaskNotRetryableError(task_id=task_id)
                revision = max(revisions, key=lambda item: item.revision)
                if (
                    revision.normalized_input is None
                    or revision.missing_fields
                    or revision.ambiguous_fields
                    or revision.validation_errors
                    or revision.source_llm_call_id is None
                ):
                    raise TaskNotRetryableError(task_id=task_id)
                selected_run = (
                    None
                    if task.selected_tool_run_id is None
                    else next(
                        (
                            item
                            for item in runs
                            if item.tool_run_id
                            == task.selected_tool_run_id
                        ),
                        None,
                    )
                )
                selected_result = (
                    None
                    if task.selected_result_id is None
                    else unit_of_work.tool_results.get(
                        task.selected_result_id
                    )
                )
                failed_without_result = (
                    task.current_status == "FAILED"
                    and task.selected_tool_run_id is not None
                    and task.selected_result_id is None
                    and selected_run is not None
                    and selected_run.task_id == task.task_id
                    and selected_run.current_status == "FAILED"
                    and unit_of_work.tool_results.get_for_tool_run(
                        selected_run.tool_run_id
                    )
                    is None
                )
                failed_before_run = (
                    task.current_status == "FAILED"
                    and task.selected_tool_run_id is None
                    and task.selected_result_id is None
                    and selected_run is None
                    and selected_result is None
                    and not runs
                )
                failed_or_partial_result = (
                    selected_run is not None
                    and selected_result is not None
                    and task.selected_tool_run_id
                    == selected_run.tool_run_id
                    and task.selected_result_id
                    == selected_result.result_id
                    and selected_result.actor_id
                    == actor_context.actor_id
                    and selected_result.task_id == task.task_id
                    and selected_result.tool_run_id
                    == selected_run.tool_run_id
                    and selected_run.task_id == task.task_id
                    and tuple(selected_run.requested_outputs)
                    == tuple(selected_result.requested_outputs)
                    and tuple(selected_run.completed_outputs)
                    == tuple(selected_result.completed_outputs)
                    and tuple(selected_run.failed_outputs)
                    == tuple(selected_result.failed_outputs)
                    and (
                        (
                            task.current_status == "FAILED"
                            and selected_run.current_status == "FAILED"
                            and selected_result.status == "FAILED"
                        )
                        or (
                            task.current_status == "PARTIALLY_SUCCEEDED"
                            and selected_run.current_status
                            == "PARTIALLY_SUCCEEDED"
                            and selected_result.status
                            == "PARTIALLY_SUCCEEDED"
                        )
                    )
                )
                if (
                    not (
                        failed_before_run
                        or failed_without_result
                        or failed_or_partial_result
                    )
                    or (
                        selected_run is not None
                        and selected_run.task_input_revision_id
                        != revision.task_input_revision_id
                    )
                ):
                    raise TaskNotRetryableError(task_id=task_id)
                llm_call = unit_of_work.llm_calls.get(
                    revision.source_llm_call_id
                )
                if llm_call is None or llm_call.status != "SUCCEEDED":
                    raise TaskNotRetryableError(task_id=task_id)
                bound_ref = task.bound_tool_ref
                if bound_ref is None:
                    raise TaskNotRetryableError(task_id=task_id)
                try:
                    registration = self._registry.resolve(bound_ref.tool_id)
                except UnknownToolError:
                    raise ToolExecutionNotAllowedError(task_id=task_id) from None
                authorization = self._authorize_bound_action(
                    registration=registration,
                    action=ToolAction.RETRY,
                    bound_ref=bound_ref,
                    task_id=task_id,
                )
                used_seeds = {
                    parameters["seed"]
                    for item in runs
                    if isinstance(
                        parameters := item.execution_input.get(
                            "runtime_parameters"
                        ),
                        dict,
                    )
                    and isinstance(parameters.get("seed"), int)
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
                    raise TaskNotRetryableError(task_id=task_id) from None
                pending = ToolRun.pending(
                    tool_run_id=tool_run_id,
                    task_id=task_id,
                    request_id=request_id,
                    task_input_revision_id=revision.task_input_revision_id,
                    attempt_no=max(
                        (item.attempt_no for item in runs),
                        default=0,
                    )
                    + 1,
                    tool_id=authorization.authorized_ref.tool_id,
                    tool_version=authorization.authorized_ref.version,
                    schema_hash=authorization.authorized_ref.schema_hash,
                    normalized_input_snapshot=dict(revision.normalized_input),
                    execution_policy_snapshot=(
                        authorization.execution_policy_snapshot
                    ),
                    input_revision_no=revision.revision,
                    execution_input=validated_input.to_json(),
                    requested_outputs=list(validated_input.requested_outputs),
                    created_at=timestamp,
                )
                record = IdempotencyRecord(
                    idempotency_record_id=record_id,
                    actor_id=actor_context.actor_id,
                    operation=TOOL_RETRY,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    first_request_id=request_id,
                    task_id=task_id,
                    message_id=None,
                    task_input_revision_id=None,
                    tool_run_id=tool_run_id,
                    explanation_id=None,
                    created_at=timestamp,
                    expires_at=None,
                )
                running_task = replace(
                    task,
                    current_status="RUNNING",
                    updated_at=timestamp,
                    completed_at=None,
                    error_code=None,
                    safe_error_message=None,
                )
                unit_of_work.tool_runs.add(pending)
                unit_of_work.idempotency_records.add(record)
                if unit_of_work.tasks.update(
                    running_task,
                    expected_status=task.current_status,
                ) is None:
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
                return ReservedToolRetry(
                    tool_run=pending,
                    idempotency_outcome=IdempotencyOutcome.CREATED,
                )
        except (
            ApplicationError,
            PersistenceError,
        ) as error:
            if isinstance(error, PersistenceError):
                replay = self._recover_reserved_retry(
                    actor_context,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    request_id=request_id,
                )
                if replay is not None:
                    return replay
                raise from_persistence_error(error, task_id=task_id) from None
            raise

    def execute_reserved_retry(
        self,
        actor_context: ActorContext,
        *,
        tool_run_id: str,
    ) -> ToolExecutionReceipt | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                pending = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor_context.actor_id,
                )
                task = (
                    None
                    if pending is None
                    else unit_of_work.tasks.get_owned(
                        pending.task_id,
                        actor_context.actor_id,
                    )
                )
                revision = (
                    None
                    if pending is None
                    else unit_of_work.task_input_revisions.get(
                        pending.task_input_revision_id
                    )
                )
                if (
                    pending is None
                    or task is None
                    or revision is None
                    or pending.current_status != "PENDING"
                    or task.current_status != "RUNNING"
                    or revision.task_id != task.task_id
                    or revision.normalized_input is None
                ):
                    raise ApplicationConflictError(
                        task_id=None if pending is None else pending.task_id
                    )
                bound_ref = task.bound_tool_ref
                if bound_ref is None:
                    raise ToolExecutionNotAllowedError(task_id=task.task_id)
                try:
                    registration = self._registry.resolve(pending.tool_id)
                except UnknownToolError:
                    raise ToolExecutionNotAllowedError(
                        task_id=task.task_id
                    ) from None
                authorization = self._authorize_bound_action(
                    registration=registration,
                    action=ToolAction.RETRY,
                    bound_ref=bound_ref,
                    task_id=task.task_id,
                )
                reserved_ref = ToolRef(
                    tool_id=pending.tool_id,
                    version=pending.tool_version,
                    schema_hash=pending.schema_hash,
                )
                if authorization.authorized_ref != reserved_ref:
                    raise ToolExecutionNotAllowedError(task_id=task.task_id)
                parameters = pending.execution_input.get(
                    "runtime_parameters"
                )
                seed = (
                    parameters.get("seed")
                    if isinstance(parameters, dict)
                    else None
                )
                validated_input = registration.tool.validate_input(
                    revision.normalized_input,
                    seed=seed,
                )
                if validated_input.to_json() != pending.execution_input:
                    raise ApplicationConflictError(task_id=task.task_id)
                conversation_id = task.conversation_id
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error) from None
        started = self._start_pending_attempt(
            pending.tool_run_id,
            task_id=pending.task_id,
        )
        if not started.invoke_runtime:
            return None
        running = started.tool_run
        prepared = _PreparedToolAttempt(
            tool_run=running,
            validated_input=validated_input,
            conversation_id=conversation_id,
            initial_chain_activated=True,
        )
        try:
            output = self._invoke_runtime(
                actor_context,
                prepared=prepared,
                request_id=running.request_id,
            )
        except ToolClientError as error:
            self._raise_persisted_runtime_failure(prepared, error=error)
        runtime_error = self._runtime_output_error(
            output,
            initial_chain_activated=True,
        )
        if runtime_error is not None:
            self._raise_persisted_runtime_failure(
                prepared,
                error=runtime_error,
                output=output,
            )
        return self._persist_runtime_success(prepared, output)

    def _recover_reserved_retry(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
    ) -> ReservedToolRetry | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                record = unit_of_work.idempotency_records.get_by_scope(
                    actor_context.actor_id,
                    TOOL_RETRY,
                    idempotency_key,
                )
                if record is None:
                    return None
                return self._load_reserved_retry(
                    unit_of_work,
                    actor_context,
                    task_id=task_id,
                    record=record,
                    request_digest=request_digest,
                    idempotency_outcome=recovered_idempotency_outcome(
                        first_request_id=record.first_request_id,
                        current_request_id=request_id,
                    ),
                )
        except PersistenceError:
            return None

    @staticmethod
    def _load_reserved_retry(
        unit_of_work: object,
        actor_context: ActorContext,
        *,
        task_id: str,
        record: IdempotencyRecord,
        request_digest: str,
        idempotency_outcome: IdempotencyOutcome,
    ) -> ReservedToolRetry:
        if record.request_digest != request_digest:
            raise IdempotencyConflictError(
                task_id=record.task_id or task_id
            )
        tool_run = (
            None
            if record.tool_run_id is None
            else unit_of_work.tool_runs.get_owned(
                record.tool_run_id,
                actor_context.actor_id,
            )
        )
        if (
            record.task_id != task_id
            or tool_run is None
            or tool_run.task_id != task_id
            or tool_run.request_id != record.first_request_id
        ):
            raise ResourceNotFoundError(task_id=task_id)
        return ReservedToolRetry(
            tool_run=tool_run,
            idempotency_outcome=idempotency_outcome,
        )

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
        started = self._start_pending_attempt(
            pending.tool_run_id,
            task_id=task_id,
        )
        if not started.invoke_runtime:
            raise ApplicationConflictError(task_id=task_id)
        running = started.tool_run
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
        pending: ToolRun | None = None
        validated_input: ToolExecutionInput | None = None
        conversation_id: str | None = None
        initial_chain_activated: bool | None = None
        original_ready_task: Task | None = None
        expected_running_task: Task | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor_context.actor_id,
                )
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
                ready_activation = (
                    task.task_type == "TOOL_EXECUTION"
                    and task.current_status == "READY"
                    and task.error_code is None
                    and task.safe_error_message is None
                    and task.selected_tool_run_id is None
                    and task.selected_result_id is None
                )
                if ready_activation:
                    original_ready_task = task
                legacy_activation = (
                    task.task_type == "TOOL_EXECUTION"
                    and task.current_status == "FAILED"
                    and task.error_code == "TOOL_UNAVAILABLE"
                    and task.selected_tool_run_id is None
                    and task.selected_result_id is None
                )
                if not (
                    activated_chain
                    or ready_activation
                    or legacy_activation
                ):
                    raise ApplicationConflictError(task_id=task_id)
                llm_call = unit_of_work.llm_calls.get(
                    revision.source_llm_call_id
                )
                if (
                    llm_call is None
                    or llm_call.task_id != task_id
                    or llm_call.status != "SUCCEEDED"
                ):
                    raise ResourceNotFoundError(task_id=task_id)
                bound_ref = task.bound_tool_ref
                if bound_ref is None:
                    raise ResourceNotFoundError(task_id=task_id)
                existing = unit_of_work.tool_runs.list_for_task(task_id)
                if existing:
                    raise ApplicationConflictError(task_id=task_id)
                try:
                    registration = self._registry.resolve(bound_ref.tool_id)
                except UnknownToolError:
                    raise ToolExecutionNotAllowedError(task_id=task_id) from None
                authorization = self._authorize_bound_action(
                    registration=registration,
                    action=ToolAction.EXECUTE,
                    bound_ref=bound_ref,
                    task_id=task_id,
                )
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
                timestamp = self._clock()
                pending = ToolRun.pending(
                    tool_run_id=tool_run_id,
                    task_id=task_id,
                    request_id=request_id,
                    task_input_revision_id=task_input_revision_id,
                    attempt_no=attempt_no,
                    tool_id=authorization.authorized_ref.tool_id,
                    tool_version=authorization.authorized_ref.version,
                    schema_hash=authorization.authorized_ref.schema_hash,
                    normalized_input_snapshot=dict(revision.normalized_input),
                    execution_policy_snapshot=(
                        authorization.execution_policy_snapshot
                    ),
                    input_revision_no=revision.revision,
                    execution_input=validated_input.to_json(),
                    requested_outputs=list(validated_input.requested_outputs),
                    created_at=timestamp,
                )
                conversation_id = task.conversation_id
                initial_chain_activated = (
                    activated_chain or ready_activation
                )
                unit_of_work.tool_runs.add(pending)
                if ready_activation:
                    expected_running_task = replace(
                        task,
                        current_status="RUNNING",
                        started_at=task.started_at or timestamp,
                        updated_at=timestamp,
                        completed_at=None,
                        error_code=None,
                        safe_error_message=None,
                    )
                    if unit_of_work.tasks.update(
                        expected_running_task,
                        expected_status="READY",
                    ) is None:
                        raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
                return (
                    pending,
                    validated_input,
                    conversation_id,
                    initial_chain_activated,
                )
        except ApplicationError:
            raise
        except PersistenceError as error:
            recovered = self._recover_initial_pending_attempt(
                actor_context,
                task_id=task_id,
                expected_pending=pending,
                validated_input=validated_input,
                conversation_id=conversation_id,
                initial_chain_activated=initial_chain_activated,
                original_ready_task=original_ready_task,
                expected_running_task=expected_running_task,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(error, task_id=task_id) from None
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _authorize_bound_action(
        self,
        *,
        registration: ToolDefinition,
        action: ToolAction,
        bound_ref: ToolRef,
        task_id: str,
    ) -> AuthorizationDecision:
        try:
            return self._registry.authorize(
                registration=registration,
                action=action,
                bound_ref=bound_ref,
            )
        except ToolAuthorizationError as error:
            error_type = (
                ToolSchemaDriftError
                if error.reason is ToolAuthorizationDenialReason.SCHEMA_DRIFT
                else ToolExecutionNotAllowedError
            )
            raise error_type(task_id=task_id) from None

    def _recover_initial_pending_attempt(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        expected_pending: ToolRun | None,
        validated_input: ToolExecutionInput | None,
        conversation_id: str | None,
        initial_chain_activated: bool | None,
        original_ready_task: Task | None,
        expected_running_task: Task | None,
    ) -> tuple[ToolRun, ToolExecutionInput, str, bool] | None:
        if (
            expected_pending is None
            or validated_input is None
            or conversation_id is None
            or initial_chain_activated is None
        ):
            return None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get_owned(
                    expected_pending.tool_run_id,
                    actor_context.actor_id,
                )
                current_task = (
                    None
                    if (
                        original_ready_task is None
                        and expected_running_task is None
                    )
                    else unit_of_work.tasks.get_owned(
                        task_id,
                        actor_context.actor_id,
                    )
                )
        except PersistenceError:
            return None
        if original_ready_task is not None or expected_running_task is not None:
            if (
                original_ready_task is None
                or expected_running_task is None
            ):
                raise ApplicationConflictError(task_id=task_id)
            if current is None and current_task == original_ready_task:
                return None
            if (
                current is not None
                and current.current_status == "PENDING"
                and current == expected_pending
                and current_task == expected_running_task
            ):
                return (
                    current,
                    validated_input,
                    conversation_id,
                    initial_chain_activated,
                )
            raise ApplicationConflictError(task_id=task_id)
        if current is None:
            return None
        if current.current_status != "PENDING" or current != expected_pending:
            raise ApplicationConflictError(task_id=task_id)
        return (
            current,
            validated_input,
            conversation_id,
            initial_chain_activated,
        )

    def _start_pending_attempt(
        self,
        tool_run_id: str,
        *,
        task_id: str,
    ) -> _StartedToolAttempt:
        running: ToolRun | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                pending = unit_of_work.tool_runs.get(tool_run_id)
                if pending is None or pending.task_id != task_id:
                    raise ResourceNotFoundError(task_id=task_id)
                if pending.current_status in {
                    "RUNNING",
                    "SUCCEEDED",
                    "PARTIALLY_SUCCEEDED",
                    "FAILED",
                }:
                    return _StartedToolAttempt(
                        tool_run=pending,
                        invoke_runtime=False,
                    )
                running = pending.start(started_at=self._clock())
                if unit_of_work.tool_runs.update(
                    running,
                    expected_status="PENDING",
                ) is None:
                    raise ResourceNotFoundError(task_id=task_id)
                unit_of_work.commit()
                return _StartedToolAttempt(
                    tool_run=running,
                    invoke_runtime=True,
                )
        except ApplicationError:
            raise
        except PersistenceError as error:
            recovered = self._recover_started_attempt(
                tool_run_id,
                task_id=task_id,
                expected_running=running,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(error, task_id=task_id) from None
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _recover_started_attempt(
        self,
        tool_run_id: str,
        *,
        task_id: str,
        expected_running: ToolRun | None,
    ) -> _StartedToolAttempt | None:
        if expected_running is None:
            return None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get(tool_run_id)
        except PersistenceError:
            return None
        if current is None or not self._same_attempt_identity(
            current,
            expected_running,
        ):
            raise ApplicationConflictError(task_id=task_id)
        if current == expected_running:
            return _StartedToolAttempt(
                tool_run=current,
                invoke_runtime=True,
            )
        if current.current_status in {
            "RUNNING",
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
        }:
            return _StartedToolAttempt(
                tool_run=current,
                invoke_runtime=False,
            )
        return None

    @staticmethod
    def _same_attempt_identity(left: ToolRun, right: ToolRun) -> bool:
        return all(
            getattr(left, field_name) == getattr(right, field_name)
            for field_name in (
                "tool_run_id",
                "task_id",
                "request_id",
                "task_input_revision_id",
                "attempt_no",
                "tool_id",
                "tool_version",
                "schema_hash",
                "normalized_input_snapshot",
                "execution_policy_snapshot",
                "input_revision_no",
                "requested_outputs",
                "execution_input",
                "created_at",
            )
        )

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
        recorded: ToolRun | None = None
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
        except PersistenceError as error:
            recovered = self._recover_runtime_success(
                prepared,
                expected_recorded=recorded,
            )
            if recovered is not None:
                return ToolExecutionReceipt(
                    tool_run=recovered,
                    output=output,
                )
            raise from_persistence_error(error, task_id=task_id) from None
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _recover_runtime_success(
        self,
        prepared: _PreparedToolAttempt,
        *,
        expected_recorded: ToolRun | None,
    ) -> ToolRun | None:
        if expected_recorded is None:
            return None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get(
                    expected_recorded.tool_run_id
                )
        except PersistenceError:
            return None
        if current is None:
            return None
        if current == prepared.tool_run:
            return None
        if (
            current.current_status != "RUNNING"
            or not self._same_attempt_identity(
                current,
                prepared.tool_run,
            )
            or current != expected_recorded
        ):
            raise ApplicationConflictError(
                task_id=prepared.tool_run.task_id
            )
        return current

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
        failed: ToolRun | None = None
        running_before_failure: ToolRun | None = None
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
                running_before_failure = current
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
        except PersistenceError as error:
            recovered = self._recover_runtime_failure(
                tool_run_id,
                task_id=task_id,
                expected_failed=failed,
                expected_running=running_before_failure,
                code=code,
                safe_message=safe_message,
                output=output,
            )
            if recovered:
                return
            raise from_persistence_error(error, task_id=task_id) from None
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _recover_runtime_failure(
        self,
        tool_run_id: str,
        *,
        task_id: str,
        expected_failed: ToolRun | None,
        expected_running: ToolRun | None,
        code: str,
        safe_message: str,
        output: ToolExecutionOutput | None,
    ) -> bool:
        if expected_failed is None:
            return False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.tool_runs.get(tool_run_id)
        except PersistenceError:
            return False
        if current is None:
            return False
        if current == expected_running:
            return False
        if (
            _is_equivalent_failed_fact(
                current,
                code=code,
                safe_message=safe_message,
                output=output,
            )
            and self._same_attempt_identity(
                current,
                expected_failed,
            )
        ):
            return True
        raise ApplicationConflictError(task_id=task_id)


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
