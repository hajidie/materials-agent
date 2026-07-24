from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from materialsagent.application.asset_service import AssetService
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.explanation_service import (
    ExplanationService,
    select_explanation_attempts,
)
from materialsagent.application.result_service import (
    ResultArtifactProjection,
    ResultPersistenceError,
    ResultService,
)
from materialsagent.application.tool_execution import (
    ToolExecutionOutcomeError,
    ToolExecutionService,
)
from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class ToolWorkflowProjection:
    task: Task
    tool_run: ToolRun
    result: ToolResult
    artifacts: tuple[ResultArtifactProjection, ...]
    explanation: NaturalLanguageExplanation
    explanation_call: LLMCall
    latest_failed_explanation: NaturalLanguageExplanation | None
    latest_failed_explanation_call: LLMCall | None


class ToolWorkflowService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        tool_execution_service: ToolExecutionService,
        asset_service: AssetService,
        result_service: ResultService,
        explanation_service: ExplanationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._tool_execution_service = tool_execution_service
        self._asset_service = asset_service
        self._result_service = result_service
        self._explanation_service = explanation_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> ToolWorkflowProjection:
        try:
            receipt = (
                self._tool_execution_service.execute_revision_with_output(
                    actor,
                    task_id=task_id,
                    task_input_revision_id=task_input_revision_id,
                    request_id=request_id,
                )
            )
        except ToolExecutionOutcomeError as error:
            self._select_failed_execution(
                actor,
                task_id=task_id,
                tool_run_id=error.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise
        try:
            assets = (
                self._asset_service.create_from_output(
                    actor,
                    task_id=task_id,
                    tool_run_id=receipt.tool_run.tool_run_id,
                    output=receipt.output,
                )
                if receipt.output.images
                else []
            )
        except ApplicationError as error:
            self._terminalize_asset_failure(
                actor,
                task_id=task_id,
                tool_run_id=receipt.tool_run.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise

        try:
            result = self._result_service.commit_initial_result(
                actor,
                receipt=receipt,
                assets=assets,
            )
        except ResultPersistenceError as error:
            self._terminalize_result_failure(
                actor,
                task_id=task_id,
                tool_run_id=receipt.tool_run.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise
        self._explanation_service.explain(
            actor,
            result_id=result.result_id,
        )
        return self._load_committed(
            actor,
            task_id=task_id,
            result_id=result.result_id,
            require_initial_attempt=True,
        )

    def execute_reserved_retry(
        self,
        actor: ActorContext,
        *,
        tool_run_id: str,
    ) -> ToolWorkflowProjection | None:
        try:
            receipt = self._tool_execution_service.execute_reserved_retry(
                actor,
                tool_run_id=tool_run_id,
            )
        except ToolExecutionOutcomeError as error:
            self._select_failed_execution(
                actor,
                task_id=error.task_id,
                tool_run_id=error.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise
        if receipt is None:
            return None
        task_id = receipt.tool_run.task_id
        try:
            assets = (
                self._asset_service.create_from_output(
                    actor,
                    task_id=task_id,
                    tool_run_id=receipt.tool_run.tool_run_id,
                    output=receipt.output,
                )
                if receipt.output.images
                else []
            )
        except ApplicationError as error:
            self._terminalize_asset_failure(
                actor,
                task_id=task_id,
                tool_run_id=receipt.tool_run.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise
        try:
            result = self._result_service.commit_retry_result(
                actor,
                receipt=receipt,
                assets=assets,
            )
        except ResultPersistenceError as error:
            self._terminalize_result_failure(
                actor,
                task_id=task_id,
                tool_run_id=receipt.tool_run.tool_run_id,
                code=error.code,
                safe_message=str(error),
            )
            raise
        self._explanation_service.explain(
            actor,
            result_id=result.result_id,
        )
        return self._load_committed(
            actor,
            task_id=task_id,
            result_id=result.result_id,
            require_initial_attempt=True,
        )

    def load_current_for_task(
        self,
        actor: ActorContext,
        *,
        task_id: str,
    ) -> ToolWorkflowProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor.actor_id,
                )
                if task is None or task.selected_result_id is None:
                    raise ResourceNotFoundError(task_id=task_id)
                result_id = task.selected_result_id
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None
        return self._load_committed(
            actor,
            task_id=task_id,
            result_id=result_id,
            require_initial_attempt=False,
        )

    def _terminalize_result_failure(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned_for_update(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.task_type != "TOOL_EXECUTION"
                    or tool_run.task_id != task.task_id
                ):
                    raise ApplicationConflictError(task_id=task_id)

                existing_result = (
                    unit_of_work.tool_results.get_for_tool_run(
                        tool_run.tool_run_id
                    )
                )
                if existing_result is not None:
                    expected_task_status = (
                        "RUNNING"
                        if existing_result.status == "SUCCEEDED"
                        else existing_result.status
                    )
                    result_error_code = (
                        None
                        if existing_result.error is None
                        else str(existing_result.error["code"])
                    )
                    if (
                        existing_result.actor_id != actor.actor_id
                        or existing_result.task_id != task.task_id
                        or existing_result.tool_run_id
                        != tool_run.tool_run_id
                        or tool_run.current_status
                        != existing_result.status
                        or tuple(tool_run.requested_outputs)
                        != existing_result.requested_outputs
                        or tuple(tool_run.completed_outputs)
                        != existing_result.completed_outputs
                        or tuple(tool_run.failed_outputs)
                        != existing_result.failed_outputs
                        or tool_run.error_code != result_error_code
                        or task.current_status != expected_task_status
                        or task.selected_tool_run_id
                        != tool_run.tool_run_id
                        or task.selected_result_id
                        != existing_result.result_id
                        or task.error_code != result_error_code
                    ):
                        raise ApplicationConflictError(task_id=task_id)
                    return

                if (
                    task.current_status != "RUNNING"
                    or tool_run.current_status != "RUNNING"
                ):
                    raise ApplicationConflictError(task_id=task_id)

                completed_at = self._clock()
                if (
                    completed_at.tzinfo is None
                    or completed_at.utcoffset() is None
                    or completed_at.utcoffset()
                    != timezone.utc.utcoffset(completed_at)
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_run = tool_run.complete_from_result(
                    completed_outputs=[],
                    failed_outputs=list(tool_run.requested_outputs),
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tool_runs.update(
                        failed_run,
                        expected_status="RUNNING",
                    )
                    is None
                    or unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise ResultPersistenceError(task_id=task_id) from error

    def _terminalize_asset_failure(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned_for_update(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.current_status != "RUNNING"
                    or tool_run.current_status != "RUNNING"
                    or tool_run.task_id != task.task_id
                ):
                    raise ApplicationConflictError(task_id=task_id)
                completed_at = self._clock()
                if (
                    completed_at.tzinfo is None
                    or completed_at.utcoffset() is None
                    or completed_at.utcoffset()
                    != timezone.utc.utcoffset(completed_at)
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_run = tool_run.complete_from_result(
                    completed_outputs=[],
                    failed_outputs=list(tool_run.requested_outputs),
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tool_runs.update(
                        failed_run,
                        expected_status="RUNNING",
                    )
                    is None
                    or unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _select_failed_execution(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.current_status != "RUNNING"
                    or tool_run.current_status != "FAILED"
                    or tool_run.task_id != task.task_id
                    or tool_run.completed_at is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=tool_run.completed_at,
                    completed_at=tool_run.completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _load_committed(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        result_id: str,
        require_initial_attempt: bool,
    ) -> ToolWorkflowProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor.actor_id,
                )
                result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                conversation = (
                    None
                    if task is None
                    else unit_of_work.conversations.get_owned(
                        task.conversation_id,
                        actor.actor_id,
                    )
                )
                if (
                    task is None
                    or result is None
                    or conversation is None
                    or result.task_id != task.task_id
                    or task.selected_result_id != result.result_id
                    or task.selected_tool_run_id != result.tool_run_id
                    or task.current_status
                    not in {"SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"}
                ):
                    raise ResourceNotFoundError(task_id=task_id)
                tool_run = unit_of_work.tool_runs.get_owned(
                    result.tool_run_id,
                    actor.actor_id,
                )
                explanations = unit_of_work.explanations.list_for_result(
                    result.result_id
                )
                initial_explanation = next(
                    (
                        item
                        for item in explanations
                        if item.attempt_no == 1
                    ),
                    None,
                )
                if (
                    tool_run is None
                    or tool_run.task_id != task.task_id
                    or tool_run.current_status != result.status
                    or (
                        require_initial_attempt
                        and (
                            initial_explanation is None
                            or initial_explanation.status
                            not in {"SUCCEEDED", "FAILED"}
                        )
                    )
                ):
                    raise ApplicationConflictError(task_id=task_id)
                eligible_explanations = [
                    item
                    for item in explanations
                    if (
                        item.task_id == task.task_id
                        and item.result_id == result.result_id
                        and item.language == "zh-CN"
                    )
                ]
                explanation_selection = select_explanation_attempts(
                    eligible_explanations
                )
                explanation = explanation_selection.primary
                latest_failed_explanation = (
                    explanation_selection.latest_failed
                )
                if explanation is None:
                    raise ApplicationConflictError(task_id=task_id)
                explanation_call = unit_of_work.llm_calls.get(
                    explanation.llm_call_id
                )
                if (
                    explanation_call is None
                    or explanation_call.task_id != task.task_id
                    or explanation_call.conversation_id
                    != conversation.conversation_id
                    or explanation_call.purpose
                    != "TOOL_RESULT_EXPLANATION"
                    or explanation_call.input_result_id != result.result_id
                    or explanation_call.status != explanation.status
                ):
                    raise ApplicationConflictError(task_id=task_id)
                if (
                    require_initial_attempt
                    and explanation.attempt_no == 1
                    and explanation_call.request_id != tool_run.request_id
                ):
                    raise ApplicationConflictError(task_id=task_id)
                latest_failed_explanation_call = (
                    None
                    if latest_failed_explanation is None
                    else unit_of_work.llm_calls.get(
                        latest_failed_explanation.llm_call_id
                    )
                )
                if latest_failed_explanation is not None and (
                    latest_failed_explanation_call is None
                    or latest_failed_explanation_call.task_id != task.task_id
                    or latest_failed_explanation_call.conversation_id
                    != conversation.conversation_id
                    or latest_failed_explanation_call.purpose
                    != "TOOL_RESULT_EXPLANATION"
                    or latest_failed_explanation_call.input_result_id
                    != result.result_id
                    or latest_failed_explanation_call.status != "FAILED"
                ):
                    raise ApplicationConflictError(task_id=task_id)
                artifacts: list[ResultArtifactProjection] = []
                for link in unit_of_work.result_asset_links.list_for_result(
                    result.result_id
                ):
                    asset = unit_of_work.assets.get_owned(
                        link.asset_id,
                        actor.actor_id,
                    )
                    if (
                        asset is None
                        or asset.current_status != "AVAILABLE"
                        or asset.task_id != task.task_id
                        or asset.producer_tool_run_id != tool_run.tool_run_id
                        or asset.media_type is None
                        or asset.width is None
                        or asset.height is None
                        or asset.bit_depth is None
                        or asset.size_bytes is None
                        or asset.sha256 is None
                    ):
                        raise ApplicationConflictError(task_id=task_id)
                    artifacts.append(
                        ResultArtifactProjection(
                            asset_id=asset.asset_id,
                            status=asset.current_status,
                            role=asset.role,
                            media_type=asset.media_type,
                            width=asset.width,
                            height=asset.height,
                            bit_depth=asset.bit_depth,
                            size_bytes=asset.size_bytes,
                            sha256=asset.sha256,
                            content_url=(
                                f"/api/v1/assets/{asset.asset_id}/content"
                            ),
                        )
                    )
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None
        return ToolWorkflowProjection(
            task=task,
            tool_run=tool_run,
            result=result,
            artifacts=tuple(artifacts),
            explanation=explanation,
            explanation_call=explanation_call,
            latest_failed_explanation=latest_failed_explanation,
            latest_failed_explanation_call=(
                latest_failed_explanation_call
            ),
        )
