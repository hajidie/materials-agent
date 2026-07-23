from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


BASE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _type() -> type[Any]:
    from materialsagent.domain.models.llm_call import LLMCall

    return LLMCall


def _call(status: str = "PENDING", **overrides: object) -> object:
    LLMCall = _type()
    started_at = None
    completed_at = None
    duration_ms = None
    error_code = None
    safe_error_message = None
    if status in {"RUNNING", "SUCCEEDED", "FAILED"}:
        started_at = BASE_TIME + timedelta(seconds=1)
    if status in {"SUCCEEDED", "FAILED"}:
        completed_at = BASE_TIME + timedelta(seconds=2)
        duration_ms = 1000
    if status == "FAILED":
        error_code = "LLM_PROVIDER_FAILED"
        safe_error_message = "LLM call failed."
    values = {
        "llm_call_id": "llm_call_domain",
        "task_id": "task_domain",
        "conversation_id": "conversation_domain",
        "request_id": "request_domain",
        "purpose": "CHAT_ORCHESTRATION",
        "input_result_id": None,
        "provider": "mock",
        "model_name": "mock-chat-orchestration-v1",
        "prompt_template_id": "chat-orchestration",
        "prompt_template_version": "1",
        "prompt_digest": "a" * 64,
        "generation_parameters": {"temperature": 0, "max_tokens": 256},
        "structured_output_summary": {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "provider_request_id": None,
        "status": status,
        "created_at": BASE_TIME,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "error_code": error_code,
        "safe_error_message": safe_error_message,
    }
    values.update(overrides)
    return LLMCall(**values)


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"])
def test_all_four_statuses_have_valid_time_shapes(status: str) -> None:
    assert _call(status).status == status


def test_both_purposes_enforce_input_result_id_contract() -> None:
    chat = _call()
    explanation = _call(
        purpose="TOOL_RESULT_EXPLANATION",
        input_result_id="result_domain",
        structured_output_summary=None,
    )

    assert chat.input_result_id is None
    assert explanation.input_result_id == "result_domain"

    with pytest.raises(ValueError, match="input_result_id must be null"):
        _call(input_result_id="result_for_chat")
    with pytest.raises(ValueError, match="input_result_id is required"):
        _call(purpose="TOOL_RESULT_EXPLANATION", input_result_id=None)


def test_negative_or_boolean_duration_is_rejected() -> None:
    for duration in (-1, True):
        with pytest.raises(ValueError, match="duration_ms"):
            _call("SUCCEEDED", duration_ms=duration)


def test_completed_at_cannot_precede_started_at() -> None:
    with pytest.raises(ValueError, match="completed_at"):
        _call("SUCCEEDED", completed_at=BASE_TIME)


def test_succeeded_rejects_stale_errors_and_failed_requires_error_code() -> None:
    with pytest.raises(ValueError, match="SUCCEEDED"):
        _call("SUCCEEDED", error_code="STALE")
    with pytest.raises(ValueError, match="FAILED"):
        _call("FAILED", error_code=None)


@pytest.mark.parametrize(
    ("status", "changes"),
    [
        ("PENDING", {"started_at": BASE_TIME}),
        ("RUNNING", {"completed_at": BASE_TIME + timedelta(seconds=2)}),
        ("SUCCEEDED", {"duration_ms": None}),
        ("FAILED", {"completed_at": None}),
    ],
)
def test_status_time_combinations_are_strict(
    status: str,
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=status):
        _call(status, **changes)


def test_utc_timestamps_are_required() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _call(created_at=BASE_TIME.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("generation_parameters", {"api_key": "secret"}),
        (
            "generation_parameters",
            {"temperature": 0, "max_tokens": 256, "messages": []},
        ),
        ("structured_output_summary", {"prompt": "full prompt text"}),
        ("structured_output_summary", {"answer_text": "full answer text"}),
        ("structured_output_summary", {"provider_response": {"raw": True}}),
        ("structured_output_summary", {"response_body": "raw response"}),
        (
            "structured_output_summary",
            {"headers": {"x-api-key": "SECRET"}},
        ),
        ("structured_output_summary", {"messages": ["full prompt"]}),
        ("structured_output_summary", {"content": "full response"}),
        ("structured_output_summary", {"api-key": "secret"}),
        ("structured_output_summary", {"system_prompt": "full prompt"}),
        ("structured_output_summary", {"fullPrompt": "full prompt"}),
        (
            "structured_output_summary",
            {"details": ({"secret": "nested secret"},)},
        ),
        ("usage", {"value": float("nan")}),
        (
            "usage",
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "response_body": "raw response",
            },
        ),
        ("structured_output_summary", {"answer_summary": "x" * 5000}),
    ],
)
def test_json_metadata_rejects_secrets_prompts_raw_responses_and_unbounded_data(
    field: str,
    unsafe_value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _call(**{field: unsafe_value})


def test_safe_json_summary_is_copied_and_does_not_store_full_answer() -> None:
    summary = {
        "route": "KNOWLEDGE_ANSWER",
        "answer_length": 12,
        "answer_digest": "b" * 64,
    }
    call = _call(structured_output_summary=summary)
    summary["answer_length"] = 999

    assert call.structured_output_summary["answer_length"] == 12
    assert "answer_text" not in call.structured_output_summary


@pytest.mark.parametrize(
    "provider_request_id",
    [
        "contains a space",
        "raw\nresponse",
        "x" * 257,
        "secret=credential",
    ],
)
def test_provider_request_id_rejects_uncontrolled_metadata(
    provider_request_id: str,
) -> None:
    with pytest.raises(ValueError, match="provider_request_id"):
        _call(provider_request_id=provider_request_id)


def test_safe_json_metadata_is_deeply_immutable_after_validation() -> None:
    call = _call(
        structured_output_summary={
            "route": "NEEDS_INPUT",
            "tool_id": "zta35g_sem_virtual_lab",
            "missing_fields": ["solution_time"],
            "ambiguous_fields": ["aging_time"],
        }
    )

    with pytest.raises(TypeError):
        call.structured_output_summary["secret"] = "late mutation"
    with pytest.raises((AttributeError, TypeError)):
        call.structured_output_summary["missing_fields"].append("material")
    with pytest.raises(TypeError):
        dict.__setitem__(
            call.structured_output_summary,
            "response_body",
            "SECRET",
        )


def test_llm_json_fields_reject_scalar_subclasses_with_hidden_state() -> None:
    class SecretInt(int):
        pass

    class SecretFloat(float):
        pass

    class SecretStr(str):
        pass

    secret_int = SecretInt(10)
    secret_float = SecretFloat(0)
    secret_route = SecretStr("TOOL_EXECUTION")
    for value in (secret_int, secret_float, secret_route):
        value.response_body = "SECRET"

    unsafe_values = (
        (
            "usage",
            {"input_tokens": secret_int, "output_tokens": 5},
        ),
        (
            "generation_parameters",
            {"temperature": secret_float, "max_tokens": 256},
        ),
        (
            "structured_output_summary",
            {"route": secret_route, "tool_id": "zta35g_sem_virtual_lab"},
        ),
    )
    for field, unsafe_value in unsafe_values:
        with pytest.raises(ValueError, match=field):
            _call(**{field: unsafe_value})


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("generation_parameters", {"temperature": 0}),
        (
            "generation_parameters",
            {"temperature": True, "max_tokens": 256},
        ),
        ("usage", {"input_tokens": 10}),
        ("usage", {"input_tokens": 10, "output_tokens": -1}),
        (
            "structured_output_summary",
            {"route": "TOOL_EXECUTION", "tool_id": "unknown_tool"},
        ),
        (
            "structured_output_summary",
            {"route": "KNOWLEDGE_ANSWER", "content": "full answer"},
        ),
    ],
)
def test_llm_json_fields_accept_only_controlled_business_schemas(
    field: str,
    unsafe_value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _call(**{field: unsafe_value})


def test_explanation_call_does_not_store_chat_structured_summary() -> None:
    with pytest.raises(ValueError, match="structured_output_summary"):
        _call(
            purpose="TOOL_RESULT_EXPLANATION",
            input_result_id="result_domain",
            structured_output_summary={"route": "KNOWLEDGE_ANSWER"},
        )


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "x" * 257,
        "Provider failed.\nTraceback: SECRET at C:\\private\\provider.py",
    ],
)
def test_safe_error_message_is_bounded_single_line_text(
    unsafe_message: str,
) -> None:
    with pytest.raises(ValueError, match="safe_error_message"):
        _call("FAILED", safe_error_message=unsafe_message)


def test_prompt_digest_must_be_sha256_hex() -> None:
    with pytest.raises(ValueError, match="prompt_digest"):
        _call(prompt_digest="not-a-digest")


def test_blank_ids_provider_and_model_are_rejected() -> None:
    call = _call()
    for field in (
        "llm_call_id",
        "task_id",
        "conversation_id",
        "request_id",
        "provider",
        "model_name",
    ):
        with pytest.raises(ValueError, match=field):
            replace(call, **{field: "   "})
