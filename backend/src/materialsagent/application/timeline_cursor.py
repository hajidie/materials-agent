from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import re
import unicodedata

from pydantic import SecretStr

from materialsagent.application.errors import InvalidCursorError
from materialsagent.domain.ports.timeline_query import TimelinePosition


_CURSOR_VERSION = 1
_MAX_TOKEN_LENGTH = 2048
_MAX_PAYLOAD_BYTES = 512
_PAYLOAD_KEYS = {
    "version",
    "conversation_id",
    "anchor_at",
    "item_type_rank",
    "item_id",
}
_ITEM_TYPE_RANKS = {10, 20, 30}
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _contains_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def _valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not _contains_control(value)
    )


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _utc_text(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("Timeline cursor timestamps must be UTC.")
    return value.isoformat().replace("+00:00", "Z")


def _decode_part(value: str) -> bytes:
    if not value or _BASE64URL.fullmatch(value) is None:
        raise ValueError
    return b64decode(
        value + ("=" * (-len(value) % 4)),
        altchars=b"-_",
        validate=True,
    )


TimelineCursor = TimelinePosition


class TimelineCursorCodec:
    def __init__(self, signing_key: SecretStr) -> None:
        secret = signing_key.get_secret_value()
        if (
            secret != secret.strip()
            or len(secret.encode("utf-8")) < 32
            or _contains_control(secret)
        ):
            raise ValueError("Invalid timeline cursor signing key.")
        self._key = secret.encode("utf-8")

    def encode(self, cursor: TimelineCursor) -> str:
        if (
            not _valid_identifier(cursor.conversation_id)
            or not _valid_identifier(cursor.item_id)
            or type(cursor.item_type_rank) is not int
            or cursor.item_type_rank not in _ITEM_TYPE_RANKS
        ):
            raise ValueError("Invalid timeline cursor.")
        payload = {
            "version": _CURSOR_VERSION,
            "conversation_id": cursor.conversation_id,
            "anchor_at": _utc_text(cursor.anchor_at),
            "item_type_rank": cursor.item_type_rank,
            "item_id": cursor.item_id,
        }
        encoded = _canonical_json(payload)
        if len(encoded) > _MAX_PAYLOAD_BYTES:
            raise ValueError("Timeline cursor payload is too large.")
        signature = hmac.new(self._key, encoded, sha256).digest()
        return (
            urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")
            + "."
            + urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def decode(
        self,
        token: str,
        *,
        expected_conversation_id: str,
    ) -> TimelineCursor:
        try:
            if (
                not isinstance(token, str)
                or not token
                or len(token) > _MAX_TOKEN_LENGTH
            ):
                raise ValueError
            parts = token.split(".")
            if len(parts) != 2:
                raise ValueError
            payload_bytes = _decode_part(parts[0])
            signature = _decode_part(parts[1])
            if (
                len(payload_bytes) > _MAX_PAYLOAD_BYTES
                or len(signature) != sha256().digest_size
            ):
                raise ValueError
            expected_signature = hmac.new(
                self._key,
                payload_bytes,
                sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError
            payload = json.loads(payload_bytes)
            if (
                not isinstance(payload, dict)
                or set(payload) != _PAYLOAD_KEYS
                or _canonical_json(payload) != payload_bytes
                or type(payload.get("version")) is not int
                or payload.get("version") != _CURSOR_VERSION
                or payload.get("conversation_id")
                != expected_conversation_id
                or not _valid_identifier(payload.get("conversation_id"))
                or not _valid_identifier(payload.get("item_id"))
                or type(payload.get("item_type_rank")) is not int
                or payload.get("item_type_rank") not in _ITEM_TYPE_RANKS
            ):
                raise ValueError
            anchor_text = payload.get("anchor_at")
            if (
                not isinstance(anchor_text, str)
                or not anchor_text.endswith("Z")
            ):
                raise ValueError
            anchor_at = datetime.fromisoformat(
                anchor_text[:-1] + "+00:00"
            )
            if _utc_text(anchor_at) != anchor_text:
                raise ValueError
            return TimelineCursor(
                conversation_id=payload["conversation_id"],
                anchor_at=anchor_at,
                item_type_rank=payload["item_type_rank"],
                item_id=payload["item_id"],
            )
        except InvalidCursorError:
            raise
        except Exception:
            raise InvalidCursorError() from None
