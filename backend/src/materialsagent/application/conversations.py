from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationInternalError,
    ApplicationValidationError,
    IdempotencyConflictError,
    InvalidCursorError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.idempotency import (
    IdempotencyOutcome,
    canonical_request_digest,
    recovered_idempotency_outcome,
    validate_idempotency_key,
)
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.idempotency_record import (
    CONVERSATION_CREATE,
    IdempotencyRecord,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)


CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 2048
MAX_CURSOR_JSON_BYTES = 512


@dataclass(frozen=True, slots=True)
class ConversationCursor:
    version: int
    updated_at: datetime
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ConversationListItem:
    conversation_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    last_activity_preview: str | None


@dataclass(frozen=True, slots=True)
class ConversationListPage:
    items: tuple[ConversationListItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationCreation:
    conversation: Conversation
    idempotency_outcome: IdempotencyOutcome

    @property
    def idempotency_replayed(self) -> bool:
        return self.idempotency_outcome.replayed


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def generate_conversation_title(content_text: str) -> str:
    normalized = _collapse_whitespace(content_text)
    if not normalized:
        return "新对话"
    if len(normalized) <= 40:
        return normalized
    return normalized[:40] + "…"


def generate_last_activity_preview(content_text: str) -> str:
    normalized = _collapse_whitespace(content_text)
    if len(normalized) <= 80:
        return normalized
    return normalized[:80] + "…"


def _is_utc(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset() == timezone.utc.utcoffset(value)
    )


def _utc_text(value: datetime) -> str:
    if not _is_utc(value):
        raise InvalidCursorError()
    return value.isoformat().replace("+00:00", "Z")


def encode_conversation_cursor(
    updated_at: datetime,
    conversation_id: str,
) -> str:
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise InvalidCursorError()
    payload = {
        "version": CURSOR_VERSION,
        "updated_at": _utc_text(updated_at),
        "conversation_id": conversation_id,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_CURSOR_JSON_BYTES:
        raise InvalidCursorError()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_conversation_cursor(cursor: str) -> ConversationCursor:
    try:
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > MAX_CURSOR_LENGTH
        ):
            raise ValueError
        padded = cursor.encode("ascii") + b"=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if not raw or len(raw) > MAX_CURSOR_JSON_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "updated_at",
            "conversation_id",
        }:
            raise ValueError
        version = payload["version"]
        updated_at_text = payload["updated_at"]
        conversation_id = payload["conversation_id"]
        if type(version) is not int or version != CURSOR_VERSION:
            raise ValueError
        if not isinstance(updated_at_text, str):
            raise ValueError
        if (
            not isinstance(conversation_id, str)
            or not conversation_id.strip()
            or len(conversation_id) > MAX_CURSOR_JSON_BYTES
        ):
            raise ValueError
        updated_at = datetime.fromisoformat(
            updated_at_text.replace("Z", "+00:00")
        )
        if not _is_utc(updated_at):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise InvalidCursorError() from None

    return ConversationCursor(
        version=CURSOR_VERSION,
        updated_at=updated_at,
        conversation_id=conversation_id,
    )


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _validated_utc_now(clock: Clock) -> datetime:
    timestamp = clock()
    if not isinstance(timestamp, datetime) or not _is_utc(timestamp):
        raise ApplicationInternalError()
    return timestamp


class ConversationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or _default_clock
        self._id_factory = id_factory or _default_id_factory

    def create(
        self,
        actor_context: ActorContext,
        title: str | None,
    ) -> Conversation:
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ApplicationValidationError()
            normalized_title = title.strip()
        else:
            normalized_title = None
        timestamp = _validated_utc_now(self._clock)
        conversation = Conversation(
            conversation_id=self._id_factory("conv"),
            actor_id=actor_context.actor_id,
            title=normalized_title,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                unit_of_work.conversations.add(conversation)
                unit_of_work.commit()
        except PersistenceError as error:
            raise from_persistence_error(error) from None
        return conversation

    def create_idempotent(
        self,
        actor_context: ActorContext,
        title: str | None,
        *,
        request_id: str,
        idempotency_key: str,
    ) -> ConversationCreation:
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ApplicationValidationError()
            normalized_title = title.strip()
        else:
            normalized_title = None
        if not isinstance(request_id, str) or not request_id.strip():
            raise ApplicationValidationError()
        try:
            validated_key = validate_idempotency_key(idempotency_key)
        except (TypeError, ValueError):
            raise ApplicationValidationError() from None
        digest = canonical_request_digest({"title": normalized_title})
        timestamp = _validated_utc_now(self._clock)
        conversation = Conversation(
            conversation_id=self._id_factory("conv"),
            actor_id=actor_context.actor_id,
            title=normalized_title,
            created_at=timestamp,
            updated_at=timestamp,
        )
        record = IdempotencyRecord(
            idempotency_record_id=self._id_factory("idem"),
            actor_id=actor_context.actor_id,
            operation=CONVERSATION_CREATE,
            idempotency_key=validated_key,
            request_digest=digest,
            first_request_id=request_id,
            task_id=None,
            message_id=None,
            task_input_revision_id=None,
            tool_run_id=None,
            explanation_id=None,
            created_at=timestamp,
            expires_at=None,
            conversation_id=conversation.conversation_id,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                if unit_of_work.actors.get_for_update(actor_context.actor_id) is None:
                    raise ResourceNotFoundError()
                existing = unit_of_work.idempotency_records.get_by_scope(
                    actor_context.actor_id,
                    CONVERSATION_CREATE,
                    validated_key,
                )
                if existing is not None:
                    return self._load_creation(
                        unit_of_work,
                        actor_context,
                        record=existing,
                        request_digest=digest,
                        outcome=IdempotencyOutcome.REPLAY,
                    )
                unit_of_work.conversations.add(conversation)
                unit_of_work.idempotency_records.add(record)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_creation(
                actor_context,
                idempotency_key=validated_key,
                request_digest=digest,
                request_id=request_id,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(error) from None
        return ConversationCreation(conversation, IdempotencyOutcome.CREATED)

    def _recover_creation(
        self,
        actor_context: ActorContext,
        *,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
    ) -> ConversationCreation | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                record = unit_of_work.idempotency_records.get_by_scope(
                    actor_context.actor_id,
                    CONVERSATION_CREATE,
                    idempotency_key,
                )
                if record is None:
                    return None
                return self._load_creation(
                    unit_of_work,
                    actor_context,
                    record=record,
                    request_digest=request_digest,
                    outcome=recovered_idempotency_outcome(
                        first_request_id=record.first_request_id,
                        current_request_id=request_id,
                    ),
                )
        except PersistenceError:
            return None

    @staticmethod
    def _load_creation(
        unit_of_work: object,
        actor_context: ActorContext,
        *,
        record: IdempotencyRecord,
        request_digest: str,
        outcome: IdempotencyOutcome,
    ) -> ConversationCreation:
        if record.request_digest != request_digest:
            raise IdempotencyConflictError()
        conversation = (
            None
            if record.conversation_id is None
            else unit_of_work.conversations.get_owned(
                record.conversation_id,
                actor_context.actor_id,
            )
        )
        if conversation is None:
            raise ResourceNotFoundError()
        return ConversationCreation(conversation, outcome)

    def list(
        self,
        actor_context: ActorContext,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ConversationListPage:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ApplicationValidationError()
        decoded_cursor = (
            decode_conversation_cursor(cursor) if cursor is not None else None
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                conversations = unit_of_work.conversations.list_owned(
                    actor_context.actor_id
                )
                candidates: list[Conversation] = []
                for conversation in conversations:
                    if decoded_cursor is not None and not (
                        conversation.updated_at < decoded_cursor.updated_at
                        or (
                            conversation.updated_at == decoded_cursor.updated_at
                            and conversation.conversation_id
                            < decoded_cursor.conversation_id
                        )
                    ):
                        continue
                    candidates.append(conversation)
                    if len(candidates) == limit + 1:
                        break

                page_conversations = candidates[:limit]
                items: list[ConversationListItem] = []
                for conversation in page_conversations:
                    latest = (
                        unit_of_work.messages.get_latest_for_conversation(
                            conversation.conversation_id,
                            actor_context.actor_id,
                        )
                    )
                    items.append(
                        ConversationListItem(
                            conversation_id=conversation.conversation_id,
                            title=conversation.title,
                            created_at=conversation.created_at,
                            updated_at=conversation.updated_at,
                            last_activity_preview=(
                                generate_last_activity_preview(
                                    latest.content_text
                                )
                                if latest is not None
                                else None
                            ),
                        )
                    )
        except PersistenceError as error:
            raise from_persistence_error(error) from None

        page_items = items[:limit]
        next_cursor = None
        if len(candidates) > limit:
            last_item = page_items[-1]
            next_cursor = encode_conversation_cursor(
                last_item.updated_at,
                last_item.conversation_id,
            )
        return ConversationListPage(
            items=tuple(page_items),
            next_cursor=next_cursor,
        )
