from __future__ import annotations

from dataclasses import replace
import math
import socket

import pytest

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput,
    ChatOrchestrationOutcome,
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    ToolCandidateProposal,
    ToolCandidateSet,
)
from materialsagent.domain.ports.tool_registry import (
    RoutingCatalogEntry,
    RoutingCatalogSnapshot,
)
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
    default_mock_responder,
)


def _catalog(tool_id: str = "zta35g_sem_virtual_lab") -> RoutingCatalogSnapshot:
    return RoutingCatalogSnapshot(entries=(RoutingCatalogEntry(
        tool_id=tool_id,
        version="1",
        schema_hash="a" * 64,
        display_name="Safe Tool",
        description="Safe routing description.",
        input_schema={"type": "object", "properties": {"material": {"type": "string"}}},
        supported_outputs=("result",),
    ),))


def _request() -> ChatOrchestrationInput:
    return ChatOrchestrationInput(
        task_id="task_contract",
        conversation_id="conversation_contract",
        request_id="request_contract",
        content_text="请分析材料。",
        routing_catalog=_catalog(),
    )


def test_router_contract_exposes_only_knowledge_or_generic_candidate_set() -> None:
    candidate_input = {"material": "ZTA35G", "temperature": {"value": 1000, "unit": "°C"}}
    result = ToolCandidateSet((ToolCandidateProposal("zta35g_sem_virtual_lab", candidate_input),))
    outcome = ChatOrchestrationOutcome(result, usage=None, provider_request_id=None)

    assert outcome.result.route == "TOOL_CANDIDATES"
    assert outcome.result.candidates[0].candidate_input == candidate_input
    assert not hasattr(outcome.result, "status")
    assert not hasattr(outcome.result.candidates[0], "version")
    assert not hasattr(outcome.result.candidates[0], "schema_hash")
    with pytest.raises(TypeError):
        outcome.result.candidates[0].candidate_input["material"] = "changed"


@pytest.mark.parametrize("count", [0, 6])
def test_candidate_set_is_bounded_to_one_through_five(count: int) -> None:
    candidates = tuple(ToolCandidateProposal(f"tool_{index}", {}) for index in range(count))
    with pytest.raises(ValueError):
        ToolCandidateSet(candidates)


def test_candidate_set_rejects_duplicate_tool_ids() -> None:
    with pytest.raises(ValueError, match="repeat"):
        ToolCandidateSet((ToolCandidateProposal("tool_one", {}), ToolCandidateProposal("tool_one", {})))


def test_candidate_set_rejects_aggregate_decoded_object_over_4096_bytes() -> None:
    candidates = tuple(
        ToolCandidateProposal(f"tool_{index}", {"value": "x" * 900})
        for index in range(5)
    )

    with pytest.raises(ValueError, match="safe size"):
        ToolCandidateSet(candidates)


@pytest.mark.parametrize("unsafe", [object(), b"bytes", math.nan, math.inf, 2**60])
def test_candidate_input_rejects_non_json_or_unbounded_values(unsafe: object) -> None:
    with pytest.raises(ValueError):
        ToolCandidateProposal("tool_one", {"unsafe": unsafe})


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"route": "KNOWLEDGE_ANSWER", "answer_text": "安全答案。"}, KnowledgeAnswer),
        ({
            "route": "TOOL_CANDIDATES",
            "candidates": [{"tool_id": "zta35g_sem_virtual_lab", "candidate_input": {"material": "ZTA35G"}}],
        }, ToolCandidateSet),
    ],
)
def test_mock_decodes_only_generic_router_results(payload, expected_type) -> None:
    outcome = MockChatOrchestrationAdapter(lambda _: payload).orchestrate(_request())
    assert isinstance(outcome.result, expected_type)


@pytest.mark.parametrize(
    "payload",
    [
        {"route": "TOOL_EXECUTION", "tool_id": "zta35g_sem_virtual_lab"},
        {"route": "NEEDS_INPUT", "tool_id": "zta35g_sem_virtual_lab"},
        {"route": "TOOL_CANDIDATES", "status": "READY", "candidates": []},
        {"route": "TOOL_CANDIDATES", "candidates": [{
            "tool_id": "tool_one", "version": "1", "schema_hash": "a" * 64, "candidate_input": {},
        }]},
    ],
)
def test_mock_rejects_legacy_status_or_binding_fields(payload) -> None:
    with pytest.raises(ChatOrchestrationProtocolError):
        MockChatOrchestrationAdapter(lambda _: payload).orchestrate(_request())


def test_request_metadata_digest_is_bound_to_catalog_snapshot() -> None:
    adapter = MockChatOrchestrationAdapter(lambda _: {"route": "KNOWLEDGE_ANSWER", "answer_text": "safe"})
    first = adapter.request_metadata(_request())
    second = adapter.request_metadata(replace(_request(), routing_catalog=_catalog("other_tool")))
    assert first.prompt_digest != second.prompt_digest


@pytest.mark.parametrize(
    ("content", "route"),
    [
        ("什么是 ZTA35G？", "KNOWLEDGE_ANSWER"),
        ("缺 aging_temperature", "TOOL_CANDIDATES"),
        ("明确歧义参数", "TOOL_CANDIDATES"),
        ("完整合法 Tool 请求", "TOOL_CANDIDATES"),
    ],
)
def test_default_mock_responder_uses_generic_routes(content: str, route: str) -> None:
    request = replace(_request(), content_text=content)
    assert default_mock_responder(request)["route"] == route


@pytest.mark.parametrize("error_type", [ChatOrchestrationTimeoutError, ChatOrchestrationProviderError])
def test_mock_propagates_controlled_safe_failures(error_type) -> None:
    def fail(_):
        raise error_type("safe")

    with pytest.raises(error_type):
        MockChatOrchestrationAdapter(fail).orchestrate(_request())


def test_mock_makes_no_socket_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    outcome = MockChatOrchestrationAdapter(default_mock_responder).orchestrate(_request())
    assert outcome.result.route in {"KNOWLEDGE_ANSWER", "TOOL_CANDIDATES"}
