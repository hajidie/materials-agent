from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json

import pytest


UTC_TIME = datetime(2026, 7, 19, 6, 30, 15, tzinfo=timezone.utc)


def _conversation_functions():
    from materialsagent.application.conversations import (
        decode_conversation_cursor,
        encode_conversation_cursor,
        generate_conversation_title,
        generate_last_activity_preview,
    )

    return (
        generate_conversation_title,
        generate_last_activity_preview,
        encode_conversation_cursor,
        decode_conversation_cursor,
    )


def _raw_cursor(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return encoded.rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    ("content_text", "expected"),
    [
        ("请分析这个材料问题", "请分析这个材料问题"),
        ("Analyze this material", "Analyze this material"),
        ("  多行\n\t中文\u3000 文本  ", "多行 中文 文本"),
        ("\t\n\u3000", "新对话"),
    ],
)
def test_generate_conversation_title_normalizes_deterministically(
    content_text: str,
    expected: str,
) -> None:
    generate_title, _, _, _ = _conversation_functions()

    assert generate_title(content_text) == expected
    assert generate_title(content_text) == expected


def test_generate_conversation_title_truncates_by_unicode_code_point() -> None:
    generate_title, _, _, _ = _conversation_functions()
    content_text = "材" * 41

    assert generate_title(content_text) == ("材" * 40) + "…"


def test_generate_conversation_title_does_not_read_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate_title, _, _, _ = _conversation_functions()

    monkeypatch.setattr("os.getenv", lambda *args: pytest.fail("read env"))
    monkeypatch.setattr("random.random", lambda: pytest.fail("read random"))
    monkeypatch.setattr("time.time", lambda: pytest.fail("read clock"))

    assert generate_title("  stable\n title  ") == "stable title"


@pytest.mark.parametrize(
    ("content_text", "expected"),
    [
        ("  one\n\ttwo\u3000three  ", "one two three"),
        ("显" * 80, "显" * 80),
        ("显" * 81, ("显" * 80) + "…"),
    ],
)
def test_last_activity_preview_is_normalized_and_bounded(
    content_text: str,
    expected: str,
) -> None:
    _, generate_preview, _, _ = _conversation_functions()

    assert generate_preview(content_text) == expected


def test_conversation_cursor_round_trip() -> None:
    _, _, encode_cursor, decode_cursor = _conversation_functions()

    encoded = encode_cursor(UTC_TIME, "conv_opaque")
    decoded = decode_cursor(encoded)

    assert decoded.version == 1
    assert decoded.updated_at == UTC_TIME
    assert decoded.conversation_id == "conv_opaque"


@pytest.mark.parametrize(
    "cursor",
    [
        "not-valid-@@@",
        "A" * 4096,
        _raw_cursor(
            {
                "version": 1,
                "updated_at": "2026-07-19T06:30:15Z",
            }
        ),
        _raw_cursor(
            {
                "version": 1,
                "updated_at": "2026-07-19T14:30:15+08:00",
                "conversation_id": "conv_opaque",
            }
        ),
        _raw_cursor(
            {
                "version": 2,
                "updated_at": "2026-07-19T06:30:15Z",
                "conversation_id": "conv_opaque",
            }
        ),
        _raw_cursor(
            {
                "version": 1.0,
                "updated_at": "2026-07-19T06:30:15Z",
                "conversation_id": "conv_opaque",
            }
        ),
        _raw_cursor(
            {
                "version": "1",
                "updated_at": "2026-07-19T06:30:15Z",
                "conversation_id": "conv_opaque",
            }
        ),
        _raw_cursor(
            {
                "version": True,
                "updated_at": "2026-07-19T06:30:15Z",
                "conversation_id": "conv_opaque",
            }
        ),
    ],
)
def test_conversation_cursor_rejects_invalid_input_without_echo(
    cursor: str,
) -> None:
    _, _, _, decode_cursor = _conversation_functions()
    from materialsagent.application.errors import InvalidCursorError

    with pytest.raises(InvalidCursorError) as exc_info:
        decode_cursor(cursor)

    assert cursor not in str(exc_info.value)


class FakeConversationRepository:
    def __init__(self, conversations: list[object]) -> None:
        self._conversations = conversations
        self.actor_ids: list[str] = []

    def list_owned(self, actor_id: str) -> list[object]:
        self.actor_ids.append(actor_id)
        return list(self._conversations)


class FakeMessageRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_latest_for_conversation(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> None:
        self.calls.append((conversation_id, actor_id))
        return None


class FakeUnitOfWork:
    def __init__(
        self,
        conversations: FakeConversationRepository,
        messages: FakeMessageRepository,
    ) -> None:
        self.conversations = conversations
        self.messages = messages

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None


class FakeUnitOfWorkFactory:
    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    def __call__(self) -> FakeUnitOfWork:
        return self._unit_of_work


def _conversation_list_fakes():
    from materialsagent.application.context import ActorContext
    from materialsagent.application.conversations import ConversationService
    from materialsagent.domain.models.conversation import Conversation

    actor_context = ActorContext(actor_id="actor_opaque", user_id=None)
    conversations = [
        Conversation(
            conversation_id=f"conv_{index:02d}",
            actor_id=actor_context.actor_id,
            title=None,
            created_at=UTC_TIME - timedelta(hours=1),
            updated_at=UTC_TIME - timedelta(minutes=index),
        )
        for index in range(10)
    ]
    conversation_repository = FakeConversationRepository(conversations)
    message_repository = FakeMessageRepository()
    unit_of_work = FakeUnitOfWork(
        conversation_repository,
        message_repository,
    )
    service = ConversationService(FakeUnitOfWorkFactory(unit_of_work))
    return (
        service,
        actor_context,
        conversations,
        conversation_repository,
        message_repository,
    )


def test_conversation_list_queries_preview_only_for_returned_limit() -> None:
    (
        service,
        actor_context,
        conversations,
        conversation_repository,
        message_repository,
    ) = _conversation_list_fakes()

    page = service.list(actor_context, limit=2)

    assert [item.conversation_id for item in page.items] == [
        conversations[0].conversation_id,
        conversations[1].conversation_id,
    ]
    assert page.next_cursor is not None
    assert conversation_repository.actor_ids == [actor_context.actor_id]
    assert message_repository.calls == [
        (conversations[0].conversation_id, actor_context.actor_id),
        (conversations[1].conversation_id, actor_context.actor_id),
    ]


def test_conversation_list_with_cursor_queries_only_returned_previews() -> None:
    (
        service,
        actor_context,
        conversations,
        _,
        message_repository,
    ) = _conversation_list_fakes()
    _, _, encode_cursor, _ = _conversation_functions()
    cursor = encode_cursor(
        conversations[1].updated_at,
        conversations[1].conversation_id,
    )

    page = service.list(actor_context, limit=2, cursor=cursor)

    assert [item.conversation_id for item in page.items] == [
        conversations[2].conversation_id,
        conversations[3].conversation_id,
    ]
    assert page.next_cursor is not None
    assert message_repository.calls == [
        (conversations[2].conversation_id, actor_context.actor_id),
        (conversations[3].conversation_id, actor_context.actor_id),
    ]


def test_actor_context_is_minimal_immutable_and_requires_local_mvp_shape() -> None:
    from materialsagent.application.context import ActorContext

    context = ActorContext(actor_id="actor_opaque", user_id=None)
    assert context.actor_id == "actor_opaque"
    assert context.user_id is None
    assert set(context.__slots__) == {"actor_id", "user_id"}
    with pytest.raises(FrozenInstanceError):
        context.actor_id = "actor_other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="actor_id"):
        ActorContext(actor_id="   ", user_id=None)
    with pytest.raises(ValueError, match="user_id"):
        ActorContext(actor_id="actor_opaque", user_id="user_client")
