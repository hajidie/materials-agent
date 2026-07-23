from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


BASE_TIME = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def _explanation_type() -> type[Any]:
    from materialsagent.domain.models.explanation import NaturalLanguageExplanation

    return NaturalLanguageExplanation


def _explanation(status: str = "PENDING", **overrides: object) -> object:
    NaturalLanguageExplanation = _explanation_type()
    started_at = BASE_TIME + timedelta(seconds=1) if status != "PENDING" else None
    completed_at = BASE_TIME + timedelta(seconds=2) if status in {"SUCCEEDED", "FAILED"} else None
    values = {
        "explanation_id": "explanation_domain",
        "task_id": "task_domain",
        "result_id": "result_domain",
        "llm_call_id": "llm_call_domain",
        "attempt_no": 1,
        "status": status,
        "language": "zh-CN",
        "text": "这是已持久化结果的安全说明。" if status == "SUCCEEDED" else None,
        "created_at": BASE_TIME,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": 1000 if status in {"SUCCEEDED", "FAILED"} else None,
        "error_code": "LLM_TIMEOUT" if status == "FAILED" else None,
        "safe_error_message": "Explanation timed out." if status == "FAILED" else None,
    }
    values.update(overrides)
    return NaturalLanguageExplanation(**values)


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"])
def test_explanation_statuses_have_valid_shapes(status: str) -> None:
    explanation = _explanation(status)

    assert explanation.status == status
    assert explanation.attempt_no == 1


@pytest.mark.parametrize(
    ("status", "changes"),
    [
        ("PENDING", {"started_at": BASE_TIME}),
        ("RUNNING", {"completed_at": BASE_TIME + timedelta(seconds=2)}),
        ("SUCCEEDED", {"text": None}),
        ("SUCCEEDED", {"error_code": "STALE"}),
        ("FAILED", {"text": "unexpected text"}),
        ("FAILED", {"error_code": None}),
    ],
)
def test_explanation_rejects_invalid_state_shapes(
    status: str,
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=status):
        _explanation(status, **changes)


def test_explanation_enforces_safe_attempt_timing_and_text() -> None:
    with pytest.raises(ValueError, match="attempt_no"):
        _explanation(attempt_no=True)
    with pytest.raises(ValueError, match="completed_at"):
        _explanation("SUCCEEDED", completed_at=BASE_TIME)
    with pytest.raises(ValueError, match="duration_ms"):
        _explanation("SUCCEEDED", duration_ms=-1)
    with pytest.raises(ValueError, match="text"):
        _explanation("SUCCEEDED", text="x" * 4097)
    with pytest.raises(ValueError, match="safe_error_message"):
        _explanation("FAILED", safe_error_message="unsafe\ntraceback")


def test_explanation_is_frozen_and_excludes_prompt_or_raw_response_fields() -> None:
    explanation = _explanation("SUCCEEDED")

    with pytest.raises(FrozenInstanceError):
        explanation.text = "late mutation"
    with pytest.raises(TypeError):
        _explanation(prompt="full prompt")
    with pytest.raises(TypeError):
        _explanation(raw_response={"secret": "raw provider output"})


def test_explanation_rejects_blank_ids_and_language() -> None:
    explanation = _explanation()
    for field in ("explanation_id", "task_id", "result_id", "llm_call_id", "language"):
        with pytest.raises(ValueError, match=field):
            replace(explanation, **{field: "   "})
