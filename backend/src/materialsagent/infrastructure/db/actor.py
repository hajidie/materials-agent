from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.ports.unit_of_work import (
    DatabaseUnavailableError,
    PersistenceConflictError,
    PersistenceError,
)
from materialsagent.infrastructure.db.base import Base


class ActorRow(Base):
    __tablename__ = "actor"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(actor_id)) > 0",
            name="ck_actor_actor_id_not_blank",
        ),
        CheckConstraint(
            "actor_origin = 'LOCAL_ANONYMOUS'",
            name="ck_actor_actor_origin_local_anonymous",
        ),
    )

    actor_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


def _raise_safe_persistence_error(error: SQLAlchemyError) -> None:
    if isinstance(error, IntegrityError):
        raise PersistenceConflictError("Persistence conflict.") from None
    if isinstance(error, DBAPIError):
        raise DatabaseUnavailableError("Database unavailable.") from None
    raise PersistenceError("Persistence operation failed.") from None


class SQLAlchemyActorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, actor_id: str) -> Actor | None:
        try:
            row = self._session.get(ActorRow, actor_id)
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)

        if row is None:
            return None
        return Actor(
            actor_id=row.actor_id,
            user_id=row.user_id,
            actor_origin=row.actor_origin,
            created_at=row.created_at,
            linked_at=row.linked_at,
        )

    def add(self, actor: Actor) -> None:
        try:
            self._session.add(
                ActorRow(
                    actor_id=actor.actor_id,
                    user_id=actor.user_id,
                    actor_origin=actor.actor_origin,
                    created_at=actor.created_at,
                    linked_at=actor.linked_at,
                )
            )
        except SQLAlchemyError as error:
            _raise_safe_persistence_error(error)
