from __future__ import annotations

from dataclasses import dataclass

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.explanation_service import (
    ExplanationService,
    PreparedExplanation,
    select_explanation_attempts,
)
from materialsagent.application.idempotency import (
    EXPLANATION_RETRY,
    TOOL_RETRY,
    canonical_request_digest,
    validate_idempotency_key,
)
from materialsagent.application.result_service import (
    ToolResultProjection,
    ToolResultQueryService,
)
from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tool_workflow import ToolWorkflowService
from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


DEFAULT_RETRY_REASON = "USER_REQUESTED_RETRY"


@dataclass(frozen=True, slots=True)
class ToolRetryProjection:
    task: Task
    tool_run: ToolRun
    result: ToolResult | None
    result_projection: ToolResultProjection | None
    explanation: NaturalLanguageExplanation | None
    explanation_call: LLMCall | None
    idempotency_replayed: bool


class ToolRetryService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        tool_execution_service: ToolExecutionService,
        tool_workflow_service: ToolWorkflowService,
        result_query_service: ToolResultQueryService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._tool_execution_service = tool_execution_service
        self._tool_workflow_service = tool_workflow_service
        self._result_query_service = result_query_service

    def retry(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        request_id: str,
        idempotency_key: str,
        reason: str = DEFAULT_RETRY_REASON,
    ) -> ToolRetryProjection:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 256
        ):
            raise ApplicationValidationError(task_id=task_id)
        try:
            key = validate_idempotency_key(idempotency_key)
        except ValueError:
            raise ApplicationValidationError(task_id=task_id) from None
        normalized_reason = reason.strip()
        digest = canonical_request_digest(
            {
                "task_id": task_id,
                "operation": TOOL_RETRY,
                "reason": normalized_reason,
            }
        )
        reservation = self._tool_execution_service.reserve_retry_attempt(
            actor,
            task_id=task_id,
            request_id=request_id,
            idempotency_key=key,
            request_digest=digest,
        )
        if not reservation.idempotency_replayed:
            self._tool_workflow_service.execute_reserved_retry(
                actor,
                tool_run_id=reservation.tool_run.tool_run_id,
            )
        return self._load_current(
            actor,
            task_id=task_id,
            tool_run_id=reservation.tool_run.tool_run_id,
            idempotency_replayed=reservation.idempotency_replayed,
        )

    def _load_current(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        idempotency_replayed: bool,
    ) -> ToolRetryProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor.actor_id,
                )
                result = unit_of_work.tool_results.get_for_tool_run(
                    tool_run_id
                )
                explanations = (
                    []
                    if result is None
                    else unit_of_work.explanations.list_for_result(
                        result.result_id
                    )
                )
                explanation = select_explanation_attempts(
                    explanations
                ).primary
                call = (
                    None
                    if explanation is None
                    else unit_of_work.llm_calls.get(
                        explanation.llm_call_id
                    )
                )
                if (
                    task is None
                    or tool_run is None
                    or tool_run.task_id != task.task_id
                    or (
                        result is not None
                        and (
                            result.task_id != task.task_id
                            or result.actor_id != actor.actor_id
                        )
                    )
                ):
                    raise ResourceNotFoundError(task_id=task_id)
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None
        result_projection = (
            None
            if result is None
            else self._result_query_service.get(actor, result.result_id)
        )
        return ToolRetryProjection(
            task=task,
            tool_run=tool_run,
            result=result,
            result_projection=result_projection,
            explanation=explanation,
            explanation_call=call,
            idempotency_replayed=idempotency_replayed,
        )


@dataclass(frozen=True, slots=True)
class ExplanationRetryProjection:
    task: Task
    result: ToolResult
    explanation: NaturalLanguageExplanation
    explanation_call: LLMCall
    idempotency_replayed: bool


class ExplanationRetryService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        explanation_service: ExplanationService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._explanation_service = explanation_service

    def retry(
        self,
        actor: ActorContext,
        *,
        result_id: str,
        request_id: str,
        idempotency_key: str,
        language: str = "zh-CN",
        reason: str = DEFAULT_RETRY_REASON,
    ) -> ExplanationRetryProjection:
        if (
            not isinstance(result_id, str)
            or not result_id.strip()
            or not isinstance(request_id, str)
            or not request_id.strip()
            or not isinstance(language, str)
            or not language.strip()
            or len(language) > 32
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 256
        ):
            raise ApplicationValidationError()
        try:
            key = validate_idempotency_key(idempotency_key)
        except ValueError:
            raise ApplicationValidationError() from None
        digest = canonical_request_digest(
            {
                "result_id": result_id,
                "operation": EXPLANATION_RETRY,
                "language": language.strip(),
                "reason": reason.strip(),
            }
        )
        reservation = self._explanation_service.reserve_retry_attempt(
            actor,
            result_id=result_id,
            request_id=request_id,
            idempotency_key=key,
            request_digest=digest,
            language=language.strip(),
        )
        if not reservation.idempotency_replayed:
            self._explanation_service.execute_reserved_retry(
                actor,
                prepared=reservation.prepared,
            )
        return self._load_current(
            actor,
            prepared=reservation.prepared,
            idempotency_replayed=reservation.idempotency_replayed,
        )

    def _load_current(
        self,
        actor: ActorContext,
        *,
        prepared: PreparedExplanation,
        idempotency_replayed: bool,
    ) -> ExplanationRetryProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                result = unit_of_work.tool_results.get_owned(
                    prepared.result_id,
                    actor.actor_id,
                )
                task = unit_of_work.tasks.get_owned(
                    prepared.task_id,
                    actor.actor_id,
                )
                explanation = unit_of_work.explanations.get(
                    prepared.explanation_id
                )
                call = unit_of_work.llm_calls.get(prepared.llm_call_id)
                if (
                    result is None
                    or task is None
                    or explanation is None
                    or call is None
                    or result.task_id != task.task_id
                    or explanation.result_id != result.result_id
                    or explanation.task_id != task.task_id
                    or explanation.llm_call_id != call.llm_call_id
                    or call.request_id != prepared.request_id
                ):
                    raise ResourceNotFoundError(task_id=prepared.task_id)
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                task_id=prepared.task_id,
            ) from None
        return ExplanationRetryProjection(
            task=task,
            result=result,
            explanation=explanation,
            explanation_call=call,
            idempotency_replayed=idempotency_replayed,
        )
