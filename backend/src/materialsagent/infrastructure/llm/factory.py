from __future__ import annotations

from collections.abc import Callable

from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from materialsagent.infrastructure.llm.configuration import ConfiguredRole


def provider_client_kwargs(config: ConfiguredRole) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "model": config.model_name,
        "api_key": config.api_key,
        "base_url": config.endpoint,
        "timeout": config.timeout_seconds,
        "max_retries": 0,
        "include_response_headers": True,
        "streaming": config.streaming,
    }
    if config.streaming:
        kwargs["stream_usage"] = True
    for name in ("temperature", "top_p", "max_tokens"):
        value = getattr(config, name)
        if value is not None:
            kwargs[name] = value
    if config.reasoning_effort is not None:
        kwargs["reasoning_effort"] = config.reasoning_effort
    extra_body: dict[str, object] = {}
    if config.provider == "deepseek":
        if config.reasoning_mode is not None:
            extra_body["thinking"] = {"type": config.reasoning_mode}
    else:
        if config.reasoning_mode is not None:
            extra_body["enable_thinking"] = config.reasoning_mode == "enabled"
        if config.top_k is not None:
            extra_body["top_k"] = config.top_k
        if config.thinking_budget is not None:
            extra_body["thinking_budget"] = config.thinking_budget
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def create_chat_model(
    config: ConfiguredRole,
    *,
    deepseek_factory: Callable[..., object] | None = None,
    qwen_factory: Callable[..., object] | None = None,
) -> object:
    if config.provider == "deepseek":
        factory = deepseek_factory or ChatDeepSeek
    elif config.provider == "qwen":
        factory = qwen_factory or ChatOpenAI
    else:  # pragma: no cover - ConfiguredRole is produced by strict parsing.
        raise ValueError("Unsupported LLM provider.")
    return factory(**provider_client_kwargs(config))
