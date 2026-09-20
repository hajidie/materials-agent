from types import SimpleNamespace
import pytest
from backend.tests.unit.test_llm_provider_factory import _role
from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter, normalize_usage
from materialsagent.infrastructure.llm.factory import provider_client_kwargs
from materialsagent.domain.ports.agent import AgentFailure, PreparedAgentCall

@pytest.mark.parametrize("provider", ["deepseek", "qwen"])
def test_remaining_budget_is_sent_as_real_provider_output_limit(provider):
    captured = []
    class Model:
        def bind(self, **kwargs): return self
        def invoke(self, messages):
            return SimpleNamespace(content='{"type":"Finish","answer":"ok"}', usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    def factory(config):
        captured.append(provider_client_kwargs(config)); return Model()
    config = _role(provider, "agent_decision", max_tokens=8000, thinking_budget=7000 if provider == "qwen" else None)
    adapter = AgentModelAdapter({"agent_decision": config}, factory=factory)
    initial = adapter.prepare("agent_decision", {"goal": "x"}, 32000, 30)
    request = adapter.prepare("agent_decision", {"goal": "x"}, initial.input_estimate + 100, 30)
    result = adapter.invoke(request)
    assert captured[0]["max_tokens"] == 100
    if provider == "qwen": assert captured[0]["extra_body"]["thinking_budget"] == 100
    assert result.usage.total_tokens == 15 and result.usage.source == "actual"

@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 10, "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 5}, "total_tokens": 18},18),
    ({"prompt_tokens": 10, "completion_tokens": 8, "reasoning_tokens": 5},23),
    ({"prompt_tokens": 10, "completion_tokens": 8, "reasoning_tokens": 5, "total_tokens": 23},23),
    ({"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 30},30),
    ({"prompt_tokens": 10, "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 5}, "reasoning_tokens": 5},18),
])
def test_full_usage_observable_components_are_not_lost_or_double_counted(usage, expected):
    result = normalize_usage(SimpleNamespace(response_metadata={"token_usage": usage}), PreparedAgentCall("agent_decision", [],100,200,10))
    assert result.total_tokens == expected and result.source == "actual"

def test_missing_usage_reserves_input_output_and_marks_estimator():
    request = PreparedAgentCall("agent_decision", [],100,200,10)
    result = normalize_usage(SimpleNamespace(), request)
    assert result.total_tokens == 300 and result.source == "estimated" and result.estimator_version

def test_exhausted_budget_does_not_construct_or_invoke_provider():
    config = _role("deepseek", "agent_decision")
    adapter = AgentModelAdapter({"agent_decision": config}, factory=lambda _: pytest.fail("Provider called"))
    with pytest.raises(AgentFailure, match="LLM_TOKEN_BUDGET_EXCEEDED"):
        adapter.prepare("agent_decision", {"goal": "x"}, 100, 30)

def test_invalid_json_retains_actual_consumption():
    class Model:
        def bind(self, **kwargs): return self
        def invoke(self, messages): return SimpleNamespace(content="invalid", usage_metadata={"input_tokens":10,"output_tokens":3,"total_tokens":13})
    adapter = AgentModelAdapter({"agent_decision": _role("deepseek", "agent_decision")}, factory=lambda _:Model())
    result = adapter.invoke(adapter.prepare("agent_decision", {"goal":"x"},32000,30))
    assert result.error_code == "LLM_INVALID_JSON" and result.usage.total_tokens == 13

def test_history_trim_preserves_pairs_and_current_goal():
    adapter = AgentModelAdapter({"agent_decision": _role("deepseek", "agent_decision", history_token_budget=100)})
    history = [{"role": "user", "content": "old"*500},{"role":"assistant","content":"answer"*500}]
    request = adapter.prepare("agent_decision", {"goal":"current", "conversation_context": history},32000,30)
    assert "current" in request.messages[1]["content"] and "oldold" not in request.messages[1]["content"]


@pytest.fixture
def full_catalog_decision(monkeypatch):
    """Exercise production preparation, including the offline tokenizer fallback."""
    from materialsagent.application.agent_runtime import AgentRuntime
    from materialsagent.application.agent_tools import RegistryAgentGateway
    from materialsagent.application.context_framework import ContextFramework
    from materialsagent.application.materials_ml_tools import build_ml_tools
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.domain.models.agent import AgentRun
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.infrastructure.llm.configuration import load_llm_configuration
    from materialsagent.infrastructure.llm import agent_model
    from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter, _Utf8ByteEncoding

    counter = object.__new__(Cl100kTokenCounter)
    counter._encoding = _Utf8ByteEncoding()
    monkeypatch.setattr(agent_model, "_counter", lambda: counter)
    config = load_llm_configuration(load_settings({
        "LLM_ADAPTER": "provider", "DEEPSEEK_API_KEY": "test-only-secret",
    })).for_role("agent_decision")
    registry = build_tool_registry(ml_registrations=build_ml_tools(
        binding_version="1", endpoint_digest="0" * 64))
    gateway = SimpleNamespace(registry=registry,
        resource_context=SimpleNamespace(supports=lambda registration: True))
    tools = SimpleNamespace(catalog=lambda: RegistryAgentGateway.catalog(gateway))
    run = AgentRun(conversation_id="conversation-private", actor_id="actor-private",
        source_message_id="message-private", goal="使用这个数据集训练一个随机森林模型，用于预测强度",
        attachments=[{"attachment_id": "resource-private", "kind": "dataset", "name": "01-training.csv"}])
    payload = AgentRuntime._context(SimpleNamespace(tools=tools), run)
    payload["resource_context"] = {
        "view": {"resources": [{"resource_ref": "r1", "resource_type": "dataset",
            "name": "01-training.csv", "source": "current_message_attachment", "dataset_ordinal": 1,
            "details": {"columns": ["温度", "时间", "强度"]}}], "complete": True, "omitted_count": 0},
        "mapping": {"r1": {"provider": "ml_resource", "resource_type": "dataset",
            "platform_resource_id": "resource-private"}},
    }
    frame = ContextFramework().build("agent_decision", payload, run)
    return config, frame.payload


def test_committed_decision_budget_fits_all_tools_and_csv_with_utf8_fallback(full_catalog_decision):
    import json
    from copy import deepcopy
    config, payload = full_catalog_decision
    before = deepcopy(payload)
    adapter = AgentModelAdapter({"agent_decision": config}, factory=lambda _: pytest.fail("Provider called during preparation"))
    request = adapter.prepare("agent_decision", payload, 32000, 30)
    sent = json.loads(request.messages[1]["content"])
    assert len(sent["tools"]) == 7
    assert sent["tools"] == payload["tools"]
    assert sent["resource_context"] == payload["resource_context"]
    assert sent["attachments"] == payload["attachments"]
    assert request.output_limit == 1024
    assert request.input_estimate > 16384
    assert request.input_estimate + request.output_limit <= config.prompt_limit_tokens - config.safety_margin_tokens
    assert payload == before
    assert "resource-private" not in request.messages[1]["content"]


@pytest.mark.parametrize("limit,remaining,error", [
    (16384, 32000, "CONTEXT_BUDGET_EXCEEDED"),
    (24576, 16000, "LLM_TOKEN_BUDGET_EXCEEDED"),
])
def test_full_catalog_still_respects_explicit_and_remaining_limits(full_catalog_decision, limit, remaining, error):
    from dataclasses import replace
    config, payload = full_catalog_decision
    adapter = AgentModelAdapter({"agent_decision": replace(config, prompt_limit_tokens=limit)},
        factory=lambda _: pytest.fail("Provider called with insufficient budget"))
    with pytest.raises(AgentFailure, match=error):
        adapter.prepare("agent_decision", payload, remaining, 30)
