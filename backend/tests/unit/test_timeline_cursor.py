from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json

import pytest
from pydantic import SecretStr

from materialsagent.application.errors import InvalidCursorError
from materialsagent.application.timeline_cursor import (
    TimelineCursor,
    TimelineCursorCodec,
)


SIGNING_KEY = SecretStr("timeline-signing-key-with-at-least-32-bytes")
ANCHOR = datetime(2026, 7, 24, 1, 2, 3, 456000, tzinfo=timezone.utc)


def _cursor(**overrides: object) -> TimelineCursor:
    values: dict[str, object] = {
        "conversation_id": "conversation_1",
        "anchor_at": ANCHOR,
        "item_type_rank": 30,
        "item_id": "task_1",
    }
    values.update(overrides)
    return TimelineCursor(**values)  # type: ignore[arg-type]


def _parts(token: str) -> tuple[bytes, bytes]:
    encoded_payload, encoded_signature = token.split(".")

    def decode(value: str) -> bytes:
        return urlsafe_b64decode(value + ("=" * (-len(value) % 4)))

    return decode(encoded_payload), decode(encoded_signature)


def _token(
    payload: dict[str, object],
    signature: bytes | None = None,
) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    resolved_signature = signature or hmac.new(
        SIGNING_KEY.get_secret_value().encode("utf-8"),
        encoded,
        sha256,
    ).digest()
    return (
        urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")
        + "."
        + urlsafe_b64encode(resolved_signature).rstrip(b"=").decode("ascii")
    )


def test_cursor_is_canonical_signed_opaque_and_round_trips() -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)

    first = codec.encode(_cursor())
    second = codec.encode(_cursor())
    payload_bytes, signature = _parts(first)

    assert first == second
    assert len(signature) == 32
    assert "conversation_1" not in first
    assert json.loads(payload_bytes) == {
        "anchor_at": "2026-07-24T01:02:03.456000Z",
        "conversation_id": "conversation_1",
        "item_id": "task_1",
        "item_type_rank": 30,
        "version": 1,
    }
    assert codec.decode(
        first,
        expected_conversation_id="conversation_1",
    ) == _cursor()


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda valid: "",
        lambda valid: "not-a-token",
        lambda valid: valid + "x",
        lambda valid: valid.replace(".", "..", 1),
        lambda valid: "!" + valid,
        lambda valid: valid.split(".")[0] + ".eA",
        lambda valid: "a" * 2049,
    ],
)
def test_cursor_rejects_malformed_tampered_or_oversized_tokens(
    token_factory,
) -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)
    token = token_factory(codec.encode(_cursor()))

    with pytest.raises(InvalidCursorError, match="分页游标无效。"):
        codec.decode(token, expected_conversation_id="conversation_1")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"conversation_id": "conversation_other"},
        {"anchor_at": "2026-07-24T09:02:03.456+08:00"},
        {"anchor_at": "2026-07-24T01:02:03"},
        {"item_type_rank": 99},
        {"item_type_rank": 30.0},
        {"item_id": ""},
        {"extra": "forbidden"},
    ],
)
def test_cursor_rejects_wrong_payload_even_when_json_shape_is_parseable(
    changes: dict[str, object],
) -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)
    valid_payload, _ = _parts(codec.encode(_cursor()))
    payload = json.loads(valid_payload)
    payload.update(changes)

    with pytest.raises(InvalidCursorError, match="分页游标无效。"):
        codec.decode(
            _token(payload),
            expected_conversation_id="conversation_1",
        )


def test_cursor_rejects_missing_payload_field_and_decoded_json_over_512_bytes(
) -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)
    payload_bytes, _ = _parts(codec.encode(_cursor()))
    missing = json.loads(payload_bytes)
    del missing["item_id"]
    oversized = dict(missing)
    oversized["item_id"] = "x" * 600

    for value in (missing, oversized):
        with pytest.raises(InvalidCursorError, match="分页游标无效。"):
            codec.decode(
                _token(value),
                expected_conversation_id="conversation_1",
            )


def test_cursor_rejects_non_utc_encode_input() -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)

    with pytest.raises(ValueError, match="UTC"):
        codec.encode(
            _cursor(anchor_at=ANCHOR.astimezone(timezone(timedelta(hours=8))))
        )


def test_cursor_signature_is_bound_to_the_configured_secret() -> None:
    token = TimelineCursorCodec(SIGNING_KEY).encode(_cursor())
    other = TimelineCursorCodec(
        SecretStr("another-signing-key-with-at-least-32-bytes")
    )

    with pytest.raises(InvalidCursorError, match="分页游标无效。"):
        other.decode(token, expected_conversation_id="conversation_1")


def test_cursor_preserves_full_timestamp_precision_for_keyset_boundaries(
) -> None:
    codec = TimelineCursorCodec(SIGNING_KEY)
    precise = datetime(
        2026,
        7,
        24,
        1,
        2,
        3,
        456789,
        tzinfo=timezone.utc,
    )

    decoded = codec.decode(
        codec.encode(_cursor(anchor_at=precise)),
        expected_conversation_id="conversation_1",
    )

    assert decoded.anchor_at == precise
