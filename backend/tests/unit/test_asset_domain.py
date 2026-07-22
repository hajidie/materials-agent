from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest


BASE = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)


def _pending():
    from materialsagent.domain.models.asset import Asset

    return Asset.pending(
        asset_id="asset_1",
        task_id="task_1",
        producer_tool_run_id="tool_run_1",
        actor_id="actor_1",
        operation_id="asset_operation_1",
        role="requested_output",
        object_key="assets/test/asset_1.png",
        pending_since=BASE,
        created_at=BASE,
    )


def _available():
    return _pending().mark_available(
        sha256="a" * 64,
        size_bytes=1234,
        width=512,
        height=512,
        bit_depth=8,
        media_type="image/png",
        encoding_rule="linear[-1,1]-half-up-uint8-png-l",
        available_at=BASE + timedelta(seconds=1),
    )


def test_pending_generated_asset_has_stable_source_and_no_final_metadata() -> None:
    asset = _pending()

    assert asset.current_status == "PENDING"
    assert asset.asset_type == "sem_image"
    assert asset.source_type == "GENERATED"
    assert asset.producer_tool_run_id == "tool_run_1"
    assert asset.media_type is None
    assert asset.sha256 is None
    assert asset.size_bytes is None


def test_allowed_asset_status_transitions_preserve_source_fields() -> None:
    pending = _pending()
    available = _available()
    failed = pending.mark_failed(
        error_code="INVALID_MODEL_OUTPUT",
        safe_error_message="Tool Runtime returned invalid model output.",
        failed_at=BASE + timedelta(seconds=1),
    )
    pending_orphan = pending.mark_orphaned(
        orphan_reason="UPLOAD_OUTCOME_UNCERTAIN",
        orphan_details={"head": "unavailable"},
        orphaned_at=BASE + timedelta(seconds=1),
    )
    available_orphan = available.mark_orphaned(
        orphan_reason="OBJECT_MISSING",
        orphan_details={"head": "missing"},
        orphaned_at=BASE + timedelta(seconds=2),
    )
    restored = available_orphan.mark_available(
        sha256=available.sha256,
        size_bytes=available.size_bytes,
        width=available.width,
        height=available.height,
        bit_depth=available.bit_depth,
        media_type=available.media_type,
        encoding_rule=available.encoding_rule,
        available_at=BASE + timedelta(seconds=3),
    )
    abandoned = pending_orphan.mark_failed(
        error_code="ASSET_UPLOAD_FAILED",
        safe_error_message="Asset upload failed.",
        failed_at=BASE + timedelta(seconds=3),
    )

    assert available.current_status == "AVAILABLE"
    assert failed.current_status == "FAILED"
    assert pending_orphan.current_status == "ORPHANED"
    assert available_orphan.current_status == "ORPHANED"
    assert restored.current_status == "AVAILABLE"
    assert abandoned.current_status == "FAILED"
    for changed in (
        available,
        failed,
        pending_orphan,
        available_orphan,
        restored,
        abandoned,
    ):
        assert changed.asset_id == pending.asset_id
        assert changed.task_id == pending.task_id
        assert changed.producer_tool_run_id == pending.producer_tool_run_id
        assert changed.actor_id == pending.actor_id
        assert changed.object_key == pending.object_key


@pytest.mark.parametrize(
    "transition",
    [
        lambda: _available().mark_available(
            sha256="a" * 64,
            size_bytes=1234,
            width=512,
            height=512,
            bit_depth=8,
            media_type="image/png",
            encoding_rule="fixed",
            available_at=BASE + timedelta(seconds=2),
        ),
        lambda: _available().mark_failed(
            error_code="ASSET_UPLOAD_FAILED",
            safe_error_message="Asset upload failed.",
            failed_at=BASE + timedelta(seconds=2),
        ),
        lambda: _pending().mark_orphaned(
            orphan_reason="UPLOAD_OUTCOME_UNCERTAIN",
            orphan_details={},
            orphaned_at=BASE - timedelta(seconds=1),
        ),
    ],
)
def test_forbidden_transition_or_time_order_is_rejected(transition) -> None:
    with pytest.raises(ValueError):
        transition()


@pytest.mark.parametrize(
    "mutation",
    [
        {"current_status": "AVAILABLE"},
        {"current_status": "FAILED"},
        {"current_status": "ORPHANED"},
        {"object_key": "../unsafe.png"},
        {"producer_tool_run_id": None},
        {
            "current_status": "ORPHANED",
            "orphaned_at": BASE + timedelta(seconds=1),
            "orphan_reason": "PARTIAL_FINAL_METADATA",
            "orphan_details": {},
            "media_type": "image/png",
        },
    ],
)
def test_invalid_status_shape_or_generated_source_is_rejected(
    mutation: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(_pending(), **mutation)
