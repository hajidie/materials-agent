from __future__ import annotations

from enum import Enum
from hashlib import sha256
import json
from typing import Final
import unicodedata

from materialsagent.domain.models.idempotency_record import (
    EXPLANATION_RETRY,
    IDEMPOTENCY_OPERATIONS,
    TASK_CREATE,
    TASK_INPUT_SUPPLEMENT,
    TOOL_RETRY,
)


MAX_IDEMPOTENCY_KEY_LENGTH: Final = 255


class IdempotencyOutcome(str, Enum):
    CREATED = "CREATED"
    RECOVERED_OWN_COMMIT = "RECOVERED_OWN_COMMIT"
    REPLAY = "REPLAY"

    @property
    def replayed(self) -> bool:
        return self is IdempotencyOutcome.REPLAY


def recovered_idempotency_outcome(
    *,
    first_request_id: str,
    current_request_id: str,
) -> IdempotencyOutcome:
    return (
        IdempotencyOutcome.RECOVERED_OWN_COMMIT
        if first_request_id == current_request_id
        else IdempotencyOutcome.REPLAY
    )


def validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Idempotency-Key must be text.")
    if not value.strip() or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError("Idempotency-Key is invalid.")
    if any(
        unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("Idempotency-Key contains a control character.")
    return value


def canonical_request_digest(value: dict[str, object]) -> str:
    if not isinstance(value, dict):
        raise ValueError("Business request must be an object.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Business request is not canonical JSON.") from error
    return sha256(encoded).hexdigest()
