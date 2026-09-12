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
