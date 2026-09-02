from __future__ import annotations

import pytest

from materialsagent.domain.ports.explanation import (
    ExplanationAssetReference,
    ExplanationInput,
    ExplanationOutcome,
    ExplanationProtocolError,
    ExplanationProviderUnavailableError,
    ExplanationTimeoutError,
)
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


def _input(*, with_performance: bool = True) -> ExplanationInput:
    return ExplanationInput(
        result_id="result_1",
        status="SUCCEEDED" if with_performance else "PARTIALLY_SUCCEEDED",
        requested_outputs=("sem_image", "mechanical_properties"),
        completed_outputs=(
            ("sem_image", "mechanical_properties")
            if with_performance
            else ("sem_image",)
        ),
        failed_outputs=() if with_performance else ("mechanical_properties",),
        data=(
            {
                "yield_strength": {"value": 650.0, "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
            if with_performance
            else {}
        ),
        artifacts=(
            ExplanationAssetReference(
                asset_id="asset_1",
                role="requested_output",
                asset_type="sem_image",
            ),
        ),
        warnings=(),
        error=(
            None
            if with_performance
            else {
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "Mechanical property prediction failed.",
                "retryable": False,
            }
        ),
        process_parameters={
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_hash="f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
    )


def test_mock_explanation_is_deterministic_and_fact_bound() -> None:
    adapter = MockExplanationAdapter(mode="success")

    first = adapter.explain(_input())
    second = adapter.explain(_input())
    failed_performance = adapter.explain(_input(with_performance=False))

    assert first == second
    assert first.text is not None
    assert "650" in first.text
    assert "3.2" in first.text
    assert failed_performance.text is not None
    assert "未获得" in failed_performance.text
    assert "650" not in failed_performance.text
    assert adapter.call_count == 3


def test_mock_explanation_request_metadata_preserves_legacy_identity() -> None:
    adapter = MockExplanationAdapter(mode="success")

    first = adapter.request_metadata(_input())
    second = adapter.request_metadata(_input())

    assert first == second
    assert first.provider == "mock"
    assert first.model_name == "mock-explanation"
    assert first.prompt_template_id == "tool-result-explanation"
    assert first.prompt_template_version == "1"
    assert len(first.prompt_digest) == 64
    assert first.generation_parameters == {
        "temperature": 0,
        "max_tokens": 512,
    }
    with pytest.raises(TypeError):
        first.generation_parameters["max_tokens"] = 1


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("timeout", ExplanationTimeoutError),
        ("provider_unavailable", ExplanationProviderUnavailableError),
        ("protocol_error", ExplanationProtocolError),
    ],
)
def test_mock_explanation_supports_controlled_transport_failures(
    mode: str,
    error_type: type[Exception],
) -> None:
    adapter = MockExplanationAdapter(mode=mode)

    with pytest.raises(error_type):
        adapter.explain(_input())

    assert adapter.call_count == 1


def test_mock_explanation_supports_explicit_failure_outcome() -> None:
    outcome = MockExplanationAdapter(mode="failure").explain(_input())

    assert outcome.text is None
    assert outcome.error_code == "EXPLANATION_FAILED"
    assert outcome.safe_error_message == "Explanation generation failed."
    assert outcome.llm_error_code is None
    assert outcome.llm_safe_error_message is None


@pytest.mark.parametrize(
    "provider_request_id",
    ["contains a space", "raw\nresponse", "x" * 257, "secret=credential"],
)
def test_explanation_outcome_rejects_uncontrolled_provider_request_id(
    provider_request_id: str,
) -> None:
    with pytest.raises(ValueError, match="provider_request_id"):
        ExplanationOutcome(
            text="受控说明。",
            usage={"input_tokens": 1, "output_tokens": 1},
            provider_request_id=provider_request_id,
            error_code=None,
            safe_error_message=None,
        )


def test_failed_explanation_outcome_accepts_exact_safe_error_boundaries() -> None:
    outcome = ExplanationOutcome(
        text=None,
        usage=None,
        provider_request_id=None,
        error_code="E" * 64,
        safe_error_message="M" * 256,
    )

    assert len(outcome.error_code) == 64
    assert len(outcome.safe_error_message) == 256


def test_failed_explanation_outcome_separates_internal_and_public_errors() -> None:
    outcome = ExplanationOutcome(
        text=None,
        usage=None,
        provider_request_id="provider-request-1",
        error_code="EXPLANATION_PROVIDER_UNAVAILABLE",
        safe_error_message="Explanation provider is unavailable.",
        llm_error_code="LLM_AUTHENTICATION_FAILED",
        llm_safe_error_message="LLM provider authentication failed.",
    )

    assert outcome.error_code == "EXPLANATION_PROVIDER_UNAVAILABLE"
    assert outcome.llm_error_code == "LLM_AUTHENTICATION_FAILED"


def test_successful_explanation_outcome_rejects_internal_errors() -> None:
    with pytest.raises(ValueError, match="Successful outcome"):
        ExplanationOutcome(
            text="受控说明。",
            usage=None,
            provider_request_id=None,
            error_code=None,
            safe_error_message=None,
            llm_error_code="LLM_PROVIDER_UNAVAILABLE",
            llm_safe_error_message="LLM provider is unavailable.",
        )


@pytest.mark.parametrize(
    ("error_code", "safe_error_message"),
    [
        ("E" * 65, "safe"),
        ("SAFE", "M" * 257),
        ("BAD\nCODE", "safe"),
        ("SAFE", "bad\nmessage"),
    ],
)
def test_failed_explanation_outcome_rejects_unbounded_or_unprintable_errors(
    error_code: str,
    safe_error_message: str,
) -> None:
    with pytest.raises(ValueError, match="controlled safe error"):
        ExplanationOutcome(
            text=None,
            usage=None,
            provider_request_id=None,
            error_code=error_code,
            safe_error_message=safe_error_message,
        )
