from __future__ import annotations

from dataclasses import replace
import socket
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput,
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    ToolCandidateSet,
)
from materialsagent.domain.ports.explanation import ExplanationInput
from materialsagent.domain.ports.conversation_context import (
    ContextTurn,
    PromptContextWindow,
)
from materialsagent.domain.ports.tool_input_extraction import (
    ToolInputExtractionInput,
)
from materialsagent.domain.ports.tool_registry import (
    RoutingCatalogEntry,
    RoutingCatalogSnapshot,
    ToolRef,
)
from materialsagent.infrastructure.llm.configuration import ConfiguredRole


def _config(
    role: str,
    provider: str = "deepseek",
) -> ConfiguredRole:
    structured = role != "tool_result_explanation"
    return ConfiguredRole(
        role=role,
        provider=provider,  # type: ignore[arg-type]
        api_key=SecretStr("test-key"),
        model_name=("deepseek-v4-flash" if provider == "deepseek" else "qwen-test"),
        endpoint=(
            "https://api.deepseek.com"
            if provider == "deepseek"
            else "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        timeout_seconds=4,
        temperature=0,
        top_p=None,
        top_k=None,
        max_tokens=1024 if structured else 768,
        reasoning_mode="disabled",
        reasoning_effort=None,
        thinking_budget=None,
        response_format="json_object" if structured else "text",
        streaming=False,
        context_window_tokens=1_000_000,
        prompt_limit_tokens=(16_384 if role == "chat_orchestration" else 8_192),
        history_token_budget=(
            8_192
            if role == "chat_orchestration"
            else 4_096
            if role == "tool_input_extraction"
            else 0
        ),
        safety_margin_tokens=1_024,
    )


def _chat_input(content: str = "请判断该请求。") -> ChatOrchestrationInput:
    return ChatOrchestrationInput(
        task_id="task_contract",
        conversation_id="conversation_contract",
        request_id="request_contract",
        content_text=content,
        routing_catalog=RoutingCatalogSnapshot(
            entries=(
                RoutingCatalogEntry(
                    tool_id="safe_tool",
                    version="1",
                    schema_hash="a" * 64,
                    display_name="Safe Tool",
                    description="Safe route.",
                    input_schema={
                        "type": "object",
                        "properties": {"value": {"type": "number"}},
                    },
                    supported_outputs=("result",),
                ),
            )
        ),
    )


def _explanation_input() -> ExplanationInput:
    return ExplanationInput(
        result_id="result_contract",
        status="SUCCEEDED",
        requested_outputs=("mechanical_properties",),
        completed_outputs=("mechanical_properties",),
        failed_outputs=(),
        data={
            "mechanical_properties": {
                "yield_strength_mpa": 900,
                "elongation_percent": 12,
            }
        },
        artifacts=(),
        warnings=(),
        error=None,
        process_parameters={},
        tool_id="zta35g_sem_virtual_lab",
        tool_version="1",
        schema_hash=(
            "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"
        ),
    )


def _tool_input_extraction_input() -> ToolInputExtractionInput:
    return ToolInputExtractionInput(
        content_text="时效温度 730 °C",
        tool_context_ref=ToolRef("safe_tool", "1", "a" * 64),
        candidate_input_schema={
            "type": "object",
            "properties": {
                "aging_temperature": {
                    "anyOf": [{"type": "null"}, {"type": "object"}]
                }
            },
        },
        missing_fields=("aging_temperature",),
        ambiguous_fields=(),
    )


def _raw(
    content: str,
    *,
    request_id: object = "provider-1",
    reasoning_content: str | None = None,
) -> object:
    additional_kwargs = {}
    if reasoning_content is not None:
        additional_kwargs["reasoning_content"] = reasoning_content
    return SimpleNamespace(
        content=content,
        usage_metadata={"input_tokens": 5, "output_tokens": 3},
        response_metadata={"request_id": request_id},
        additional_kwargs=additional_kwargs,
        id=None,
    )


def _envelope(
    parsed: object,
    *,
    content: str = '{"ok":true}',
    parsing_error: object = None,
    reasoning_content: str | None = None,
) -> dict[str, object]:
    return {
        "raw": _raw(content, reasoning_content=reasoning_content),
        "parsed": parsed,
        "parsing_error": parsing_error,
    }


class FakeRunnable:
    def __init__(
        self,
        response: object = None,
        exception: Exception | None = None,
    ) -> None:
        self.response = response
        self.exception = exception
        self.call_count = 0
        self.messages = None

    def invoke(self, messages: object) -> object:
        self.call_count += 1
        self.messages = messages
        if self.exception is not None:
            raise self.exception
        return self.response


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"route": "KNOWLEDGE_ANSWER", "answer_text": "答案。"}, KnowledgeAnswer),
        (
            {
                "route": "TOOL_CANDIDATES",
                "candidates": [
                    {
                        "tool_id": "safe_tool",
                        "proposed_arguments": {"value": 3},
                    }
                ],
            },
            ToolCandidateSet,
        ),
    ],
)
def test_chat_maps_each_provider_route_once(
    provider: str,
    payload: dict[str, object],
    expected_type: type[object],
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
        ProviderChatResponse,
    )

    parsed = ProviderChatResponse.model_validate(payload)
    model = FakeRunnable(_envelope(parsed))
    adapter = LangChainChatOrchestrationAdapter(
        _config("chat_orchestration", provider),
        structured_runnable=model,
    )
    outcome = adapter.orchestrate(_chat_input())

    assert isinstance(outcome.result, expected_type)
    assert outcome.provider_request_id == "provider-1"
    assert model.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"route": "TOOL_EXECUTION", "tool_id": "safe_tool"},
        {"route": "NEEDS_INPUT", "tool_id": "safe_tool"},
        {"route": "TOOL_CANDIDATES", "status": "READY", "candidates": []},
        {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": "safe_tool",
                    "version": "1",
                    "candidate_input": {},
                }
            ],
        },
        {"route": "TOOL_CANDIDATES", "candidates": []},
    ],
)
def test_provider_chat_schema_rejects_legacy_or_privileged_fields(
    payload: dict[str, object],
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        ProviderChatResponse,
    )

    with pytest.raises(ValidationError):
        ProviderChatResponse.model_validate(payload)


def test_chat_prompt_metadata_matches_the_single_render_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm.common import canonical_prompt_digest
    from materialsagent.infrastructure.llm import langchain_chat
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
        ProviderChatResponse,
    )

    render_calls = 0
    original_render = langchain_chat.render_chat_orchestration_prompt

    def counted_render(value: ChatOrchestrationInput) -> list[dict[str, str]]:
        nonlocal render_calls
        render_calls += 1
        return original_render(value)

    monkeypatch.setattr(
        langchain_chat,
        "render_chat_orchestration_prompt",
        counted_render,
    )

    parsed = ProviderChatResponse.model_validate(
        {"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"}
    )
    model = FakeRunnable(_envelope(parsed))
    adapter = LangChainChatOrchestrationAdapter(
        _config("chat_orchestration"),
        structured_runnable=model,
    )
    request = _chat_input()
    metadata = adapter.request_metadata(request)
    outcome = adapter.orchestrate(request)

    assert outcome.result.route == "KNOWLEDGE_ANSWER"
    assert render_calls == 1
    assert metadata.prompt_template_version == "6"
    assert metadata.generation_parameters["schema_version"] == 1
    assert metadata.generation_parameters["tool_calling_mode"] == "structured"
    assert metadata.prompt_digest == canonical_prompt_digest(
        template_id=metadata.prompt_template_id,
        template_version=metadata.prompt_template_version,
        messages=model.messages,
    )
    rendered = str(model.messages)
    assert "safe_tool" in rendered
    assert "proposal_schema" in rendered
    assert "candidate_input_schema" not in rendered
    assert "schema_hash" not in rendered
    assert "required_permissions" not in rendered
    assert "Runtime URL" not in rendered


def test_chat_adapter_preserves_history_order_and_counts_structured_schema() -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
        ProviderChatResponse,
    )
    from materialsagent.infrastructure.llm.prompts import (
        render_chat_orchestration_prompt,
    )
    from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter

    parsed = ProviderChatResponse.model_validate(
        {"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"}
    )
    model = FakeRunnable(_envelope(parsed))
    adapter = LangChainChatOrchestrationAdapter(
        _config("chat_orchestration"),
        structured_runnable=model,
    )
    request = replace(
        _chat_input("current user"),
        context_window=PromptContextWindow(
            recent_turns=(
                ContextTurn(
                    "historical user",
                    '{"context_ref":"ctx_ref_0001","kind":"history"}',
                ),
            )
        ),
    )
    rendered = render_chat_orchestration_prompt(request)

    adapter.orchestrate(request)

    assert [message["role"] for message in model.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert model.messages == rendered
    message_tokens = Cl100kTokenCounter().count_messages(rendered)
    assert adapter.count_prompt_tokens(request) > message_tokens


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (
            _envelope(None, content="not-json", parsing_error=ValueError()),
            "LLM_INVALID_JSON",
        ),
        (
            _envelope(
                None,
                content='{"route":"bad"}',
                parsing_error=ValueError(),
            ),
            "LLM_SCHEMA_MISMATCH",
        ),
    ],
)
def test_chat_protocol_failures_are_controlled(
    response: object,
    error_code: str,
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
    )

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        LangChainChatOrchestrationAdapter(
            _config("chat_orchestration"),
            structured_runnable=FakeRunnable(response),
        ).orchestrate(_chat_input())
    assert captured.value.error_code == error_code


@pytest.mark.parametrize(
    ("exception", "error_type"),
    [
        (httpx.TimeoutException("private"), ChatOrchestrationTimeoutError),
        (ConnectionError("private"), ChatOrchestrationProviderError),
    ],
)
def test_chat_maps_provider_failures_without_sdk_text(
    exception: Exception,
    error_type: type[Exception],
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
    )

    with pytest.raises(error_type) as captured:
        LangChainChatOrchestrationAdapter(
            _config("chat_orchestration"),
            structured_runnable=FakeRunnable(exception=exception),
        ).orchestrate(_chat_input())
    assert "private" not in captured.value.safe_error_message


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "LLM_AUTHENTICATION_FAILED"),
        (402, "LLM_BALANCE_EXHAUSTED"),
        (429, "LLM_RATE_LIMITED"),
        (503, "LLM_PROVIDER_UNAVAILABLE"),
    ],
)
def test_provider_http_failures_have_provider_neutral_safe_codes(
    status_code: int,
    expected_code: str,
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
    )

    class FakeProviderError(Exception):
        def __init__(self) -> None:
            super().__init__("private provider response")
            self.status_code = status_code
            self.request_id = "failed-request-1"

    with pytest.raises(ChatOrchestrationProviderError) as captured:
        LangChainChatOrchestrationAdapter(
            _config("chat_orchestration", "qwen"),
            structured_runnable=FakeRunnable(exception=FakeProviderError()),
        ).orchestrate(_chat_input())

    assert captured.value.error_code == expected_code
    assert captured.value.provider_request_id == "failed-request-1"
    assert "private provider response" not in captured.value.safe_error_message


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_fixed_tool_extractor_uses_bound_context_and_returns_delta(
    provider: str,
) -> None:
    from materialsagent.infrastructure.llm.langchain_tool_input import (
        LangChainToolInputExtractionAdapter,
        ProviderToolInputExtractionResponse,
    )

    parsed = ProviderToolInputExtractionResponse.model_validate(
        {
            "candidate_input_delta": {
                "aging_temperature": {"value": 730, "unit": "°C"}
            }
        }
    )
    model = FakeRunnable(_envelope(parsed))
    outcome = LangChainToolInputExtractionAdapter(
        _config("tool_input_extraction", provider),
        structured_runnable=model,
    ).extract(_tool_input_extraction_input())

    assert outcome.candidate_input_delta == {
        "aging_temperature": {"value": 730, "unit": "°C"}
    }
    assert outcome.request_metadata.provider == provider
    assert "safe_tool" in str(model.messages)
    assert model.call_count == 1


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_explanation_preserves_plain_text_and_discards_reasoning(
    provider: str,
) -> None:
    from materialsagent.infrastructure.llm.langchain_explanation import (
        LangChainExplanationAdapter,
    )

    hidden = "private chain of thought"
    model = FakeRunnable(_raw("第一段。\n第二段。", reasoning_content=hidden))
    adapter = LangChainExplanationAdapter(
        _config("tool_result_explanation", provider),
        chat_model=model,
    )
    request = _explanation_input()
    metadata = adapter.request_metadata(request)
    outcome = adapter.explain(request)

    assert outcome.text == "第一段。 第二段。"
    assert hidden not in repr(outcome)
    assert hidden not in repr(metadata)
    assert outcome.provider_request_id == "provider-1"
    assert model.call_count == 1


def test_fake_adapters_make_no_socket_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm.langchain_chat import (
        LangChainChatOrchestrationAdapter,
        ProviderChatResponse,
    )
    from materialsagent.infrastructure.llm.langchain_explanation import (
        LangChainExplanationAdapter,
    )

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Unexpected socket attempt.")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    chat = LangChainChatOrchestrationAdapter(
        _config("chat_orchestration"),
        structured_runnable=FakeRunnable(
            _envelope(
                ProviderChatResponse.model_validate(
                    {"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"}
                )
            )
        ),
    )
    explanation = LangChainExplanationAdapter(
        _config("tool_result_explanation"),
        chat_model=FakeRunnable(_raw("说明。")),
    )

    assert chat.orchestrate(_chat_input()).result.route == "KNOWLEDGE_ANSWER"
    assert explanation.explain(_explanation_input()).text == "说明。"
