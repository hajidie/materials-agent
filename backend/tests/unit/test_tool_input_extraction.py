from __future__ import annotations

from dataclasses import fields
from importlib import import_module
from datetime import datetime, timezone

import pytest


def _port_module():
    try:
        return import_module(
            "materialsagent.domain.ports.tool_input_extraction"
        )
    except ModuleNotFoundError:
        pytest.fail("The fixed-Tool input extraction protocol is not implemented.")


def test_extraction_input_exposes_only_fixed_tool_context_and_user_delta() -> None:
    module = _port_module()
    from materialsagent.domain.ports.tool_registry import ToolRef

    command = module.ToolInputExtractionInput(
        content_text="solution time is 2 h",
        tool_context_ref=ToolRef("safe_tool", "1", "a" * 64),
        candidate_input_schema={"type": "object", "properties": {}},
        missing_fields=("solution_time",),
        ambiguous_fields=(),
    )

    assert {field.name for field in fields(command)} == {
        "content_text",
        "tool_context_ref",
        "candidate_input_schema",
        "missing_fields",
        "ambiguous_fields",
        "context_window",
    }
    assert not hasattr(command, "task_id")
    assert not hasattr(command, "conversation_id")
    assert not hasattr(command, "request_id")


def test_candidate_input_delta_is_deeply_immutable_and_detached() -> None:
    module = _port_module()
    source = {"value": {"unit": "h", "amounts": [2, 3]}}

    outcome = module.ToolInputExtractionOutcome(
        candidate_input_delta=source,
        request_metadata=module.ToolInputExtractionRequestMetadata(
            provider="mock",
            model_name="mock-tool-input-v1",
            prompt_template_id="tool-input-extraction",
            prompt_template_version="1",
            prompt_digest="a" * 64,
            generation_parameters={"temperature": 0, "max_tokens": 256},
        ),
    )
    source["value"]["amounts"].append(4)

    assert outcome.candidate_input_delta == {
        "value": {"unit": "h", "amounts": (2, 3)}
    }
    with pytest.raises(TypeError):
        outcome.candidate_input_delta["other"] = 1


@pytest.mark.parametrize(
    "candidate_input_delta",
    [
        {"unsafe": float("nan")},
        {"unsafe": object()},
        {"oversized": "x" * 4097},
    ],
)
def test_candidate_input_delta_rejects_nonstandard_or_oversized_json(
    candidate_input_delta: dict[str, object],
) -> None:
    module = _port_module()

    with pytest.raises(ValueError):
        module.ToolInputExtractionOutcome(
            candidate_input_delta=candidate_input_delta,
            request_metadata=module.ToolInputExtractionRequestMetadata(
                provider="mock",
                model_name="mock-tool-input-v1",
                prompt_template_id="tool-input-extraction",
                prompt_template_version="1",
                prompt_digest="a" * 64,
                generation_parameters={"temperature": 0, "max_tokens": 256},
            ),
        )


def test_extraction_audit_applies_4096_byte_limit_to_delta_not_wrapper() -> None:
    module = _port_module()
    from materialsagent.domain.models.llm_call import LLMCall
    from materialsagent.domain.ports.tool_registry import ToolRef

    delta = {"value": "x" * 4070}
    outcome = module.ToolInputExtractionOutcome(
        candidate_input_delta=delta,
        request_metadata=module.ToolInputExtractionRequestMetadata(
            provider="mock",
            model_name="mock-tool-input-v1",
            prompt_template_id="tool-input-extraction",
            prompt_template_version="1",
            prompt_digest="a" * 64,
            generation_parameters={"temperature": 0, "max_tokens": 256},
        ),
    )
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)

    call = LLMCall(
        llm_call_id="llm_large_delta",
        task_id="task_large_delta",
        conversation_id="conversation_large_delta",
        source_message_id="message_large_delta",
        request_id="request_large_delta",
        purpose="TOOL_INPUT_EXTRACTION",
        input_result_id=None,
        provider="mock",
        model_name="mock-tool-input-v1",
        prompt_template_id="tool-input-extraction",
        prompt_template_version="1",
        prompt_digest="a" * 64,
        generation_parameters={"temperature": 0, "max_tokens": 256},
        structured_output_summary={
            "candidate_input_delta": outcome.candidate_input_delta
        },
        usage=None,
        provider_request_id=None,
        status="SUCCEEDED",
        created_at=now,
        started_at=now,
        completed_at=now,
        duration_ms=0,
        error_code=None,
        safe_error_message=None,
        tool_context_ref=ToolRef("safe_tool", "1", "a" * 64),
    )

    assert len(call.structured_output_summary["candidate_input_delta"]["value"]) == 4070


def test_candidate_input_delta_rejects_cycles_as_controlled_validation() -> None:
    module = _port_module()
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError):
        module.ToolInputExtractionOutcome(
            candidate_input_delta=cyclic,
            request_metadata=module.ToolInputExtractionRequestMetadata(
                provider="mock",
                model_name="mock-tool-input-v1",
                prompt_template_id="tool-input-extraction",
                prompt_template_version="1",
                prompt_digest="a" * 64,
                generation_parameters={"temperature": 0, "max_tokens": 256},
            ),
        )
