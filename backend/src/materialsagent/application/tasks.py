from __future__ import annotations

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.domain.models.task import Task
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


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
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor_context.actor_id,
                )
        except PersistenceError as error:
            raise from_persistence_error(error) from None
        if task is None:
            raise ResourceNotFoundError()
        return task
