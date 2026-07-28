from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from pydantic import SecretStr, ValidationError

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput,
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    NeedsInputCandidate,
    ToolCandidate,
)
from materialsagent.domain.ports.explanation import (
    ExplanationAssetReference,
    ExplanationInput,
)
from materialsagent.infrastructure.config import DeepSeekConfig


def _config() -> DeepSeekConfig:
    return DeepSeekConfig(
        api_key=SecretStr("test-placeholder-never-sent"),
        model_name="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        timeout_seconds=60.0,
    )


def _chat_input(content: str = "请判断该请求。") -> ChatOrchestrationInput:
    return ChatOrchestrationInput(
        task_id="task_1",
        conversation_id="conversation_1",
        request_id="request_1",
        content_text=content,
    )


def _explanation_input() -> ExplanationInput:
    return ExplanationInput(
        result_id="result_1",
        status="SUCCEEDED",
        requested_outputs=("sem_image", "mechanical_properties"),
        completed_outputs=("sem_image", "mechanical_properties"),
        failed_outputs=(),
        data={
            "yield_strength": {"value": 650.0, "unit": "MPa"},
            "elongation": {"value": 3.2, "unit": "%"},
        },
        artifacts=(
            ExplanationAssetReference(
                asset_id="asset_1",
                role="requested_output",
                asset_type="sem_image",
            ),
        ),
        warnings=(),
        error=None,
        process_parameters={
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        tool_id="zta35g_sem_virtual_lab",
        tool_version="0.1.0",
        schema_version="1.0",
    )


class FakeRunnable:
    def __init__(
        self,
        response: object = None,
        *,
        exception: Exception | None = None,
    ) -> None:
        self.response = response
        self.exception = exception
        self.call_count = 0
        self.inputs: list[object] = []

    def invoke(self, value: object) -> object:
        self.call_count += 1
        self.inputs.append(value)
        if self.exception is not None:
            raise self.exception
        return self.response


def _raw(
    *,
    content: str = '{"route":"KNOWLEDGE_ANSWER"}',
    usage: object = None,
    headers: object = None,
    raw_id: str = "completion-must-not-be-persisted",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        usage_metadata=usage,
        response_metadata={
            "headers": {} if headers is None else headers,
            "id": "metadata-completion-must-not-be-persisted",
        },
        id=raw_id,
    )


def _chat_envelope(
    parsed: object,
    *,
    raw: object | None = None,
    parsing_error: Exception | None = None,
) -> dict[str, object]:
    return {
        "raw": _raw() if raw is None else raw,
        "parsed": parsed,
        "parsing_error": parsing_error,
    }


@pytest.mark.parametrize(
    ("payload", "result_type"),
    [
        (
            {
                "route": "KNOWLEDGE_ANSWER",
                "answer_text": "ZTA35G 是氧化锆增韧氧化铝复合材料。",
            },
            KnowledgeAnswer,
        ),
        (
            {
                "route": "TOOL_EXECUTION",
                "tool_id": "zta35g_sem_virtual_lab",
                "material": "ZTA35G",
                "candidate_parameters": {
                    "solution_temperature": {"value": 1000, "unit": "°C"},
                    "solution_time": {"value": 3, "unit": "h"},
                    "aging_temperature": {"value": 730, "unit": "°C"},
                    "aging_time": {"value": 3, "unit": "h"},
                },
                "requested_outputs": ["sem_image", "mechanical_properties"],
            },
            ToolCandidate,
        ),
        (
            {
                "route": "NEEDS_INPUT",
                "tool_id": "zta35g_sem_virtual_lab",
                "material": "ZTA35G",
                "candidate_parameters": {
                    "solution_temperature": {"value": 1000, "unit": "°C"},
                    "solution_time": {"value": 3, "unit": "h"},
                    "aging_temperature": None,
                    "aging_time": {"value": 3, "unit": "h"},
                },
                "missing_fields": ["aging_temperature"],
                "ambiguous_fields": [],
                "follow_up_suggestion": "请补充时效温度。",
                "requested_outputs": ["sem_image"],
            },
            NeedsInputCandidate,
        ),
    ],
)
def test_chat_maps_each_strict_provider_route_once(
    payload: dict[str, object],
    result_type: type[object],
) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
        ProviderChatResponse,
    )

    parsed = ProviderChatResponse.model_validate(payload)
    runnable = FakeRunnable(
        _chat_envelope(
            parsed,
            raw=_raw(
                usage={
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "total_tokens": 16,
                },
                headers={
                    "X-Request-ID": "provider-request-1",
                    "Authorization": "must-not-be-copied",
                },
            ),
        )
    )

    outcome = DeepSeekChatAdapter(
        _config(),
        structured_runnable=runnable,
    ).orchestrate(_chat_input())

    assert isinstance(outcome.result, result_type)
    assert outcome.usage == {"input_tokens": 12, "output_tokens": 4}
    assert outcome.provider_request_id == "provider-request-1"
    assert runnable.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "safe",
            "extra": "forbidden",
        },
        {"route": "UNKNOWN", "answer_text": "safe"},
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "unknown",
            "material": "ZTA35G",
            "candidate_parameters": {
                "solution_temperature": None,
                "solution_time": None,
                "aging_temperature": None,
                "aging_time": None,
            },
            "requested_outputs": ["sem_image"],
        },
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": {
                "solution_temperature": None,
                "solution_time": None,
                "aging_temperature": None,
            },
            "requested_outputs": ["sem_image"],
        },
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": {
                "solution_temperature": None,
                "solution_time": None,
                "aging_temperature": None,
                "aging_time": None,
            },
            "requested_outputs": ["unknown"],
        },
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": {"nested": "forbidden"},
            "candidate_parameters": {
                "solution_temperature": None,
                "solution_time": None,
                "aging_temperature": None,
                "aging_time": None,
            },
            "requested_outputs": ["sem_image"],
        },
    ],
)
def test_provider_chat_schema_is_strict(payload: dict[str, object]) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        ProviderChatResponse,
    )

    with pytest.raises(ValidationError):
        ProviderChatResponse.model_validate(payload)


def test_chat_model_configuration_and_structured_output_boundary() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
        ProviderChatResponse,
    )

    structured = FakeRunnable()

    class FakeModel:
        def __init__(self) -> None:
            self.structured_calls: list[tuple[object, dict[str, object]]] = []

        def with_structured_output(
            self,
            schema: object,
            **kwargs: object,
        ) -> FakeRunnable:
            self.structured_calls.append((schema, kwargs))
            return structured

    model = FakeModel()
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> FakeModel:
        captured.update(kwargs)
        return model

    DeepSeekChatAdapter(_config(), model_factory=factory)

    assert captured == {
        "model": "deepseek-v4-flash",
        "api_key": _config().api_key,
        "base_url": "https://api.deepseek.com",
        "timeout": 60.0,
        "max_retries": 0,
        "temperature": 0,
        "max_tokens": 1024,
        "streaming": False,
        "include_response_headers": True,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert model.structured_calls == [
        (
            ProviderChatResponse,
            {"method": "json_mode", "include_raw": True},
        )
    ]


def test_chat_metadata_is_pure_and_matches_invoked_prompt() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
        ProviderChatResponse,
    )

    runnable = FakeRunnable(
        _chat_envelope(
            ProviderChatResponse.model_validate(
                {
                    "route": "KNOWLEDGE_ANSWER",
                    "answer_text": "safe",
                }
            )
        )
    )
    adapter = DeepSeekChatAdapter(_config(), structured_runnable=runnable)

    first = adapter.request_metadata(_chat_input())
    second = adapter.request_metadata(_chat_input())
    changed = adapter.request_metadata(_chat_input("不同输入"))
    outcome = adapter.orchestrate(_chat_input())

    assert first == second
    assert first.prompt_template_id == "chat-orchestration"
    assert first.prompt_template_version == "2"
    assert first.prompt_digest != changed.prompt_digest
    assert first.generation_parameters == {
        "temperature": 0,
        "max_tokens": 1024,
        "thinking_mode": "disabled",
        "response_format": "json_object",
        "streaming": False,
    }
    assert runnable.call_count == 1
    assert runnable.inputs[0]
    rendered = json.dumps(runnable.inputs[0], ensure_ascii=False)
    assert "JSON" in rendered
    assert "NEEDS_INPUT" in rendered
    assert outcome.result.route == "KNOWLEDGE_ANSWER"


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (
            _chat_envelope(
                None,
                raw=_raw(content=""),
            ),
            "LLM_EMPTY_RESPONSE",
        ),
        (
            _chat_envelope(
                None,
                raw=_raw(content="{invalid-json"),
                parsing_error=json.JSONDecodeError("bad", "x", 0),
            ),
            "LLM_INVALID_JSON",
        ),
        (
            _chat_envelope(None),
            "LLM_SCHEMA_MISMATCH",
        ),
        (
            _chat_envelope(
                {"route": "KNOWLEDGE_ANSWER", "answer_text": 1}
            ),
            "LLM_SCHEMA_MISMATCH",
        ),
    ],
)
def test_chat_protocol_failures_are_static_and_single_call(
    response: object,
    error_code: str,
) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    runnable = FakeRunnable(response)

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        DeepSeekChatAdapter(
            _config(),
            structured_runnable=runnable,
        ).orchestrate(_chat_input())

    assert captured.value.error_code == error_code
    assert "bad" not in captured.value.safe_error_message
    assert runnable.call_count == 1


def test_output_parser_exception_with_invalid_raw_json_is_invalid_json() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    raw_content = "{invalid-provider-json"
    runnable = FakeRunnable(
        _chat_envelope(
            None,
            raw=_raw(content=raw_content),
            parsing_error=OutputParserException(
                "sensitive-parser-detail",
                llm_output=raw_content,
            ),
        )
    )

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        DeepSeekChatAdapter(
            _config(),
            structured_runnable=runnable,
        ).orchestrate(_chat_input())

    assert captured.value.error_code == "LLM_INVALID_JSON"
    assert raw_content not in captured.value.safe_error_message
    assert "sensitive-parser-detail" not in captured.value.safe_error_message
    assert runnable.call_count == 1


def test_output_parser_exception_with_valid_json_is_schema_mismatch() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    raw_content = '{"route":"KNOWLEDGE_ANSWER","answer_text":1}'
    runnable = FakeRunnable(
        _chat_envelope(
            None,
            raw=_raw(content=raw_content),
            parsing_error=OutputParserException(
                "sensitive-schema-detail",
                llm_output=raw_content,
            ),
        )
    )

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        DeepSeekChatAdapter(
            _config(),
            structured_runnable=runnable,
        ).orchestrate(_chat_input())

    assert captured.value.error_code == "LLM_SCHEMA_MISMATCH"
    assert raw_content not in captured.value.safe_error_message
    assert "sensitive-schema-detail" not in captured.value.safe_error_message
    assert runnable.call_count == 1


def test_direct_json_decode_error_is_invalid_json_and_single_call() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    raw_content = "{direct-invalid-json"
    runnable = FakeRunnable(
        exception=json.JSONDecodeError(
            "sensitive-direct-parser-detail",
            raw_content,
            0,
        )
    )

    with pytest.raises(ChatOrchestrationProtocolError) as captured:
        DeepSeekChatAdapter(
            _config(),
            structured_runnable=runnable,
        ).orchestrate(_chat_input())

    assert captured.value.error_code == "LLM_INVALID_JSON"
    assert raw_content not in captured.value.safe_error_message
    assert "sensitive-direct-parser-detail" not in (
        captured.value.safe_error_message
    )
    assert runnable.call_count == 1


def test_domain_mapping_uses_explicit_required_value_validation() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        ProviderChatResponse,
        _domain_result,
    )

    invalid = ProviderChatResponse.model_construct(
        route="TOOL_EXECUTION",
        tool_id=None,
        material="ZTA35G",
        candidate_parameters=None,
        requested_outputs=None,
    )

    with pytest.raises(ValueError, match="required values"):
        _domain_result(invalid)


class FakeStatusError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        *,
        request_id: str | None = "failure-request-1",
    ) -> None:
        super().__init__("sensitive-sdk-error-body")
        self.status_code = status_code
        self.request_id = request_id


@pytest.mark.parametrize(
    ("exception", "error_type", "error_code"),
    [
        (
            httpx.TimeoutException("sensitive-timeout"),
            ChatOrchestrationTimeoutError,
            "LLM_TIMEOUT",
        ),
        (
            FakeStatusError(401),
            ChatOrchestrationProviderError,
            "LLM_AUTHENTICATION_FAILED",
        ),
        (
            FakeStatusError(402),
            ChatOrchestrationProviderError,
            "LLM_BALANCE_EXHAUSTED",
        ),
        (
            FakeStatusError(429),
            ChatOrchestrationProviderError,
            "LLM_RATE_LIMITED",
        ),
        (
            FakeStatusError(500),
            ChatOrchestrationProviderError,
            "LLM_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(503),
            ChatOrchestrationProviderError,
            "LLM_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(400),
            ChatOrchestrationProviderError,
            "LLM_REQUEST_REJECTED",
        ),
        (
            FakeStatusError(422),
            ChatOrchestrationProviderError,
            "LLM_REQUEST_REJECTED",
        ),
    ],
)
def test_chat_maps_provider_failures_without_sdk_text(
    exception: Exception,
    error_type: type[Exception],
    error_code: str,
) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    runnable = FakeRunnable(exception=exception)

    with pytest.raises(error_type) as captured:
        DeepSeekChatAdapter(
            _config(),
            structured_runnable=runnable,
        ).orchestrate(_chat_input())

    error = captured.value
    assert error.error_code == error_code
    assert error.provider_request_id == (
        None
        if isinstance(exception, httpx.TimeoutException)
        else "failure-request-1"
    )
    assert "sensitive" not in error.safe_error_message
    assert runnable.call_count == 1


def test_completion_ids_and_invalid_metadata_are_not_request_ids() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
        ProviderChatResponse,
    )

    parsed = ProviderChatResponse.model_validate(
        {"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"}
    )
    runnable = FakeRunnable(
        _chat_envelope(
            parsed,
            raw=_raw(
                usage={
                    "input_tokens": True,
                    "output_tokens": 2,
                },
                headers={"x-request-id": "contains a space"},
            ),
        )
    )

    outcome = DeepSeekChatAdapter(
        _config(),
        structured_runnable=runnable,
    ).orchestrate(_chat_input())

    assert outcome.usage is None
    assert outcome.provider_request_id is None


def test_explanation_model_configuration_and_independent_plain_invoke() -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    model = FakeRunnable(
        _raw(
            content="受控说明。",
            usage={"input_tokens": 9, "output_tokens": 3},
            headers={"x-request-id": "explanation-request-1"},
        )
    )
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> FakeRunnable:
        captured.update(kwargs)
        return model

    adapter = DeepSeekExplanationAdapter(_config(), model_factory=factory)
    outcome = adapter.explain(_explanation_input())

    assert captured["max_tokens"] == 768
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["max_retries"] == 0
    assert captured["streaming"] is False
    assert captured["include_response_headers"] is True
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert outcome.text == "受控说明。"
    assert outcome.usage == {"input_tokens": 9, "output_tokens": 3}
    assert outcome.provider_request_id == "explanation-request-1"
    assert model.call_count == 1


def test_explanation_metadata_is_pure_and_uses_v2_prompt() -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    model = FakeRunnable(_raw(content="受控说明。"))
    adapter = DeepSeekExplanationAdapter(_config(), chat_model=model)

    first = adapter.request_metadata(_explanation_input())
    second = adapter.request_metadata(_explanation_input())
    adapter.explain(_explanation_input())

    assert first == second
    assert first.prompt_template_id == "tool-result-explanation"
    assert first.prompt_template_version == "2"
    assert first.generation_parameters == {
        "temperature": 0,
        "max_tokens": 768,
        "thinking_mode": "disabled",
        "response_format": "text",
        "streaming": False,
    }
    assert model.inputs[0]


def test_explanation_normalizes_multiline_text_without_second_call() -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    model = FakeRunnable(
        _raw(content="第一段。\r\n第二段。\t补充说明。")
    )

    outcome = DeepSeekExplanationAdapter(
        _config(),
        chat_model=model,
    ).explain(_explanation_input())

    assert outcome.text == "第一段。 第二段。 补充说明。"
    assert outcome.error_code is None
    assert outcome.llm_error_code is None
    assert model.call_count == 1


@pytest.mark.parametrize(
    ("response", "llm_error_code"),
    [
        (_raw(content=""), "LLM_EMPTY_RESPONSE"),
        (_raw(content="x" * 4097), "LLM_SCHEMA_MISMATCH"),
        (_raw(content="bad\u0000control"), "LLM_SCHEMA_MISMATCH"),
        (
            SimpleNamespace(
                content="",
                additional_kwargs={"reasoning_content": "must-not-be-used"},
                usage_metadata=None,
                response_metadata={},
                id="completion-id",
            ),
            "LLM_EMPTY_RESPONSE",
        ),
    ],
)
def test_explanation_rejects_invalid_or_reasoning_only_text(
    response: object,
    llm_error_code: str,
) -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    model = FakeRunnable(response)
    outcome = DeepSeekExplanationAdapter(
        _config(),
        chat_model=model,
    ).explain(_explanation_input())

    assert outcome.text is None
    assert outcome.error_code == "EXPLANATION_PROTOCOL_ERROR"
    assert outcome.llm_error_code == llm_error_code
    assert "reasoning_content" not in outcome.safe_error_message
    assert "reasoning_content" not in outcome.llm_safe_error_message
    assert model.call_count == 1


@pytest.mark.parametrize(
    ("exception", "llm_error_code", "public_error_code"),
    [
        (
            httpx.TimeoutException("sensitive"),
            "LLM_TIMEOUT",
            "EXPLANATION_TIMEOUT",
        ),
        (
            FakeStatusError(400),
            "LLM_REQUEST_REJECTED",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(401),
            "LLM_AUTHENTICATION_FAILED",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(402),
            "LLM_BALANCE_EXHAUSTED",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(422),
            "LLM_REQUEST_REJECTED",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(429),
            "LLM_RATE_LIMITED",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(500),
            "LLM_PROVIDER_UNAVAILABLE",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            FakeStatusError(503),
            "LLM_PROVIDER_UNAVAILABLE",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
        (
            ConnectionError("sensitive"),
            "LLM_PROVIDER_UNAVAILABLE",
            "EXPLANATION_PROVIDER_UNAVAILABLE",
        ),
    ],
)
def test_explanation_maps_provider_failures_to_safe_outcomes(
    exception: Exception,
    llm_error_code: str,
    public_error_code: str,
) -> None:
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    model = FakeRunnable(exception=exception)
    outcome = DeepSeekExplanationAdapter(
        _config(),
        chat_model=model,
    ).explain(_explanation_input())

    assert outcome.error_code == public_error_code
    assert outcome.llm_error_code == llm_error_code
    assert outcome.text is None
    assert "sensitive" not in outcome.safe_error_message
    assert "sensitive" not in outcome.llm_safe_error_message
    assert model.call_count == 1


def test_fake_adapters_make_no_socket_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
        ProviderChatResponse,
    )
    from materialsagent.infrastructure.llm.deepseek_explanation import (
        DeepSeekExplanationAdapter,
    )

    def forbidden_connect(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Public network access is forbidden.")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
    chat = DeepSeekChatAdapter(
        _config(),
        structured_runnable=FakeRunnable(
            _chat_envelope(
                ProviderChatResponse.model_validate(
                    {
                        "route": "KNOWLEDGE_ANSWER",
                        "answer_text": "safe",
                    }
                )
            )
        ),
    )
    explanation = DeepSeekExplanationAdapter(
        _config(),
        chat_model=FakeRunnable(_raw(content="受控说明。")),
    )

    assert chat.orchestrate(_chat_input()).result.route == "KNOWLEDGE_ANSWER"
    assert explanation.explain(_explanation_input()).text == "受控说明。"
