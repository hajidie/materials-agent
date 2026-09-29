from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.tests.unit.test_llm_provider_factory import _role
from materialsagent.domain.ports.agent import PreparedAgentCall
from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter, _count, normalize_usage
from materialsagent.infrastructure.llm.factory import provider_client_kwargs


@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_native_model_uses_bounded_provider_settings_without_json_action_format(provider):
    captured = []

    def factory(config):
        captured.append(provider_client_kwargs(config))
        return object()

    config = _role(provider, "agent_decision", max_tokens=8000)
    model = AgentModelAdapter({"agent_decision": config}, factory=factory).native_model(
        timeout=10, output_limit=100)
    assert model is not None
    assert captured[0]["timeout"] == 10
    assert captured[0]["max_tokens"] == 100
    assert "response_format" not in captured[0]


@pytest.mark.parametrize("metadata,expected,reasoning", [
    ({"response_metadata": {"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 8,
        "completion_tokens_details": {"reasoning_tokens": 5}, "total_tokens": 18}}}, 18, 5),
    ({"response_metadata": {"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 8, "reasoning_tokens": 5}}}, 23, 5),
    ({"response_metadata": {"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 8, "reasoning_tokens": 5, "total_tokens": 23}}}, 23, 5),
    ({"response_metadata": {"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 30}}}, 30, 0),
    ({"response_metadata": {"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 8,
        "completion_tokens_details": {"reasoning_tokens": 5}, "reasoning_tokens": 5}}}, 18, 5),
    ({"usage_metadata": {"input_tokens": 10, "output_tokens": 20,
        "total_tokens": 30, "output_token_details": {"reasoning": 8}}}, 30, 8),
])
def test_full_usage_observable_components_are_not_lost_or_double_counted(metadata, expected, reasoning):
    result = normalize_usage(SimpleNamespace(**metadata), PreparedAgentCall("agent_decision", [], 100, 200, 10))
    assert result.total_tokens == expected and result.source == "actual"
    assert result.reasoning_tokens == reasoning


def test_missing_usage_reserves_input_output_and_marks_estimator():
    request = PreparedAgentCall("agent_decision", [], 100, 200, 10)
    result = normalize_usage(SimpleNamespace(), request)
    assert result.total_tokens == 300 and result.source == "estimated" and result.estimator_version


def test_prompt_token_estimate_never_uses_zero_for_unicode_payload():
    assert _count({"goal": "屈服强度换算", "resources": ["训练数据"]}) >= 128
