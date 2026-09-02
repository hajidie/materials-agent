from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationInternalError,
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.explanation_service import (
    select_explanation_attempts,
)
from materialsagent.application.result_service import (
    ResultArtifactProjection,
)
from materialsagent.application.timeline import (
    NeedsInputSummary,
    PublicExplanationSummary,
    PublicResultSummary,
    PublicToolRunSummary,
    _needs_input,
    _public_explanation,
    _public_result,
    project_tool_run,
)
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.ports.timeline_query import TaskDetailQueryPort
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
)


@dataclass(frozen=True, slots=True)
class TaskProjection:
    task: Task
    anchor_at: datetime
    needs_input: NeedsInputSummary | None
    tool_run_count: int
    tool_runs: tuple[PublicToolRunSummary, ...]
    selected_result: ToolResult | None
    selected_result_summary: PublicResultSummary | None
    assets: tuple[ResultArtifactProjection, ...]
    explanation_summary: PublicExplanationSummary | None
    latest_explanation_failure: PublicExplanationSummary | None


class TaskQueryService:
    def __init__(self, query: TaskDetailQueryPort) -> None:
        self._query = query

    def get(
        self,
        actor_context: ActorContext,
        task_id: str,
    ) -> Task:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ApplicationValidationError()
        return self.get_projection(actor_context, task_id).task

    def get_projection(
        self,
        actor_context: ActorContext,
        task_id: str,
    ) -> TaskProjection:
        try:
            snapshot = self._query.fetch_owned_task_detail(
                actor_id=actor_context.actor_id,
                task_id=task_id,
            )
        except PersistenceError as error:
            raise from_persistence_error(error) from None
        if snapshot is None:
            raise ResourceNotFoundError(task_id=task_id)
        task = snapshot.task
        messages = list(snapshot.messages)
        revisions = list(snapshot.revisions)
        runs = list(snapshot.tool_runs)
        selected_result = snapshot.selected_result
        links = list(snapshot.result_asset_links)
        assets = list(snapshot.assets)
        explanations = list(snapshot.explanations)
        llm_calls = list(snapshot.llm_calls)
        try:
            if task.actor_id != actor_context.actor_id or any(
                message.actor_id != actor_context.actor_id
                or message.conversation_id != task.conversation_id
                or message.task_id != task.task_id
                for message in messages
            ) or any(
                revision.task_id != task.task_id
                for revision in revisions
            ) or any(
                run.task_id != task.task_id for run in runs
            ):
                raise ApplicationInternalError(task_id=task.task_id)
            ordered_messages = sorted(
                messages,
                key=lambda item: (
                    item.created_at,
                    item.message_id,
                ),
            )
            initial = next(
                (
                    message
                    for message in ordered_messages
                    if message.role == "USER"
                ),
                None,
            )
            anchor_at = (
                task.created_at
                if initial is None
                else initial.created_at
            )
            ordered_runs = sorted(
                runs,
                key=lambda item: (
                    item.created_at,
                    item.attempt_no,
                    item.tool_run_id,
                ),
            )
            selected_run = next(
                (
                    run
                    for run in ordered_runs
                    if run.tool_run_id == task.selected_tool_run_id
                ),
                None,
            )
            if (
                (task.selected_tool_run_id is not None)
                != (selected_run is not None)
                or (task.selected_result_id is not None)
                != (selected_result is not None)
            ):
                raise ApplicationInternalError(task_id=task.task_id)
            if selected_result is not None and (
                selected_run is None
                or selected_result.actor_id != task.actor_id
                or selected_result.task_id != task.task_id
                or selected_result.tool_run_id
                != selected_run.tool_run_id
                or selected_result.status != selected_run.current_status
                or tuple(selected_result.requested_outputs)
                != tuple(selected_run.requested_outputs)
                or tuple(selected_result.completed_outputs)
                != tuple(selected_run.completed_outputs)
                or tuple(selected_result.failed_outputs)
                != tuple(selected_run.failed_outputs)
                or selected_result.tool_id != selected_run.tool_id
                or selected_result.tool_version
                != selected_run.tool_version
                or selected_result.schema_hash
                != selected_run.schema_hash
            ):
                raise ApplicationInternalError(task_id=task.task_id)
            if len(assets) != len(links):
                raise ApplicationInternalError(task_id=task.task_id)
            artifact_projections: list[ResultArtifactProjection] = []
            for link, asset in sorted(
                zip(links, assets, strict=True),
                key=lambda pair: (
                    pair[0].artifact_order,
                    pair[0].asset_id,
                ),
            ):
                if (
                    selected_result is None
                    or selected_run is None
                    or link.result_id != selected_result.result_id
                    or asset is None
                    or asset.asset_id != link.asset_id
                    or asset.actor_id != task.actor_id
                    or asset.task_id != task.task_id
                    or asset.producer_tool_run_id
                    != selected_run.tool_run_id
                    or asset.current_status != "AVAILABLE"
                    or asset.media_type is None
                    or asset.width is None
                    or asset.height is None
                    or asset.bit_depth is None
                    or asset.size_bytes is None
                    or asset.sha256 is None
                ):
                    raise ApplicationInternalError(task_id=task.task_id)
                artifact_projections.append(
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
            if len(llm_calls) != len(explanations):
                raise ApplicationInternalError(task_id=task.task_id)
            for explanation, call in zip(
                explanations,
                llm_calls,
                strict=True,
            ):
                if (
                    selected_result is None
                    or explanation.task_id != task.task_id
                    or explanation.result_id
                    != selected_result.result_id
                    or call is None
                    or call.llm_call_id != explanation.llm_call_id
                    or call.task_id != task.task_id
                    or call.conversation_id != task.conversation_id
                    or call.purpose != "TOOL_RESULT_EXPLANATION"
                    or call.input_result_id != selected_result.result_id
                ):
                    raise ApplicationInternalError(task_id=task.task_id)
            selection = select_explanation_attempts(explanations)
        except ApplicationInternalError:
            raise
        except Exception:
            raise ApplicationInternalError(task_id=task.task_id) from None
        return TaskProjection(
            task=task,
            anchor_at=anchor_at,
            needs_input=_needs_input(task, revisions),
            tool_run_count=len(ordered_runs),
            tool_runs=tuple(
                project_tool_run(
                    run,
                    is_selected=(
                        run.tool_run_id == task.selected_tool_run_id
                    ),
                )
                for run in ordered_runs
            ),
            selected_result=selected_result,
            selected_result_summary=(
                None
                if selected_result is None
                else _public_result(selected_result)
            ),
            assets=tuple(artifact_projections),
            explanation_summary=_public_explanation(
                selection.primary
            ),
            latest_explanation_failure=_public_explanation(
                selection.latest_failed
            ),
        )
