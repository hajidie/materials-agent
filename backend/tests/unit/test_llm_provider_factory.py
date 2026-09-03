from __future__ import annotations

from pydantic import SecretStr

from materialsagent.infrastructure.llm.configuration import ConfiguredRole


def _role(provider: str, role: str, **overrides: object) -> ConfiguredRole:
    values: dict[str, object] = {
        "role": role,
        "provider": provider,
        "model_name": "provider-model",
        "api_key": SecretStr("test-key"),
        "endpoint": (
            "https://api.deepseek.com"
            if provider == "deepseek"
            else "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "timeout_seconds": 60,
        "temperature": 0,
        "top_p": None,
        "top_k": None,
        "max_tokens": 1024,
        "reasoning_mode": "disabled",
        "reasoning_effort": None,
        "thinking_budget": None,
        "response_format": (
            "text" if role == "tool_result_explanation" else "json_object"
        ),
        "streaming": False,
    }
    values.update(overrides)
    return ConfiguredRole(**values)  # type: ignore[arg-type]


def test_deepseek_factory_receives_only_controlled_parameters() -> None:
    from materialsagent.infrastructure.llm.factory import create_chat_model

    captured: list[dict[str, object]] = []

    def fake(**kwargs: object) -> object:
        captured.append(kwargs)
        return object()

    config = _role(
        "deepseek",
        "chat_orchestration",
        top_p=0.8,
        reasoning_mode="disabled",
    )
    created = create_chat_model(config, deepseek_factory=fake)

    assert created is not None
    assert captured == [
        {
            "model": "provider-model",
            "api_key": config.api_key,
            "base_url": "https://api.deepseek.com",
            "timeout": 60,
            "max_retries": 0,
            "include_response_headers": True,
            "streaming": False,
            "temperature": 0,
            "top_p": 0.8,
            "max_tokens": 1024,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    ]


def test_qwen_reasoning_extensions_and_streaming_are_controlled() -> None:
    from materialsagent.infrastructure.llm.factory import create_chat_model

    captured: list[dict[str, object]] = []

    def fake(**kwargs: object) -> object:
        captured.append(kwargs)
        return object()

    config = _role(
        "qwen",
        "tool_result_explanation",
        temperature=None,
        top_p=0.9,
        top_k=20,
        max_tokens=768,
        reasoning_mode="enabled",
        reasoning_effort="high",
        thinking_budget=4096,
        streaming=True,
    )
    create_chat_model(config, qwen_factory=fake)

    assert captured[0]["base_url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert captured[0]["max_retries"] == 0
    assert captured[0]["streaming"] is True
    assert captured[0]["reasoning_effort"] == "high"
    assert "temperature" not in captured[0]
    assert captured[0]["extra_body"] == {
        "enable_thinking": True,
        "top_k": 20,
        "thinking_budget": 4096,
    }


def test_three_roles_create_independent_model_instances() -> None:
    from materialsagent.infrastructure.llm.factory import create_chat_model

    created: list[object] = []

    def fake(**_kwargs: object) -> object:
        instance = object()
        created.append(instance)
        return instance

    roles = (
        _role("deepseek", "chat_orchestration"),
        _role("deepseek", "tool_input_extraction"),
        _role(
            "deepseek",
            "tool_result_explanation",
            max_tokens=768,
        ),
    )
    resolved = [
        create_chat_model(role, deepseek_factory=fake) for role in roles
    ]

    assert resolved == created
    assert len({id(item) for item in resolved}) == 3
