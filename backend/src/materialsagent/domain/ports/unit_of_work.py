from __future__ import annotations

from typing import Protocol, Self

from materialsagent.domain.models.actor import Actor


class PersistenceError(RuntimeError):
    """Safe persistence failure without driver or SQL details."""


class PersistenceConflictError(PersistenceError):
    """A stable persistence uniqueness or concurrency conflict."""


class DatabaseUnavailableError(PersistenceError):
    """The configured database could not be reached or authenticated."""


class ActorRepository(Protocol):
    def get(self, actor_id: str) -> Actor | None: ...

    def add(self, actor: Actor) -> None: ...


class UnitOfWork(Protocol):
    actors: ActorRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
