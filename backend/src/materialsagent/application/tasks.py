from __future__ import annotations

from dataclasses import dataclass

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class TaskProjection:
    task: Task
    selected_result: ToolResult | None


class TaskQueryService:
    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

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
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor_context.actor_id,
                )
                selected_result = (
                    unit_of_work.tool_results.get_owned(
                        task.selected_result_id,
                        actor_context.actor_id,
                    )
                    if task is not None
                    and task.selected_result_id is not None
                    else None
                )
        except PersistenceError as error:
            raise from_persistence_error(error) from None
        if task is None:
            raise ResourceNotFoundError()
        if task.selected_result_id is not None and (
            selected_result is None
            or selected_result.task_id != task.task_id
            or selected_result.tool_run_id != task.selected_tool_run_id
        ):
            raise ApplicationValidationError()
        return TaskProjection(
            task=task,
            selected_result=selected_result,
        )
