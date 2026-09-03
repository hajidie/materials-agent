from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from materialsagent.application.idempotency import (
    EXPLANATION_RETRY,
    TASK_CREATE,
    TASK_INPUT_SUPPLEMENT,
    TOOL_RETRY,
    canonical_request_digest,
    validate_idempotency_key,
)
from materialsagent.domain.models.idempotency_record import IdempotencyRecord


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _record(**overrides: object) -> IdempotencyRecord:
    values: dict[str, object] = {
        "idempotency_record_id": "idem_1",
        "actor_id": "actor_1",
        "operation": TASK_CREATE,
        "idempotency_key": "opaque-client-key",
        "request_digest": "a" * 64,
        "first_request_id": "req_1",
        "task_id": "task_1",
        "message_id": "msg_1",
        "task_input_revision_id": None,
        "tool_run_id": None,
        "explanation_id": None,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    values.update(overrides)
    return IdempotencyRecord(**values)  # type: ignore[arg-type]


def test_idempotency_key_is_opaque_bounded_and_control_free() -> None:
    assert validate_idempotency_key("  client key  ") == "  client key  "
    assert validate_idempotency_key("x" * 255) == "x" * 255

    for value in (
        "",
        "   ",
        "x" * 256,
        "line\nbreak",
        "nul\x00byte",
        "unicode\u0085control",
    ):
        with pytest.raises(ValueError):
            validate_idempotency_key(value)


def test_canonical_request_digest_is_stable_and_strict_json() -> None:
    left = {
        "submission_mode": "NEW_TASK",
        "conversation_id": "conv_1",
        "content_text": "中文",
        "target_task_id": None,
    }
    right = {
        "target_task_id": None,
        "content_text": "中文",
        "conversation_id": "conv_1",
        "submission_mode": "NEW_TASK",
    }
    assert canonical_request_digest(left) == canonical_request_digest(right)
    assert canonical_request_digest(left) == (
        "48c4eeb6a2caa11ee8af599dfa6eab8363680c8ac7fa976ceee6a1a0dd68db76"
    )
    with pytest.raises(ValueError):
        canonical_request_digest({"value": float("nan")})


def test_record_requires_exact_operation_resource_binding() -> None:
    assert _record().task_id == "task_1"
    with pytest.raises(ValueError):
        _record(message_id=None)
    with pytest.raises(ValueError):
        _record(tool_run_id="run_1")
    with pytest.raises(ValueError):
        _record(operation="UNKNOWN")


@pytest.mark.parametrize(
    ("operation", "binding"),
    [
        (
            TASK_CREATE,
            {
                "message_id": "msg_1",
                "task_input_revision_id": None,
                "tool_run_id": None,
                "explanation_id": None,
            },
        ),
        (
            TASK_INPUT_SUPPLEMENT,
            {
                "message_id": "msg_1",
                "task_input_revision_id": "revision_2",
                "tool_run_id": None,
                "explanation_id": None,
            },
        ),
        (
            TOOL_RETRY,
            {
                "message_id": None,
                "task_input_revision_id": None,
                "tool_run_id": "tool_run_2",
                "explanation_id": None,
            },
        ),
        (
            EXPLANATION_RETRY,
            {
                "message_id": None,
                "task_input_revision_id": None,
                "tool_run_id": None,
                "explanation_id": "explanation_2",
            },
        ),
    ],
)
def test_all_four_operations_accept_only_their_resource_binding(
    operation: str,
    binding: dict[str, object],
) -> None:
    assert _record(operation=operation, **binding).operation == operation


def test_record_requires_safe_ids_digest_and_utc_expiry() -> None:
    for overrides in (
        {"actor_id": " "},
        {"request_digest": "A" * 64},
        {"first_request_id": ""},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"created_at": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(ValueError):
            _record(**overrides)


def test_record_accepts_nullable_expiration_without_ttl_semantics() -> None:
    record = _record(expires_at=None)

    assert record.expires_at is None


def test_record_is_frozen_and_never_contains_response_payload() -> None:
    record = _record()
    assert set(record.__dataclass_fields__) == {
        "idempotency_record_id",
        "actor_id",
        "operation",
        "idempotency_key",
        "request_digest",
            "first_request_id",
            "conversation_id",
            "task_id",
        "message_id",
        "task_input_revision_id",
        "tool_run_id",
        "explanation_id",
        "created_at",
        "expires_at",
    }
    with pytest.raises((AttributeError, TypeError)):
        record.task_id = "changed"  # type: ignore[misc]
