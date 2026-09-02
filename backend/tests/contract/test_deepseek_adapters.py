from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import socket

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
from materialsagent.domain.ports.tool_registry import RoutingCatalogEntry, RoutingCatalogSnapshot
from materialsagent.domain.ports.tool_input_extraction import ToolInputExtractionInput
from materialsagent.domain.ports.tool_registry import ToolRef
from materialsagent.infrastructure.config import DeepSeekConfig


def _config() -> DeepSeekConfig:
    return DeepSeekConfig(
        api_key=SecretStr("test-key"), model_name="deepseek-v4-flash",
        base_url="https://api.deepseek.com", timeout_seconds=4,
    )


def _chat_input(content: str = "请判断该请求。") -> ChatOrchestrationInput:
    return ChatOrchestrationInput(
        task_id="task_contract", conversation_id="conversation_contract",
        request_id="request_contract", content_text=content,
        routing_catalog=RoutingCatalogSnapshot(entries=(RoutingCatalogEntry(
            tool_id="safe_tool", version="1", schema_hash="a" * 64,
            display_name="Safe Tool", description="Safe route.",
            input_schema={"type": "object", "properties": {"value": {"type": "number"}}},
            supported_outputs=("result",),
        ),)),
    )


def _explanation_input() -> ExplanationInput:
    return ExplanationInput(
        result_id="result_contract", status="SUCCEEDED", requested_outputs=("mechanical_properties",),
        completed_outputs=("mechanical_properties",), failed_outputs=(),
        data={"mechanical_properties": {"yield_strength_mpa": 900, "elongation_percent": 12}},
        artifacts=(), warnings=(), error=None, process_parameters={},
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


def _raw(content: str, *, request_id: object = "provider-1") -> object:
    return SimpleNamespace(
        content=content,
        usage_metadata={"input_tokens": 5, "output_tokens": 3},
        response_metadata={"request_id": request_id},
        id=None,
    )


def _envelope(parsed: object, *, content: str = '{"ok":true}', parsing_error=None):
    return {"raw": _raw(content), "parsed": parsed, "parsing_error": parsing_error}


class FakeRunnable:
    def __init__(self, response: object = None, exception: Exception | None = None) -> None:
        self.response = response
        self.exception = exception
        self.call_count = 0
        self.messages = None

    def invoke(self, messages):
        self.call_count += 1
        self.messages = messages
        if self.exception is not None:
            raise self.exception
        return self.response


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"route": "KNOWLEDGE_ANSWER", "answer_text": "答案。"}, KnowledgeAnswer),
        ({"route": "TOOL_CANDIDATES", "candidates": [{"tool_id": "safe_tool", "candidate_input": {"value": 3}}]}, ToolCandidateSet),
    ],
)
def test_chat_maps_each_generic_provider_route_once(payload, expected_type) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter, ProviderChatResponse

    parsed = ProviderChatResponse.model_validate(payload)
    model = FakeRunnable(_envelope(parsed))
    outcome = DeepSeekChatAdapter(_config(), structured_runnable=model).orchestrate(_chat_input())
    assert isinstance(outcome.result, expected_type)
    assert model.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"route": "TOOL_EXECUTION", "tool_id": "safe_tool"},
        {"route": "NEEDS_INPUT", "tool_id": "safe_tool"},
        {"route": "TOOL_CANDIDATES", "status": "READY", "candidates": []},
        {"route": "TOOL_CANDIDATES", "candidates": [{"tool_id": "safe_tool", "version": "1", "candidate_input": {}}]},
        {"route": "TOOL_CANDIDATES", "candidates": []},
    ],
)
def test_provider_chat_schema_rejects_legacy_or_privileged_fields(payload) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import ProviderChatResponse

    with pytest.raises(ValidationError):
        ProviderChatResponse.model_validate(payload)


def test_chat_prompt_is_generated_from_snapshot_and_metadata_matches_invocation() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter, ProviderChatResponse

    parsed = ProviderChatResponse.model_validate({"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"})
    model = FakeRunnable(_envelope(parsed))
    adapter = DeepSeekChatAdapter(_config(), structured_runnable=model)
    metadata = adapter.request_metadata(_chat_input())
    outcome = adapter.orchestrate(_chat_input())

    assert outcome.result.route == "KNOWLEDGE_ANSWER"
    assert metadata.prompt_template_version == "5"
    rendered = str(model.messages)
    assert "safe_tool" in rendered
    assert "TOOL_EXECUTION" not in rendered
    assert "NEEDS_INPUT" not in rendered
    assert "Runtime URL" not in rendered


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (_envelope(None, content="not-json", parsing_error=ValueError()), "LLM_INVALID_JSON"),
        (_envelope(None, content='{"route":"bad"}', parsing_error=ValueError()), "LLM_SCHEMA_MISMATCH"),
    ],
)
def test_chat_protocol_failures_are_controlled(response, error_code) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        DeepSeekChatAdapter(_config(), structured_runnable=FakeRunnable(response)).orchestrate(_chat_input())
    assert captured.value.error_code == error_code


@pytest.mark.parametrize(
    ("exception", "error_type"),
    [(httpx.TimeoutException("private"), ChatOrchestrationTimeoutError),
     (ConnectionError("private"), ChatOrchestrationProviderError)],
)
def test_chat_maps_provider_failures_without_leaking_sdk_text(exception, error_type) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter

    with pytest.raises(error_type) as captured:
        DeepSeekChatAdapter(_config(), structured_runnable=FakeRunnable(exception=exception)).orchestrate(_chat_input())
    assert "private" not in captured.value.safe_error_message


@pytest.mark.parametrize(
    "payload",
    [
        {"candidate_input_delta": {}, "tool_id": "other_tool"},
        {"candidate_input_delta": {}, "status": "READY"},
        {"candidate_input_delta": {}, "execution_permission": True},
        {"candidate_input_delta": "not-an-object"},
    ],
)
def test_fixed_tool_extractor_schema_rejects_privileged_or_invalid_fields(
    payload,
) -> None:
    try:
        from materialsagent.infrastructure.llm.deepseek_tool_input import (
            ProviderToolInputExtractionResponse,
        )
    except ModuleNotFoundError:
        pytest.fail("DeepSeek fixed-Tool input extraction is not implemented.")

    with pytest.raises(ValidationError):
        ProviderToolInputExtractionResponse.model_validate(payload)


def test_fixed_tool_extractor_uses_only_bound_context_and_returns_delta() -> None:
    try:
        from materialsagent.infrastructure.llm.deepseek_tool_input import (
            DeepSeekToolInputExtractionAdapter,
            ProviderToolInputExtractionResponse,
        )
    except ModuleNotFoundError:
        pytest.fail("DeepSeek fixed-Tool input extraction is not implemented.")

    parsed = ProviderToolInputExtractionResponse.model_validate(
        {
            "candidate_input_delta": {
                "aging_temperature": {"value": 730, "unit": "°C"}
            }
        }
    )
    model = FakeRunnable(_envelope(parsed))
    outcome = DeepSeekToolInputExtractionAdapter(
        _config(),
        structured_runnable=model,
    ).extract(_tool_input_extraction_input())

    assert outcome.candidate_input_delta == {
        "aging_temperature": {"value": 730, "unit": "°C"}
    }
    assert outcome.request_metadata.prompt_template_id == "tool-input-extraction"
    rendered = str(model.messages)
    assert "safe_tool" in rendered
    assert "aging_temperature" in rendered
    assert "时效温度 730 °C" in rendered
    assert "other_tool" not in rendered
    assert "full conversation" not in rendered.casefold()


def test_explanation_adapter_preserves_plain_text_contract() -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import DeepSeekExplanationAdapter

    model = FakeRunnable(_raw("第一段。\n第二段。"))
    outcome = DeepSeekExplanationAdapter(_config(), chat_model=model).explain(_explanation_input())
    assert outcome.text == "第一段。 第二段。"
    assert model.call_count == 1


def test_fake_adapters_make_no_socket_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter, ProviderChatResponse
    from materialsagent.infrastructure.llm.deepseek_explanation import DeepSeekExplanationAdapter

    monkeypatch.setattr(socket.socket, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    chat = DeepSeekChatAdapter(
        _config(), structured_runnable=FakeRunnable(_envelope(ProviderChatResponse.model_validate({"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"}))),
    )
    explanation = DeepSeekExplanationAdapter(_config(), chat_model=FakeRunnable(_raw("说明。")))
    assert chat.orchestrate(_chat_input()).result.route == "KNOWLEDGE_ANSWER"
    assert explanation.explain(_explanation_input()).text == "说明。"
