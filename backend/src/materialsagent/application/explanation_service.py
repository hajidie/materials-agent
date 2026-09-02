from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    ApplicationValidationError,
    ResourceNotFoundError,
    ExplanationNotRetryableError,
    IdempotencyConflictError,
)
from materialsagent.application.idempotency import (
    EXPLANATION_RETRY,
    IdempotencyOutcome,
    recovered_idempotency_outcome,
)
from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.explanation import (
    ExplanationAssetReference,
    ExplanationInput,
    ExplanationOutcome,
    ExplanationPort,
    ExplanationProtocolError,
    ExplanationProviderUnavailableError,
    ExplanationTimeoutError,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWork,
    UnitOfWorkFactory,
)


class ExplanationPersistenceError(ApplicationInternalError):
    default_message = "Explanation outcome could not be persisted."
    default_code = "EXPLANATION_PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class PreparedExplanation:
    explanation_id: str
    llm_call_id: str
    task_id: str
    result_id: str
    attempt_no: int
    request_id: str
    projection: ExplanationInput


@dataclass(frozen=True, slots=True)
class ReservedExplanationRetry:
    prepared: PreparedExplanation
    idempotency_outcome: IdempotencyOutcome

    @property
    def idempotency_replayed(self) -> bool:
        return self.idempotency_outcome.replayed


@dataclass(frozen=True, slots=True)
class ExplanationAttemptSelection:
    primary: NaturalLanguageExplanation | None
    latest_failed: NaturalLanguageExplanation | None


@dataclass(frozen=True, slots=True)
class _InitialExplanationSources:
    result: ToolResult
    task: Task
    conversation_id: str
    tool_run: ToolRun
    projection: ExplanationInput


@dataclass(frozen=True, slots=True)
class _TerminalExplanationFacts:
    status: str
    call: LLMCall
    explanation: NaturalLanguageExplanation


_INITIAL_EXPLANATION_ATTEMPT = 1


def select_explanation_attempts(
    explanations: list[NaturalLanguageExplanation],
) -> ExplanationAttemptSelection:
    terminal = [
        item
        for item in explanations
        if item.status in {"SUCCEEDED", "FAILED"}
        and item.completed_at is not None
    ]
    successful = [item for item in terminal if item.status == "SUCCEEDED"]
    primary = (
        max(
            successful,
            key=lambda item: (
                item.completed_at,
                item.explanation_id,
            ),
        )
        if successful
        else (
            max(
                terminal,
                key=lambda item: (
                    item.attempt_no,
                    item.explanation_id,
                ),
            )
            if terminal
            else None
        )
    )
    latest_attempt = (
        max(
            explanations,
            key=lambda item: (
                item.attempt_no,
                item.explanation_id,
            ),
        )
        if explanations
        else None
    )
    latest_failed = (
        latest_attempt
        if (
            latest_attempt is not None
            and latest_attempt.status == "FAILED"
            and latest_attempt.completed_at is not None
        )
        else None
    )
    return ExplanationAttemptSelection(
        primary=primary,
        latest_failed=latest_failed,
    )


class ExplanationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        port: ExplanationPort,
        *,
        clock: Callable[[], datetime] | None = None,
        explanation_id_factory: Callable[[], str] | None = None,
        llm_call_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._port = port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._explanation_id_factory = (
            explanation_id_factory or (lambda: str(uuid4()))
        )
        self._llm_call_id_factory = (
            llm_call_id_factory or (lambda: str(uuid4()))
        )

    @staticmethod
    def _projection(
        unit_of_work: UnitOfWork,
        result: ToolResult,
    ) -> ExplanationInput:
        links = unit_of_work.result_asset_links.list_for_result(
            result.result_id
        )
        artifacts: list[ExplanationAssetReference] = []
        for link in links:
            asset = unit_of_work.assets.get(link.asset_id)
            if (
                asset is None
                or asset.current_status != "AVAILABLE"
                or asset.actor_id != result.actor_id
                or asset.task_id != result.task_id
                or asset.producer_tool_run_id != result.tool_run_id
            ):
                raise ApplicationConflictError(task_id=result.task_id)
            artifacts.append(
                ExplanationAssetReference(
                    asset_id=asset.asset_id,
                    role=asset.role,
                    asset_type=asset.asset_type,
                )
            )
        process_parameters = result.provenance.get(
            "normalized_process_parameters",
            {},
        )
        if not isinstance(process_parameters, Mapping):
            raise ApplicationConflictError(task_id=result.task_id)
        return ExplanationInput(
            result_id=result.result_id,
            status=result.status,
            requested_outputs=tuple(result.requested_outputs),
            completed_outputs=tuple(result.completed_outputs),
            failed_outputs=tuple(result.failed_outputs),
            data=result.data,
            artifacts=tuple(artifacts),
            warnings=tuple(result.warnings),
            error=result.error,
            process_parameters=process_parameters,
            tool_id=result.tool_id,
            tool_version=result.tool_version,
            schema_hash=result.schema_hash,
        )

    @staticmethod
    def _load_and_validate_attempt_sources(
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        prepared: PreparedExplanation,
        *,
        lock_task: bool,
    ) -> tuple[
        ToolResult,
        Task,
        ToolRun,
        LLMCall,
        NaturalLanguageExplanation,
    ]:
        result = unit_of_work.tool_results.get_owned(
            prepared.result_id,
            actor.actor_id,
        )
        task = (
            unit_of_work.tasks.get_owned_for_update(
                prepared.task_id,
                actor.actor_id,
            )
            if lock_task
            else unit_of_work.tasks.get_owned(
                prepared.task_id,
                actor.actor_id,
            )
        )
        conversation = (
            None
            if task is None
            else unit_of_work.conversations.get_owned(
                task.conversation_id,
                actor.actor_id,
            )
        )
        tool_run = (
            None
            if result is None
            else unit_of_work.tool_runs.get_owned(
                result.tool_run_id,
                actor.actor_id,
            )
        )
        call = unit_of_work.llm_calls.get(prepared.llm_call_id)
        explanation = unit_of_work.explanations.get(
            prepared.explanation_id
        )
        if (
            result is None
            or task is None
            or conversation is None
            or tool_run is None
            or call is None
            or explanation is None
            or result.result_id != prepared.result_id
            or result.task_id != prepared.task_id
            or task.task_id != prepared.task_id
            or task.selected_result_id != result.result_id
            or task.selected_tool_run_id != result.tool_run_id
            or tool_run.task_id != task.task_id
            or call.task_id != task.task_id
            or call.conversation_id != conversation.conversation_id
            or call.request_id != prepared.request_id
            or call.purpose != "TOOL_RESULT_EXPLANATION"
            or call.input_result_id != result.result_id
            or explanation.task_id != task.task_id
            or explanation.result_id != result.result_id
            or explanation.llm_call_id != call.llm_call_id
            or explanation.attempt_no != prepared.attempt_no
        ):
            raise ApplicationConflictError(task_id=prepared.task_id)
        return result, task, tool_run, call, explanation

    @staticmethod
    def _expected_task_status(
        result: ToolResult,
        outcome: ExplanationOutcome,
    ) -> str:
        if result.status != "SUCCEEDED":
            return result.status
        return (
            "SUCCEEDED"
            if outcome.text is not None
            else "PARTIALLY_SUCCEEDED"
        )

    def _read_equivalent_terminal(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
        outcome: ExplanationOutcome,
    ) -> NaturalLanguageExplanation:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    result,
                    task,
                    _,
                    call,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                if (
                    not self._equivalent(call, explanation, outcome)
                    or task.current_status
                    != self._expected_task_status(result, outcome)
                    or task.completed_at is None
                ):
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                return explanation
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error

    def _recover_terminal_commit(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
        outcome: ExplanationOutcome,
    ) -> NaturalLanguageExplanation | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    result,
                    task,
                    _,
                    call,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if (
            self._equivalent(call, explanation, outcome)
            and task.current_status
            == self._expected_task_status(result, outcome)
            and task.completed_at is not None
        ):
            return explanation
        if call.status == explanation.status == "RUNNING":
            return None
        raise ApplicationConflictError(task_id=prepared.task_id)

    def prepare(
        self,
        actor: ActorContext,
        *,
        result_id: str,
    ) -> PreparedExplanation:
        return self._prepare_initial_attempt(actor, result_id=result_id)

    def reserve_retry_attempt(
        self,
        actor: ActorContext,
        *,
        result_id: str,
        request_id: str,
        idempotency_key: str,
        request_digest: str,
        language: str,
    ) -> ReservedExplanationRetry:
        explanation_id = self._explanation_id_factory()
        llm_call_id = self._llm_call_id_factory()
        created_at = self._clock()
        record_id = f"idem_{uuid4().hex}"
        task_id: str | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                existing = unit_of_work.idempotency_records.get_by_scope(
                    actor.actor_id,
                    EXPLANATION_RETRY,
                    idempotency_key,
                )
                if existing is not None:
                    return self._load_reserved_retry(
                        unit_of_work,
                        actor,
                        result_id=result_id,
                        record=existing,
                        request_digest=request_digest,
                        idempotency_outcome=IdempotencyOutcome.REPLAY,
                    )
                if language != "zh-CN":
                    raise ApplicationValidationError()
                located_result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                if located_result is None:
                    raise ResourceNotFoundError()
                task_id = located_result.task_id
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                if task is None:
                    raise ResourceNotFoundError(task_id=task_id)
                existing = unit_of_work.idempotency_records.get_by_scope(
                    actor.actor_id,
                    EXPLANATION_RETRY,
                    idempotency_key,
                )
                if existing is not None:
                    return self._load_reserved_retry(
                        unit_of_work,
                        actor,
                        result_id=result_id,
                        record=existing,
                        request_digest=request_digest,
                        idempotency_outcome=IdempotencyOutcome.REPLAY,
                    )
                result = unit_of_work.tool_results.get_owned_for_update(
                    result_id,
                    actor.actor_id,
                )
                tool_run = (
                    None
                    if result is None
                    else unit_of_work.tool_runs.get_owned(
                        result.tool_run_id,
                        actor.actor_id,
                    )
                )
                conversation = unit_of_work.conversations.get_owned(
                    task.conversation_id,
                    actor.actor_id,
                )
                attempts = unit_of_work.explanations.list_for_result(
                    result_id
                )
                if (
                    result is None
                    or tool_run is None
                    or conversation is None
                    or task.selected_result_id != result.result_id
                    or task.selected_tool_run_id != result.tool_run_id
                    or tool_run.task_id != task.task_id
                    or task.current_status
                    not in {"PARTIALLY_SUCCEEDED", "FAILED"}
                    or any(
                        item.status in {"PENDING", "RUNNING"}
                        for item in attempts
                    )
                    or any(
                        item.status == "SUCCEEDED"
                        and item.language == language
                        for item in attempts
                    )
                    or not any(item.status == "FAILED" for item in attempts)
                ):
                    raise ExplanationNotRetryableError(task_id=task_id)
                projection = self._projection(unit_of_work, result)
                metadata = self._port.request_metadata(projection)
                attempt_no = max(item.attempt_no for item in attempts) + 1
                call = LLMCall(
                    llm_call_id=llm_call_id,
                    task_id=task.task_id,
                    conversation_id=conversation.conversation_id,
                    request_id=request_id,
                    purpose="TOOL_RESULT_EXPLANATION",
                    input_result_id=result.result_id,
                    provider=metadata.provider,
                    model_name=metadata.model_name,
                    prompt_template_id=metadata.prompt_template_id,
                    prompt_template_version=metadata.prompt_template_version,
                    prompt_digest=metadata.prompt_digest,
                    generation_parameters=metadata.generation_parameters,
                    structured_output_summary=None,
                    usage=None,
                    provider_request_id=None,
                    status="PENDING",
                    created_at=created_at,
                    started_at=None,
                    completed_at=None,
                    duration_ms=None,
                    error_code=None,
                    safe_error_message=None,
                )
                explanation = NaturalLanguageExplanation(
                    explanation_id=explanation_id,
                    task_id=task.task_id,
                    result_id=result.result_id,
                    llm_call_id=llm_call_id,
                    attempt_no=attempt_no,
                    status="PENDING",
                    language=language,
                    text=None,
                    created_at=created_at,
                    started_at=None,
                    completed_at=None,
                    duration_ms=None,
                    error_code=None,
                    safe_error_message=None,
                )
                record = IdempotencyRecord(
                    idempotency_record_id=record_id,
                    actor_id=actor.actor_id,
                    operation=EXPLANATION_RETRY,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    first_request_id=request_id,
                    task_id=task.task_id,
                    message_id=None,
                    task_input_revision_id=None,
                    tool_run_id=None,
                    explanation_id=explanation_id,
                    created_at=created_at,
                    expires_at=None,
                )
                running_task = replace(
                    task,
                    current_status="RUNNING",
                    updated_at=created_at,
                    completed_at=None,
                    error_code=None,
                    safe_error_message=None,
                )
                unit_of_work.llm_calls.add(call)
                unit_of_work.explanations.add(explanation)
                unit_of_work.idempotency_records.add(record)
                if unit_of_work.tasks.update(
                    running_task,
                    expected_status=task.current_status,
                ) is None:
                    raise ApplicationConflictError(task_id=task.task_id)
                unit_of_work.commit()
                return ReservedExplanationRetry(
                    prepared=PreparedExplanation(
                        explanation_id=explanation_id,
                        llm_call_id=llm_call_id,
                        task_id=task.task_id,
                        result_id=result_id,
                        attempt_no=attempt_no,
                        request_id=request_id,
                        projection=projection,
                    ),
                    idempotency_outcome=IdempotencyOutcome.CREATED,
                )
        except PersistenceError as error:
            replay = self._recover_reserved_retry(
                actor,
                result_id=result_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                request_id=request_id,
            )
            if replay is not None:
                return replay
            raise ExplanationPersistenceError(task_id=task_id) from error

    def execute_reserved_retry(
        self,
        actor: ActorContext,
        *,
        prepared: PreparedExplanation,
    ) -> NaturalLanguageExplanation:
        if not self._start(actor, prepared):
            return self._load_current_attempt(actor, prepared)
        outcome = self._invoke_provider_safely(prepared.projection)
        return self.finalize(actor, prepared=prepared, outcome=outcome)

    def _recover_reserved_retry(
        self,
        actor: ActorContext,
        *,
        result_id: str,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
    ) -> ReservedExplanationRetry | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                record = unit_of_work.idempotency_records.get_by_scope(
                    actor.actor_id,
                    EXPLANATION_RETRY,
                    idempotency_key,
                )
                if record is None:
                    return None
                return self._load_reserved_retry(
                    unit_of_work,
                    actor,
                    result_id=result_id,
                    record=record,
                    request_digest=request_digest,
                    idempotency_outcome=recovered_idempotency_outcome(
                        first_request_id=record.first_request_id,
                        current_request_id=request_id,
                    ),
                )
        except PersistenceError:
            return None

    def _load_reserved_retry(
        self,
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        *,
        result_id: str,
        record: IdempotencyRecord,
        request_digest: str,
        idempotency_outcome: IdempotencyOutcome,
    ) -> ReservedExplanationRetry:
        if record.request_digest != request_digest:
            raise IdempotencyConflictError(task_id=record.task_id)
        explanation = (
            None
            if record.explanation_id is None
            else unit_of_work.explanations.get(record.explanation_id)
        )
        call = (
            None
            if explanation is None
            else unit_of_work.llm_calls.get(explanation.llm_call_id)
        )
        result = unit_of_work.tool_results.get_owned(
            result_id,
            actor.actor_id,
        )
        if (
            explanation is None
            or call is None
            or result is None
            or record.task_id != result.task_id
            or explanation.result_id != result_id
            or explanation.task_id != result.task_id
            or call.llm_call_id != explanation.llm_call_id
            or call.request_id != record.first_request_id
            or call.input_result_id != result_id
        ):
            raise ResourceNotFoundError(task_id=record.task_id)
        return ReservedExplanationRetry(
            prepared=PreparedExplanation(
                explanation_id=explanation.explanation_id,
                llm_call_id=call.llm_call_id,
                task_id=result.task_id,
                result_id=result_id,
                attempt_no=explanation.attempt_no,
                request_id=call.request_id,
                projection=self._projection(unit_of_work, result),
            ),
            idempotency_outcome=idempotency_outcome,
        )

    def _prepare_initial_attempt(
        self,
        actor: ActorContext,
        *,
        result_id: str,
    ) -> PreparedExplanation:
        explanation_id = self._explanation_id_factory()
        llm_call_id = self._llm_call_id_factory()
        created_at = self._clock()
        attempt_no = _INITIAL_EXPLANATION_ATTEMPT
        task_id: str | None = None
        prepared: PreparedExplanation | None = None
        expected_call: LLMCall | None = None
        expected_explanation: NaturalLanguageExplanation | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                sources = self._load_initial_prepare_sources(
                    unit_of_work,
                    actor,
                    result_id=result_id,
                    attempt_no=attempt_no,
                )
                task_id = sources.task.task_id
                call, explanation = self._build_initial_pending_facts(
                    sources,
                    explanation_id=explanation_id,
                    llm_call_id=llm_call_id,
                    attempt_no=attempt_no,
                    created_at=created_at,
                )
                expected_call = call
                expected_explanation = explanation
                prepared = PreparedExplanation(
                    explanation_id=explanation_id,
                    llm_call_id=llm_call_id,
                    task_id=sources.task.task_id,
                    result_id=result_id,
                    attempt_no=attempt_no,
                    request_id=sources.tool_run.request_id,
                    projection=sources.projection,
                )
                unit_of_work.llm_calls.add(call)
                unit_of_work.explanations.add(explanation)
                unit_of_work.commit()
        except PersistenceError as error:
            if (
                prepared is not None
                and expected_call is not None
                and expected_explanation is not None
            ):
                recovered = self._recover_initial_prepare(
                    actor,
                    prepared,
                    expected_call=expected_call,
                    expected_explanation=expected_explanation,
                )
                if recovered is not None:
                    return recovered
            raise ExplanationPersistenceError(
                task_id=task_id
            ) from error

        try:
            with self._unit_of_work_factory() as unit_of_work:
                persisted_result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                if persisted_result is None:
                    raise ExplanationPersistenceError()
                projection = self._projection(
                    unit_of_work,
                    persisted_result,
                )
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=task_id
            ) from error
        if prepared is None:
            raise ExplanationPersistenceError(task_id=task_id)
        return PreparedExplanation(
            explanation_id=prepared.explanation_id,
            llm_call_id=prepared.llm_call_id,
            task_id=persisted_result.task_id,
            result_id=prepared.result_id,
            attempt_no=prepared.attempt_no,
            request_id=prepared.request_id,
            projection=projection,
        )

    def _recover_initial_prepare(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
        *,
        expected_call: LLMCall,
        expected_explanation: NaturalLanguageExplanation,
    ) -> PreparedExplanation | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                call = unit_of_work.llm_calls.get(prepared.llm_call_id)
                explanation = unit_of_work.explanations.get(
                    prepared.explanation_id
                )
                if call is None and explanation is None:
                    return None
                if call is None or explanation is None:
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                (
                    result,
                    _,
                    _,
                    current_call,
                    current_explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                projection = self._projection(unit_of_work, result)
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if (
            current_call.status != "PENDING"
            or current_explanation.status != "PENDING"
            or current_call != expected_call
            or current_explanation != expected_explanation
            or projection != prepared.projection
        ):
            raise ApplicationConflictError(task_id=prepared.task_id)
        return prepared

    def _load_initial_prepare_sources(
        self,
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        *,
        result_id: str,
        attempt_no: int,
    ) -> _InitialExplanationSources:
        result = unit_of_work.tool_results.get_owned(
            result_id,
            actor.actor_id,
        )
        if result is None:
            raise ResourceNotFoundError()
        task = unit_of_work.tasks.get_owned(
            result.task_id,
            actor.actor_id,
        )
        if (
            task is None
            or task.selected_result_id != result.result_id
            or task.selected_tool_run_id != result.tool_run_id
        ):
            raise ApplicationConflictError(task_id=result.task_id)
        conversation = unit_of_work.conversations.get_owned(
            task.conversation_id,
            actor.actor_id,
        )
        tool_run = unit_of_work.tool_runs.get_owned(
            result.tool_run_id,
            actor.actor_id,
        )
        if (
            conversation is None
            or tool_run is None
            or tool_run.task_id != result.task_id
            or unit_of_work.explanations.get_for_result_attempt(
                result.result_id,
                attempt_no,
            )
            is not None
        ):
            raise ApplicationConflictError(task_id=result.task_id)
        return _InitialExplanationSources(
            result=result,
            task=task,
            conversation_id=conversation.conversation_id,
            tool_run=tool_run,
            projection=self._projection(unit_of_work, result),
        )

    def _build_initial_pending_facts(
        self,
        sources: _InitialExplanationSources,
        *,
        explanation_id: str,
        llm_call_id: str,
        attempt_no: int,
        created_at: datetime,
    ) -> tuple[LLMCall, NaturalLanguageExplanation]:
        metadata = self._port.request_metadata(sources.projection)
        call = LLMCall(
            llm_call_id=llm_call_id,
            task_id=sources.task.task_id,
            conversation_id=sources.conversation_id,
            request_id=sources.tool_run.request_id,
            purpose="TOOL_RESULT_EXPLANATION",
            input_result_id=sources.result.result_id,
            provider=metadata.provider,
            model_name=metadata.model_name,
            prompt_template_id=metadata.prompt_template_id,
            prompt_template_version=metadata.prompt_template_version,
            prompt_digest=metadata.prompt_digest,
            generation_parameters=metadata.generation_parameters,
            structured_output_summary=None,
            usage=None,
            provider_request_id=None,
            status="PENDING",
            created_at=created_at,
            started_at=None,
            completed_at=None,
            duration_ms=None,
            error_code=None,
            safe_error_message=None,
        )
        explanation = NaturalLanguageExplanation(
            explanation_id=explanation_id,
            task_id=sources.result.task_id,
            result_id=sources.result.result_id,
            llm_call_id=call.llm_call_id,
            attempt_no=attempt_no,
            status="PENDING",
            language="zh-CN",
            text=None,
            created_at=created_at,
            started_at=None,
            completed_at=None,
            duration_ms=None,
            error_code=None,
            safe_error_message=None,
        )
        return call, explanation

    def _start(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
    ) -> bool:
        started_at = self._clock()
        needs_fresh_read = False
        running_call: LLMCall | None = None
        running_explanation: NaturalLanguageExplanation | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    _,
                    _,
                    _,
                    call,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                if call.status == "RUNNING" and explanation.status == "RUNNING":
                    return False
                if (
                    call.status != "PENDING"
                    or explanation.status != "PENDING"
                ):
                    if (
                        call.status == explanation.status
                        and call.status in {"SUCCEEDED", "FAILED"}
                    ):
                        return False
                    raise ApplicationConflictError(task_id=prepared.task_id)
                running_call = replace(
                    call,
                    status="RUNNING",
                    started_at=started_at,
                )
                running_explanation = replace(
                    explanation,
                    status="RUNNING",
                    started_at=started_at,
                )
                if (
                    unit_of_work.llm_calls.update(
                        running_call,
                        expected_status="PENDING",
                    )
                    is None
                    or unit_of_work.explanations.update(
                        running_explanation,
                        expected_status="PENDING",
                    )
                    is None
                ):
                    unit_of_work.rollback()
                    needs_fresh_read = True
                else:
                    unit_of_work.commit()
                    return True
        except PersistenceError as error:
            recovered = self._recover_started_attempt(
                actor,
                prepared,
                expected_call=running_call,
                expected_explanation=running_explanation,
            )
            if recovered is not None:
                return recovered
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if needs_fresh_read:
            try:
                with self._unit_of_work_factory() as unit_of_work:
                    (
                        _,
                        _,
                        _,
                        call,
                        explanation,
                    ) = self._load_and_validate_attempt_sources(
                        unit_of_work,
                        actor,
                        prepared,
                        lock_task=False,
                    )
                    if (
                        call.status == explanation.status
                        and call.status
                        in {"RUNNING", "SUCCEEDED", "FAILED"}
                    ):
                        return False
            except PersistenceError as error:
                raise ExplanationPersistenceError(
                    task_id=prepared.task_id
                ) from error
            raise ApplicationConflictError(task_id=prepared.task_id)
        raise ApplicationConflictError(task_id=prepared.task_id)

    def _recover_started_attempt(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
        *,
        expected_call: LLMCall | None,
        expected_explanation: NaturalLanguageExplanation | None,
    ) -> bool | None:
        if expected_call is None or expected_explanation is None:
            return None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    _,
                    _,
                    _,
                    call,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
        except PersistenceError:
            return None
        if call == expected_call and explanation == expected_explanation:
            return True
        if (
            call.status == explanation.status
            and call.status in {"RUNNING", "SUCCEEDED", "FAILED"}
            and self._same_attempt_identity(
                call,
                explanation,
                prepared,
            )
        ):
            return False
        if call.status == explanation.status == "PENDING":
            return None
        raise ApplicationConflictError(task_id=prepared.task_id)

    @staticmethod
    def _same_attempt_identity(
        call: LLMCall,
        explanation: NaturalLanguageExplanation,
        prepared: PreparedExplanation,
    ) -> bool:
        return (
            call.llm_call_id == prepared.llm_call_id
            and call.task_id == prepared.task_id
            and call.request_id == prepared.request_id
            and call.purpose == "TOOL_RESULT_EXPLANATION"
            and call.input_result_id == prepared.result_id
            and explanation.explanation_id == prepared.explanation_id
            and explanation.llm_call_id == prepared.llm_call_id
            and explanation.task_id == prepared.task_id
            and explanation.result_id == prepared.result_id
            and explanation.attempt_no == prepared.attempt_no
        )

    def _load_current_attempt(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
    ) -> NaturalLanguageExplanation:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    _,
                    _,
                    _,
                    _,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                return explanation
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error

    @staticmethod
    def _equivalent(
        call: LLMCall,
        explanation: NaturalLanguageExplanation,
        outcome: ExplanationOutcome,
    ) -> bool:
        expected_status = (
            "SUCCEEDED" if outcome.text is not None else "FAILED"
        )
        expected_call_error_code = (
            outcome.llm_error_code or outcome.error_code
        )
        expected_call_safe_error_message = (
            outcome.llm_safe_error_message or outcome.safe_error_message
        )
        return (
            call.status == expected_status
            and explanation.status == expected_status
            and explanation.text == outcome.text
            and explanation.error_code == outcome.error_code
            and explanation.safe_error_message
            == outcome.safe_error_message
            and call.error_code == expected_call_error_code
            and call.safe_error_message
            == expected_call_safe_error_message
            and call.provider_request_id == outcome.provider_request_id
            and call.usage == outcome.usage
        )

    def finalize(
        self,
        actor: ActorContext,
        *,
        prepared: PreparedExplanation,
        outcome: ExplanationOutcome,
    ) -> NaturalLanguageExplanation:
        self._start(actor, prepared)
        completed_at = self._clock()
        needs_fresh_read = False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                (
                    result,
                    task,
                    _,
                    call,
                    explanation,
                ) = self._load_and_validate_attempt_sources(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=True,
                )
                if call.status in {"SUCCEEDED", "FAILED"} or explanation.status in {
                    "SUCCEEDED",
                    "FAILED",
                }:
                    if (
                        self._equivalent(call, explanation, outcome)
                        and task.current_status
                        == self._expected_task_status(result, outcome)
                        and task.completed_at is not None
                    ):
                        return explanation
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                if (
                    call.status != "RUNNING"
                    or explanation.status != "RUNNING"
                    or call.started_at is None
                    or explanation.started_at is None
                ):
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )

                terminal_facts = self._build_terminal_explanation_facts(
                    call,
                    explanation,
                    outcome=outcome,
                    completed_at=completed_at,
                )
                if not self._persist_terminal_explanation(
                    unit_of_work,
                    terminal_facts,
                ):
                    needs_fresh_read = True
                elif result.status == "SUCCEEDED":
                    terminal_task = self._build_terminal_task(
                        task,
                        outcome=outcome,
                        completed_at=completed_at,
                    )
                    if (
                        unit_of_work.tasks.update(
                            terminal_task,
                            expected_status="RUNNING",
                        )
                        is None
                    ):
                        unit_of_work.rollback()
                        needs_fresh_read = True
                elif result.status in {
                    "PARTIALLY_SUCCEEDED",
                    "FAILED",
                }:
                    if task.current_status == "RUNNING":
                        result_error = result.error or {}
                        terminal_task = replace(
                            task,
                            current_status=result.status,
                            updated_at=completed_at,
                            completed_at=completed_at,
                            error_code=(
                                str(result_error["code"])
                                if "code" in result_error
                                else None
                            ),
                            safe_error_message=(
                                str(result_error["safe_message"])
                                if "safe_message" in result_error
                                else None
                            ),
                        )
                        if unit_of_work.tasks.update(
                            terminal_task,
                            expected_status="RUNNING",
                        ) is None:
                            unit_of_work.rollback()
                            needs_fresh_read = True
                    elif task.current_status != result.status:
                        raise ApplicationConflictError(
                            task_id=prepared.task_id
                        )
                if not needs_fresh_read:
                    unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_terminal_commit(
                actor,
                prepared,
                outcome,
            )
            if recovered is not None:
                return recovered
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if needs_fresh_read:
            return self._read_equivalent_terminal(
                actor,
                prepared,
                outcome,
            )

        return self._read_equivalent_terminal(
            actor,
            prepared,
            outcome,
        )

    @staticmethod
    def _build_terminal_explanation_facts(
        call: LLMCall,
        explanation: NaturalLanguageExplanation,
        *,
        outcome: ExplanationOutcome,
        completed_at: datetime,
    ) -> _TerminalExplanationFacts:
        status = "SUCCEEDED" if outcome.text is not None else "FAILED"
        call_duration = max(
            0,
            int((completed_at - call.started_at).total_seconds() * 1000),
        )
        explanation_duration = max(
            0,
            int(
                (completed_at - explanation.started_at).total_seconds()
                * 1000
            ),
        )
        call_error_code = outcome.llm_error_code or outcome.error_code
        call_safe_error_message = (
            outcome.llm_safe_error_message or outcome.safe_error_message
        )
        return _TerminalExplanationFacts(
            status=status,
            call=replace(
                call,
                usage=outcome.usage,
                provider_request_id=outcome.provider_request_id,
                status=status,
                completed_at=completed_at,
                duration_ms=call_duration,
                error_code=call_error_code,
                safe_error_message=call_safe_error_message,
            ),
            explanation=replace(
                explanation,
                status=status,
                text=outcome.text,
                completed_at=completed_at,
                duration_ms=explanation_duration,
                error_code=outcome.error_code,
                safe_error_message=outcome.safe_error_message,
            ),
        )

    @staticmethod
    def _persist_terminal_explanation(
        unit_of_work: UnitOfWork,
        terminal_facts: _TerminalExplanationFacts,
    ) -> bool:
        if (
            unit_of_work.llm_calls.update(
                terminal_facts.call,
                expected_status="RUNNING",
            )
            is None
            or unit_of_work.explanations.update(
                terminal_facts.explanation,
                expected_status="RUNNING",
            )
            is None
        ):
            unit_of_work.rollback()
            return False
        return True

    @staticmethod
    def _build_terminal_task(
        task: Task,
        *,
        outcome: ExplanationOutcome,
        completed_at: datetime,
    ) -> Task:
        return replace(
            task,
            current_status=(
                "SUCCEEDED"
                if outcome.text is not None
                else "PARTIALLY_SUCCEEDED"
            ),
            updated_at=completed_at,
            completed_at=completed_at,
            error_code=outcome.error_code,
            safe_error_message=outcome.safe_error_message,
        )

    def _invoke_provider_safely(
        self,
        projection: ExplanationInput,
    ) -> ExplanationOutcome:
        try:
            outcome = self._port.explain(projection)
            if not isinstance(outcome, ExplanationOutcome):
                raise ExplanationProtocolError()
            return outcome
        except ExplanationTimeoutError:
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_TIMEOUT",
                safe_error_message="Explanation generation timed out.",
            )
        except ExplanationProviderUnavailableError:
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_PROVIDER_UNAVAILABLE",
                safe_error_message="Explanation provider is unavailable.",
            )
        except (ExplanationProtocolError, ValueError, TypeError):
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_PROTOCOL_ERROR",
                safe_error_message=(
                    "Explanation provider returned an invalid response."
                ),
            )
        except Exception:
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_FAILED",
                safe_error_message="Explanation generation failed.",
            )

    def explain(
        self,
        actor: ActorContext,
        *,
        result_id: str,
    ) -> NaturalLanguageExplanation:
        prepared = self.prepare(actor, result_id=result_id)
        if not self._start(actor, prepared):
            return self._load_current_attempt(actor, prepared)
        outcome = self._invoke_provider_safely(prepared.projection)
        return self.finalize(actor, prepared=prepared, outcome=outcome)
