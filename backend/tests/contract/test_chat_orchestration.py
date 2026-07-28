from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from importlib import import_module
import socket
from typing import Any

import pytest


def test_chat_port_exposes_immutable_request_metadata_and_outcome() -> None:
    from materialsagent.domain.ports.chat_orchestration import (
        ChatOrchestrationOutcome,
        ChatOrchestrationRequestMetadata,
        KnowledgeAnswer,
    )

    metadata = ChatOrchestrationRequestMetadata(
        provider="deepseek",
        model_name="deepseek-v4-flash",
        prompt_template_id="chat-orchestration",
        prompt_template_version="2",
        prompt_digest="a" * 64,
        generation_parameters={
            "temperature": 0,
            "max_tokens": 1024,
            "thinking_mode": "disabled",
            "response_format": "json_object",
            "streaming": False,
        },
    )
    outcome = ChatOrchestrationOutcome(
        result=KnowledgeAnswer(answer_text="受控答案。"),
        usage={"input_tokens": 3, "output_tokens": 2},
        provider_request_id="provider-request-1",
    )

    assert metadata.provider == "deepseek"
    assert metadata.generation_parameters["max_tokens"] == 1024
    assert outcome.result.route == "KNOWLEDGE_ANSWER"
    assert outcome.usage == {"input_tokens": 3, "output_tokens": 2}
    with pytest.raises(TypeError):
        metadata.generation_parameters["max_tokens"] = 1
    with pytest.raises(TypeError):
        outcome.usage["input_tokens"] = 0


@pytest.mark.parametrize(
    ("usage", "provider_request_id"),
    [
        ({"input_tokens": True, "output_tokens": 1}, None),
        ({"input_tokens": 1, "output_tokens": -1}, None),
        ({"input_tokens": 1}, None),
        (None, "contains a space"),
    ],
)
def test_chat_outcome_rejects_uncontrolled_metadata(
    usage: object,
    provider_request_id: str | None,
) -> None:
    from materialsagent.domain.ports.chat_orchestration import (
        ChatOrchestrationOutcome,
        KnowledgeAnswer,
    )

    with pytest.raises(ValueError):
        ChatOrchestrationOutcome(
            result=KnowledgeAnswer(answer_text="受控答案。"),
            usage=usage,
            provider_request_id=provider_request_id,
        )


def test_port_module_owns_the_safe_failure_contract() -> None:
    port_module = import_module(
        "materialsagent.domain.ports.chat_orchestration"
    )

    for error_name in (
        "ChatOrchestrationTimeoutError",
        "ChatOrchestrationProviderError",
        "ChatOrchestrationProtocolError",
    ):
        error_type = getattr(port_module, error_name, None)
        assert error_type is not None
        assert error_type.__module__ == port_module.__name__


def test_mock_adapter_has_no_static_provider_request_id() -> None:
    port_module = import_module(
        "materialsagent.domain.ports.chat_orchestration"
    )
    mock_module = import_module("materialsagent.infrastructure.llm.mock")
    adapter = mock_module.MockChatOrchestrationAdapter(
        responder=lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "安全答案。",
        }
    )

    annotations = port_module.ChatOrchestrationPort.__annotations__
    assert {"provider", "model_name"} <= set(annotations)
    assert "provider_request_id" not in annotations
    assert not hasattr(adapter, "provider_request_id")


def test_mock_request_metadata_preserves_the_legacy_identity() -> None:
    from hashlib import sha256

    from materialsagent.infrastructure.llm.mock import (
        MockChatOrchestrationAdapter,
    )

    types = _types()
    request = _request(types)
    adapter = MockChatOrchestrationAdapter(
        lambda _: {
            "route": "KNOWLEDGE_ANSWER",
            "answer_text": "安全答案。",
        }
    )

    metadata = adapter.request_metadata(request)

    expected = sha256(
        (
            "chat-orchestration:1\n"
            f"{request.content_text}"
        ).encode("utf-8")
    ).hexdigest()
    assert metadata.provider == "mock"
    assert metadata.model_name == "mock-chat-orchestration-v1"
    assert metadata.prompt_template_id == "chat-orchestration"
    assert metadata.prompt_template_version == "1"
    assert metadata.prompt_digest == expected
    assert metadata.generation_parameters == {
        "temperature": 0,
        "max_tokens": 256,
    }


def _types() -> dict[str, Any]:
    from materialsagent.domain.ports.chat_orchestration import (
        AmbiguousValue,
        ChatOrchestrationProtocolError,
        ChatOrchestrationProviderError,
        ChatOrchestrationInput,
        ChatOrchestrationTimeoutError,
        KnowledgeAnswer,
        NeedsInputCandidate,
        ParameterCandidate,
        ToolCandidate,
        ZTA35GParameterCandidates,
    )
    from materialsagent.infrastructure.llm.mock import MockChatOrchestrationAdapter

    return locals()


def _request(types: dict[str, Any]) -> Any:
    return types["ChatOrchestrationInput"](
        task_id="task_contract",
        conversation_id="conversation_contract",
        request_id="request_contract",
        content_text="请分析 ZTA35G。",
    )


def _parameters() -> dict[str, object]:
    return {
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {"value": 3, "unit": "h"},
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 180, "unit": "min"},
    }


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_route"),
    [
        (
            {"route": "KNOWLEDGE_ANSWER", "answer_text": "安全答案。"},
            "KnowledgeAnswer",
            "KNOWLEDGE_ANSWER",
        ),
        (
            {
                "route": "TOOL_EXECUTION",
                "tool_id": "zta35g_sem_virtual_lab",
                "material": "ZTA35G",
                "candidate_parameters": _parameters(),
                "requested_outputs": ["sem_image"],
            },
            "ToolCandidate",
            "TOOL_EXECUTION",
        ),
        (
            {
                "route": "NEEDS_INPUT",
                "tool_id": "zta35g_sem_virtual_lab",
                "material": {"candidates": ["ZTA35G", "ZTA35G?"]},
                "candidate_parameters": {
                    "solution_temperature": {
                        "value": 1000,
                        "unit": "°C",
                    }
                },
                "missing_fields": ["solution_time"],
                "ambiguous_fields": ["material"],
                "follow_up_suggestion": "请确认材料与保温时间。",
                "requested_outputs": ["mechanical_properties"],
            },
            "NeedsInputCandidate",
            "NEEDS_INPUT",
        ),
    ],
)
def test_mock_decodes_each_discriminated_result(
    payload: dict[str, object],
    expected_type: str,
    expected_route: str,
) -> None:
    types = _types()
    adapter = types["MockChatOrchestrationAdapter"](
        responder=lambda _: payload
    )

    result = adapter.orchestrate(_request(types)).result

    assert isinstance(result, types[expected_type])
    assert result.route == expected_route
    assert adapter.provider == "mock"
    assert adapter.model_name == "mock-chat-orchestration-v1"


def test_candidate_models_are_strongly_typed() -> None:
    types = _types()
    adapter = types["MockChatOrchestrationAdapter"](
        responder=lambda _: {
            "route": "NEEDS_INPUT",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": {"candidates": ["ZTA35G", "ZTA35G?"]},
            "candidate_parameters": {
                "solution_time": {"value": 61, "unit": "min"}
            },
            "missing_fields": ["aging_time"],
            "ambiguous_fields": ["material"],
            "follow_up_suggestion": "请确认。",
            "requested_outputs": ["sem_image", "sem_image"],
        }
    )

    result = adapter.orchestrate(_request(types)).result

    assert isinstance(result.material, types["AmbiguousValue"])
    assert isinstance(
        result.candidate_parameters.solution_time,
        types["ParameterCandidate"],
    )
    assert result.candidate_parameters.aging_time is None
    assert result.requested_outputs == ("sem_image", "sem_image")


def test_ambiguous_values_are_deduplicated_and_remain_distinct() -> None:
    types = _types()

    ambiguous = types["AmbiguousValue"](("h", "h", "min", "min"))

    assert ambiguous.candidates == ("h", "min")
    with pytest.raises(ValueError, match="two distinct"):
        types["AmbiguousValue"](("h", "h"))


@pytest.mark.parametrize(
    "unsafe_value",
    [
        object(),
        b"bytes",
        {"raw": "mapping"},
        float("nan"),
        float("inf"),
        Decimal("1.1"),
        2**60,
    ],
)
def test_candidate_contract_rejects_non_json_values(
    unsafe_value: object,
) -> None:
    types = _types()

    with pytest.raises(ValueError, match="candidate"):
        types["ParameterCandidate"](value=unsafe_value, unit="h")


def test_candidate_contract_rejects_scalar_subclasses() -> None:
    types = _types()

    class SecretInt(int):
        pass

    class SecretFloat(float):
        pass

    class SecretStr(str):
        pass

    values = (SecretInt(3), SecretFloat(3.0), SecretStr("3"))
    for value in values:
        value.response_body = "SECRET"
        with pytest.raises(ValueError, match="candidate"):
            types["ParameterCandidate"](value=value, unit="h")


def test_requested_outputs_reject_arbitrary_python_objects() -> None:
    types = _types()

    with pytest.raises(ValueError, match="requested_outputs"):
        types["ToolCandidate"](
            tool_id="zta35g_sem_virtual_lab",
            material="ZTA35G",
            candidate_parameters=types["ZTA35GParameterCandidates"](),
            requested_outputs=(object(),),
        )


def test_parameter_container_rejects_arbitrary_python_objects() -> None:
    types = _types()

    with pytest.raises(ValueError, match="candidate_parameters"):
        types["ZTA35GParameterCandidates"](solution_time=object())


@pytest.mark.parametrize(
    "payload",
    [
        {"route": "UNKNOWN", "answer_text": "x"},
        {"route": "KNOWLEDGE_ANSWER", "answer_text": "   "},
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "unknown_tool",
            "material": "ZTA35G",
            "candidate_parameters": {},
            "requested_outputs": ["sem_image"],
        },
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": {},
        },
        {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": {"unknown_parameter": None},
            "requested_outputs": ["sem_image"],
        },
    ],
)
def test_mock_rejects_protocol_violations(payload: dict[str, object]) -> None:
    types = _types()
    adapter = types["MockChatOrchestrationAdapter"](
        responder=lambda _: payload
    )

    with pytest.raises(types["ChatOrchestrationProtocolError"]):
        adapter.orchestrate(_request(types))


@pytest.mark.parametrize(
    "error_name",
    ["ChatOrchestrationTimeoutError", "ChatOrchestrationProviderError"],
)
def test_mock_propagates_injected_safe_failures(error_name: str) -> None:
    types = _types()
    error_type = types[error_name]

    def responder(_: object) -> dict[str, object]:
        raise error_type("Safe injected failure.")

    adapter = types["MockChatOrchestrationAdapter"](responder=responder)

    with pytest.raises(error_type, match="Safe injected failure"):
        adapter.orchestrate(_request(types))


def test_mock_normalizes_unexpected_responder_error_without_leaking_text() -> None:
    types = _types()

    def responder(_: object) -> dict[str, object]:
        raise RuntimeError("Provider exploded with SECRET and C:\\private\\key")

    adapter = types["MockChatOrchestrationAdapter"](responder=responder)

    with pytest.raises(types["ChatOrchestrationProviderError"]) as captured:
        adapter.orchestrate(_request(types))

    assert str(captured.value) == "Chat orchestration provider failed."
    assert "SECRET" not in str(captured.value)


def test_mock_normalizes_hostile_mapping_decode_error() -> None:
    types = _types()

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            if key == "route":
                return "TOOL_EXECUTION"
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("SECRET provider mapping failure")

        def __len__(self) -> int:
            return 1

    adapter = types["MockChatOrchestrationAdapter"](
        responder=lambda _: HostileMapping()
    )

    with pytest.raises(types["ChatOrchestrationProtocolError"]) as captured:
        adapter.orchestrate(_request(types))

    assert str(captured.value) == (
        "Chat orchestration result violated the protocol."
    )
    assert "SECRET" not in str(captured.value)


def test_mock_is_deterministic_and_has_no_external_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    types = _types()
    payload = {"route": "KNOWLEDGE_ANSWER", "answer_text": "固定答案。"}
    calls: list[object] = []

    def blocked_network(*_: object, **__: object) -> object:
        raise AssertionError("Mock adapter attempted network access.")

    monkeypatch.setattr(socket, "create_connection", blocked_network)

    def responder(value: object) -> dict[str, object]:
        calls.append(value)
        return payload

    adapter = types["MockChatOrchestrationAdapter"](responder=responder)
    request = _request(types)

    first = adapter.orchestrate(request)
    second = adapter.orchestrate(request)

    assert first == second
    assert calls == [request, request]
    assert vars(adapter) == {"_responder": responder}


@pytest.mark.parametrize(
    ("content_text", "expected_route"),
    [
        ("什么是 ZTA35G？", "KNOWLEDGE_ANSWER"),
        ("谢谢", "KNOWLEDGE_ANSWER"),
        ("缺 aging_temperature", "NEEDS_INPUT"),
        ("明确歧义参数", "NEEDS_INPUT"),
        ("solution_time = 180 min", "TOOL_EXECUTION"),
        ("越界温度", "TOOL_EXECUTION"),
        ("完整合法 Tool 请求", "TOOL_EXECUTION"),
    ],
)
def test_default_mock_responder_has_controlled_acceptance_scenarios(
    content_text: str,
    expected_route: str,
) -> None:
    try:
        from materialsagent.infrastructure.llm.mock import default_mock_responder
    except ImportError:
        pytest.fail("default_mock_responder is not implemented yet.")
    types = _types()
    request = types["ChatOrchestrationInput"](
        task_id="task_contract",
        conversation_id="conversation_contract",
        request_id="request_contract",
        content_text=content_text,
    )
    adapter = types["MockChatOrchestrationAdapter"](default_mock_responder)

    first = adapter.orchestrate(request)
    second = adapter.orchestrate(request)

    assert first == second
    assert first.result.route == expected_route


@pytest.mark.parametrize(
    "content_text",
    [
        "730 °C",
        "730°C",
        "aging_temperature = 730 °C",
        "aging_temperature=730°C",
        "时效温度 730 °C",
    ],
)
def test_default_mock_responder_accepts_controlled_aging_temperature_supplement(
    content_text: str,
) -> None:
    from materialsagent.infrastructure.llm.mock import default_mock_responder

    types = _types()
    request = types["ChatOrchestrationInput"](
        task_id="task_contract",
        conversation_id="conversation_contract",
        request_id="request_contract",
        content_text=content_text,
    )
    result = types["MockChatOrchestrationAdapter"](
        default_mock_responder
    ).orchestrate(request).result

    assert isinstance(result, types["ToolCandidate"])
    assert result.route == "TOOL_EXECUTION"
    assert result.requested_outputs == (
        "sem_image",
        "mechanical_properties",
    )
    parameters = result.candidate_parameters
    assert (
        parameters.solution_temperature.value,
        parameters.solution_temperature.unit,
    ) == (1000, "°C")
    assert (
        parameters.solution_time.value,
        parameters.solution_time.unit,
    ) == (3, "h")
    assert (
        parameters.aging_temperature.value,
        parameters.aging_temperature.unit,
    ) == (730, "°C")
    assert (
        parameters.aging_time.value,
        parameters.aging_time.unit,
    ) == (3, "h")


def test_default_mock_responder_exposes_only_controlled_safe_failures() -> None:
    try:
        from materialsagent.infrastructure.llm.mock import default_mock_responder
    except ImportError:
        pytest.fail("default_mock_responder is not implemented yet.")
    types = _types()
    adapter = types["MockChatOrchestrationAdapter"](default_mock_responder)

    cases = (
        ("Mock timeout", types["ChatOrchestrationTimeoutError"]),
        ("Mock provider failure", types["ChatOrchestrationProviderError"]),
        ("Mock protocol failure", types["ChatOrchestrationProtocolError"]),
    )
    for content_text, error_type in cases:
        request = types["ChatOrchestrationInput"](
            task_id="task_contract",
            conversation_id="conversation_contract",
            request_id="request_contract",
            content_text=content_text,
        )
        with pytest.raises(error_type) as captured:
            adapter.orchestrate(request)
        assert "SECRET" not in str(captured.value)
        assert "C:\\" not in str(captured.value)
