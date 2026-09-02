from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.readiness import ReadinessService
from materialsagent.infrastructure.config import (
    ConfigurationError,
    load_settings,
)


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
        "actor_context": ActorContext(
            actor_id="actor_test",
            user_id=None,
        ),
    }
    options.update(overrides)
    return create_app(**options)


def test_mock_wiring_does_not_construct_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm import (
        deepseek_chat,
        deepseek_explanation,
    )
    from materialsagent.infrastructure.llm.mock import (
        MockChatOrchestrationAdapter,
    )
    from materialsagent.infrastructure.llm.mock_explanation import (
        MockExplanationAdapter,
    )

    def forbidden_model(**_kwargs: object) -> object:
        raise AssertionError("DeepSeek model must remain lazy in Mock mode.")

    monkeypatch.setattr(deepseek_chat, "ChatDeepSeek", forbidden_model)
    monkeypatch.setattr(
        deepseek_explanation,
        "ChatDeepSeek",
        forbidden_model,
    )

    app = _app(settings=load_settings({"LLM_ADAPTER": "mock"}))

    assert isinstance(
        app.state.chat_orchestration_service._orchestration_port,
        MockChatOrchestrationAdapter,
    )
    assert isinstance(
        app.state.explanation_retry_service._explanation_service._port,
        MockExplanationAdapter,
    )


def test_clean_mock_subprocess_does_not_import_deepseek_modules() -> None:
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
            actor_context=ActorContext(
                actor_id="actor_test",
                user_id=None,
            ),
        )

        forbidden = (
            "materialsagent.infrastructure.llm.deepseek_chat",
            "materialsagent.infrastructure.llm.deepseek_explanation",
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


def test_deepseek_wiring_constructs_two_independent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm import (
        deepseek_chat,
        deepseek_explanation,
    )

    created: list[SimpleNamespace] = []

    class FakeModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.structured_calls = 0
            created.append(self)

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

    monkeypatch.setattr(deepseek_chat, "ChatDeepSeek", FakeModel)
    monkeypatch.setattr(
        deepseek_explanation,
        "ChatDeepSeek",
        FakeModel,
    )
    settings = load_settings(
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-placeholder-never-sent",
        }
    )

    app = _app(settings=settings)

    chat_port = app.state.chat_orchestration_service._orchestration_port
    explanation_port = (
        app.state.explanation_retry_service._explanation_service._port
    )
    assert chat_port.provider == explanation_port.provider == "deepseek"
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {item.kwargs["max_tokens"] for item in created} == {1024, 768}
    assert sum(item.structured_calls for item in created) == 1


def test_deepseek_missing_key_fails_closed_without_fallback() -> None:
    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid DeepSeek configuration\.$",
    ):
        _app(settings=load_settings({"LLM_ADAPTER": "deepseek"}))


def test_explicit_ports_take_priority_without_deepseek_key() -> None:
    explicit_chat = SimpleNamespace(
        provider="explicit",
        model_name="explicit-chat",
    )
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
        settings=load_settings({"LLM_ADAPTER": "deepseek"}),
        chat_orchestration_port=explicit_chat,
        tool_input_extraction_port=explicit_tool_input,
        explanation_port=explicit_explanation,
    )

    assert (
        app.state.chat_orchestration_service._orchestration_port
        is explicit_chat
    )
    assert (
        app.state.chat_orchestration_service._tool_input_extraction_port
        is explicit_tool_input
    )
    assert (
        app.state.explanation_retry_service._explanation_service._port
        is explicit_explanation
    )
