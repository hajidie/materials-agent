from __future__ import annotations

from collections.abc import Callable

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.ports.unit_of_work import (
    PersistenceConflictError,
    UnitOfWork,
)
from materialsagent.infrastructure.config import AppSettings, ConfigurationError


UnitOfWorkFactory = Callable[[], UnitOfWork]


def ensure_local_actor(
    settings: AppSettings,
    unit_of_work_factory: UnitOfWorkFactory,
) -> Actor:
    actor_id = settings.local_actor_id
    if actor_id is None or not actor_id.strip():
        raise ConfigurationError("LOCAL_ACTOR_ID is required.")

    try:
        with unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.actors.get(actor_id)
            if existing is not None:
                return existing

            actor = Actor.local_anonymous(actor_id)
            unit_of_work.actors.add(actor)
            unit_of_work.commit()
            return actor
    except PersistenceConflictError:
        with unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.actors.get(actor_id)
            if existing is not None:
                return existing
        raise PersistenceConflictError("Persistence conflict.") from None
