from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.readiness import ReadinessService
from materialsagent.infrastructure.config import ConfigurationError, load_settings


def _readiness() -> ReadinessService:
    return ReadinessService(
        postgresql_probe=lambda: True,
        object_storage_probe=lambda: True,
    )


def _app(**overrides: object):
    from materialsagent.main import create_app

    options: dict[str, object] = {
        "settings": load_settings({}),
        "readiness_service": _readiness(),
        "unit_of_work_factory": lambda: None,
        "actor_context": ActorContext(actor_id="actor_test", user_id=None),
    }
    options.update(overrides)
    return create_app(**options)


def test_mock_wiring_uses_all_three_mock_adapters() -> None:
    from materialsagent.infrastructure.llm.mock import MockChatOrchestrationAdapter
    from materialsagent.infrastructure.llm.mock_explanation import (
        MockExplanationAdapter,
    )
    from materialsagent.infrastructure.llm.mock_tool_input import (
        MockToolInputExtractionAdapter,
    )

    app = _app(settings=load_settings({"LLM_ADAPTER": "mock"}))

    service = app.state.chat_orchestration_service
    assert isinstance(service._orchestration_port, MockChatOrchestrationAdapter)
    assert isinstance(
        service._tool_input_extraction_port,
        MockToolInputExtractionAdapter,
    )
    assert isinstance(
        app.state.explanation_retry_service._explanation_service._port,
        MockExplanationAdapter,
    )


def test_clean_mock_subprocess_does_not_import_provider_modules() -> None:
    script = textwrap.dedent(
        """
        import sys

        from materialsagent.application.context import ActorContext
        from materialsagent.application.readiness import ReadinessService
        from materialsagent.infrastructure.config import load_settings
        from materialsagent.main import create_app

        create_app(
            settings=load_settings({"LLM_ADAPTER": "mock"}),
            readiness_service=ReadinessService(
                postgresql_probe=lambda: True,
                object_storage_probe=lambda: True,
            ),
            unit_of_work_factory=lambda: None,
            actor_context=ActorContext(actor_id="actor_test", user_id=None),
        )

        forbidden = (
            "materialsagent.infrastructure.llm.factory",
            "materialsagent.infrastructure.llm.langchain_chat",
            "materialsagent.infrastructure.llm.langchain_tool_input",
            "materialsagent.infrastructure.llm.langchain_explanation",
        )
        assert all(name not in sys.modules for name in forbidden)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_provider_wiring_constructs_three_independent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm import factory

    created: list[SimpleNamespace] = []

    class FakeModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.structured_calls = 0
            created.append(self)  # type: ignore[arg-type]

        def with_structured_output(
            self,
            _schema: object,
            **_kwargs: object,
        ) -> object:
            self.structured_calls += 1
            return SimpleNamespace(invoke=lambda _value: None)

        def invoke(self, _value: object) -> object:
            return SimpleNamespace(
                content="safe",
                usage_metadata=None,
                response_metadata={},
            )

    monkeypatch.setattr(factory, "ChatDeepSeek", FakeModel)
    app = _app(
        settings=load_settings(
            {
                "LLM_ADAPTER": "provider",
                "DEEPSEEK_API_KEY": "test-placeholder-never-sent",
            }
        )
    )

    chat_service = app.state.chat_orchestration_service
    chat_port = chat_service._orchestration_port
    tool_input_port = chat_service._tool_input_extraction_port
    explanation_port = (
        app.state.explanation_retry_service._explanation_service._port
    )
    assert {
        chat_port.provider,
        tool_input_port.provider,
        explanation_port.provider,
    } == {"deepseek"}
    assert len(created) == 3
    assert len({id(item) for item in created}) == 3
    assert sorted(item.kwargs["max_tokens"] for item in created) == [768, 1024, 1024]
    assert sum(item.structured_calls for item in created) == 2


def test_provider_missing_selected_key_fails_closed_without_fallback() -> None:
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        _app(settings=load_settings({"LLM_ADAPTER": "provider"}))


def test_explicit_ports_take_priority_without_provider_key() -> None:
    explicit_chat = SimpleNamespace(provider="explicit", model_name="explicit-chat")
    explicit_tool_input = SimpleNamespace(
        provider="explicit",
        model_name="explicit-tool-input",
        extract=lambda _command: None,
    )
    explicit_explanation = SimpleNamespace(
        provider="explicit",
        model_name="explicit-explanation",
    )

    app = _app(
        settings=load_settings({"LLM_ADAPTER": "provider"}),
        chat_orchestration_port=explicit_chat,
        tool_input_extraction_port=explicit_tool_input,
        explanation_port=explicit_explanation,
    )

    assert app.state.chat_orchestration_service._orchestration_port is explicit_chat
    assert (
        app.state.chat_orchestration_service._tool_input_extraction_port
        is explicit_tool_input
    )
    assert (
        app.state.explanation_retry_service._explanation_service._port
        is explicit_explanation
    )
