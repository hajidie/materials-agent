from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ConversationBusyError, from_persistence_error
from materialsagent.domain.models.asset import LEGACY_DB_KEY, METADATA_V1
from materialsagent.domain.models.conversation_object_cleanup import (
    COMPLETED,
    PENDING,
    SAFETY_BLOCKED,
    ConversationObjectCleanup,
)
from materialsagent.domain.ports.storage import (
    StorageError,
    StorageIntegrityError,
    StorageService,
    StorageUnavailableError,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError, UnitOfWorkFactory


_ENVIRONMENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
_OBJECT_KEY_PATTERN = re.compile(
    r"assets/(?P<environment>[a-z0-9][a-z0-9-]{0,31})/"
    r"(?P<asset_id>asset_[a-z0-9_-]{1,90})\.png\Z"
)
MAX_CLEANUP_DRAIN = 100
_IDENTITY_METADATA = frozenset({"asset-id", "operation-id", "producer-tool-run-id"})
_logger = logging.getLogger("materialsagent.conversation_cleanup")


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class CleanupDrainSummary:
    attempted: int = 0
    completed: int = 0
    pending: int = 0
    safety_blocked: int = 0


@dataclass(frozen=True, slots=True)
class ConversationDeletion:
    conversation_id: str
    cleanup_ids: tuple[str, ...]


class ConversationCleanupService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        storage: StorageService,
        *,
        environment: str,
        bucket: str,
        storage_namespace: str,
        process_cutoff: datetime,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        if _ENVIRONMENT_PATTERN.fullmatch(environment) is None:
            raise ValueError("environment is unsafe.")
        for value in (bucket, storage_namespace):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("storage identity is required.")
        if process_cutoff.tzinfo is None or process_cutoff.utcoffset() != timezone.utc.utcoffset(process_cutoff):
            raise ValueError("process_cutoff must be UTC.")
        self._unit_of_work_factory = unit_of_work_factory
        self._storage = storage
        self._environment = environment
        self._bucket = bucket
        self._storage_namespace = storage_namespace
        self._process_cutoff = process_cutoff
        self._clock = clock or _utc_now
        self._id_factory = id_factory or _opaque_id

    def recover_stale(self, actor_context: ActorContext, *, conversation_id: str | None = None) -> dict[str, int]:
        recovered_at = self._clock()
        try:
            with self._unit_of_work_factory() as unit_of_work:
                summary = unit_of_work.conversation_lifecycle.recover_stale(
                    actor_id=actor_context.actor_id,
                    process_cutoff=self._process_cutoff,
                    recovered_at=recovered_at,
                    conversation_id=conversation_id,
                )
                unit_of_work.commit()
                return summary
        except PersistenceError as error:
            raise from_persistence_error(error, conversation_id=conversation_id) from None

    def delete(self, actor_context: ActorContext, conversation_id: str) -> ConversationDeletion:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return ConversationDeletion(str(conversation_id), ())
        now = self._clock()
        cleanup_ids: list[str] = []
        try:
            with self._unit_of_work_factory() as unit_of_work:
                if unit_of_work.actors.get_for_update(actor_context.actor_id) is None:
                    return ConversationDeletion(conversation_id, ())
                conversation = unit_of_work.conversations.get_owned_for_update(
                    conversation_id,
                    actor_context.actor_id,
                )
                if conversation is None:
                    return ConversationDeletion(conversation_id, ())
                unit_of_work.conversation_lifecycle.recover_stale(
                    actor_id=actor_context.actor_id,
                    process_cutoff=self._process_cutoff,
                    recovered_at=now,
                    conversation_id=conversation_id,
                )
                if unit_of_work.conversation_lifecycle.current_activity_exists(
                    actor_id=actor_context.actor_id,
                    conversation_id=conversation_id,
                    process_cutoff=self._process_cutoff,
                ):
                    raise ConversationBusyError(conversation_id=conversation_id)
                assets = unit_of_work.conversation_lifecycle.list_assets_for_delete(
                    actor_id=actor_context.actor_id,
                    conversation_id=conversation_id,
                )
                for asset in assets:
                    cleanup = ConversationObjectCleanup(
                        cleanup_id=self._id_factory("cleanup"),
                        actor_id=actor_context.actor_id,
                        conversation_id=conversation_id,
                        asset_id=asset.asset_id,
                        operation_id=asset.operation_id,
                        producer_tool_run_id=str(asset.producer_tool_run_id),
                        object_key=asset.object_key,
                        bucket=asset.storage_bucket or self._bucket,
                        storage_namespace=(
                            asset.storage_namespace or self._storage_namespace
                        ),
                        identity_version=asset.storage_identity_version,
                        status=PENDING,
                        attempts=0,
                        created_at=now,
                        last_attempt_at=None,
                        completed_at=None,
                        safety_error_code=None,
                    )
                    unit_of_work.conversation_object_cleanups.add(cleanup)
                    cleanup_ids.append(cleanup.cleanup_id)
                if not unit_of_work.conversations.delete(
                    conversation_id,
                    actor_context.actor_id,
                ):
                    return ConversationDeletion(conversation_id, ())
                unit_of_work.commit()
        except ConversationBusyError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, conversation_id=conversation_id) from None
        return ConversationDeletion(conversation_id, tuple(cleanup_ids))

    def drain(
        self,
        *,
        limit: int = MAX_CLEANUP_DRAIN,
        preferred_ids: tuple[str, ...] = (),
    ) -> CleanupDrainSummary:
        bounded_limit = max(0, min(int(limit), MAX_CLEANUP_DRAIN))
        if bounded_limit == 0:
            return CleanupDrainSummary()
        try:
            with self._unit_of_work_factory() as unit_of_work:
                pending = unit_of_work.conversation_object_cleanups.list_pending(
                    limit=bounded_limit,
                    preferred_ids=preferred_ids,
                )
        except PersistenceError:
            _logger.warning("conversation_cleanup_batch status=database_unavailable")
            return CleanupDrainSummary(pending=1)

        completed = blocked = still_pending = 0
        for cleanup in pending:
            outcome = self._attempt(cleanup)
            completed += outcome == COMPLETED
            blocked += outcome == SAFETY_BLOCKED
            still_pending += outcome == PENDING
        summary = CleanupDrainSummary(
            attempted=len(pending),
            completed=completed,
            pending=still_pending,
            safety_blocked=blocked,
        )
        _logger.info(
            "conversation_cleanup_batch attempted=%d completed=%d pending=%d safety_blocked=%d",
            summary.attempted,
            summary.completed,
            summary.pending,
            summary.safety_blocked,
        )
        return summary

    def _attempt(self, cleanup: ConversationObjectCleanup) -> str:
        now = self._clock()
        key_match = _OBJECT_KEY_PATTERN.fullmatch(cleanup.object_key)
        if (
            key_match is None
            or key_match.group("asset_id") != cleanup.asset_id
        ):
            return self._persist_outcome(
                cleanup.block(attempted_at=now, error_code="OBJECT_KEY_IDENTITY_MISMATCH")
            )
        if cleanup.bucket != self._bucket or cleanup.storage_namespace != self._storage_namespace:
            return self._persist_outcome(
                cleanup.pending_after(attempted_at=now, error_code="STORAGE_NAMESPACE_UNAVAILABLE")
            )
        try:
            stored = self._storage.head(cleanup.object_key)
        except StorageIntegrityError:
            return self._persist_outcome(
                cleanup.block(attempted_at=now, error_code="OBJECT_METADATA_UNREADABLE")
            )
        except (StorageUnavailableError, StorageError):
            return self._persist_outcome(
                cleanup.pending_after(attempted_at=now, error_code="STORAGE_UNAVAILABLE")
            )
        if stored is None:
            return self._persist_outcome(cleanup.complete(completed_at=now))

        actual = {key: stored.metadata.get(key) for key in _IDENTITY_METADATA}
        expected = {
            "asset-id": cleanup.asset_id,
            "operation-id": cleanup.operation_id,
            "producer-tool-run-id": cleanup.producer_tool_run_id,
        }
        present = {key for key, value in actual.items() if value is not None}
        metadata_matches = present == _IDENTITY_METADATA and actual == expected
        legacy_without_identity = cleanup.identity_version == LEGACY_DB_KEY and not present
        if cleanup.identity_version == METADATA_V1 and not metadata_matches:
            return self._persist_outcome(
                cleanup.block(attempted_at=now, error_code="OBJECT_METADATA_IDENTITY_MISMATCH")
            )
        if cleanup.identity_version == LEGACY_DB_KEY and not (metadata_matches or legacy_without_identity):
            return self._persist_outcome(
                cleanup.block(attempted_at=now, error_code="LEGACY_METADATA_IDENTITY_MISMATCH")
            )
        try:
            self._storage.delete(cleanup.object_key)
        except StorageError:
            return self._persist_outcome(
                cleanup.pending_after(attempted_at=now, error_code="STORAGE_UNAVAILABLE")
            )
        return self._persist_outcome(cleanup.complete(completed_at=now))

    def _persist_outcome(self, cleanup: ConversationObjectCleanup) -> str:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                updated = unit_of_work.conversation_object_cleanups.update(
                    cleanup,
                    expected_status=PENDING,
                )
                if updated is None:
                    current = unit_of_work.conversation_object_cleanups.get(cleanup.cleanup_id)
                    return PENDING if current is None else current.status
                unit_of_work.commit()
                return updated.status
        except PersistenceError:
            return PENDING
