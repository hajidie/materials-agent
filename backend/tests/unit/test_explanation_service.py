from __future__ import annotations

import inspect
from types import SimpleNamespace


def test_explanation_service_public_surface_has_explicit_retry_api() -> None:
    from materialsagent.application.explanation_service import (
        ExplanationService,
    )

    public_methods = {
        name
        for name, value in inspect.getmembers(
            ExplanationService,
            predicate=inspect.isfunction,
        )
        if not name.startswith("_")
    }

    assert public_methods == {
        "explain",
        "prepare",
        "finalize",
        "reserve_retry_attempt",
        "execute_reserved_retry",
    }
    assert "retry" not in inspect.signature(
        ExplanationService.explain
    ).parameters


def test_explanation_service_preserves_internal_and_public_errors() -> None:
    from materialsagent.application.explanation_service import (
        ExplanationService,
    )
    from materialsagent.domain.ports.explanation import ExplanationOutcome

    expected = ExplanationOutcome(
        text=None,
        usage=None,
        provider_request_id="provider-request-1",
        error_code="EXPLANATION_PROVIDER_UNAVAILABLE",
        safe_error_message="Explanation provider is unavailable.",
        llm_error_code="LLM_AUTHENTICATION_FAILED",
        llm_safe_error_message="LLM provider authentication failed.",
    )
    port = SimpleNamespace(explain=lambda _value: expected)
    service = ExplanationService(lambda: None, port)

    actual = service._invoke_provider_safely(SimpleNamespace())

    assert actual == expected
    assert actual.error_code == "EXPLANATION_PROVIDER_UNAVAILABLE"
    assert actual.llm_error_code == "LLM_AUTHENTICATION_FAILED"
