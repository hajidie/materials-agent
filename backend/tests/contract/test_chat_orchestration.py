from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
import socket
from typing import Any

import pytest


def _types() -> dict[str, Any]:
    from materialsagent.domain.ports.chat_orchestration import (
        AmbiguousValue,
        ChatOrchestrationInput,
        KnowledgeAnswer,
        NeedsInputCandidate,
        ParameterCandidate,
        ToolCandidate,
        ZTA35GParameterCandidates,
    )
    from materialsagent.infrastructure.llm.mock import (
        ChatOrchestrationProtocolError,
        ChatOrchestrationProviderError,
        ChatOrchestrationTimeoutError,
        MockChatOrchestrationAdapter,
    )

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

    result = adapter.orchestrate(_request(types))

    assert isinstance(result, types[expected_type])
    assert result.route == expected_route
    assert adapter.provider == "mock"
    assert adapter.model_name == "mock-chat-orchestration-v1"
    assert adapter.provider_request_id is None


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

    result = adapter.orchestrate(_request(types))

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
