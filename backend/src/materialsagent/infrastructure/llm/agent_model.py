"""Native provider models and bounded usage accounting."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
import json
import re
from typing import Any

from materialsagent.domain.models.agent import TokenUsage, canonical
from materialsagent.domain.ports.agent import PreparedAgentCall


@lru_cache(maxsize=1)
def _counter():
    from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter
    return Cl100kTokenCounter()


def _count(value: Any) -> int:
    # Existing project tokenizer, doubled for provider differences plus request framing.
    # Its offline fallback counts UTF-8 bytes; never silently meter zero.
    text = canonical(value)
    try:
        return min(len(text.encode("utf-8")), _counter().count_text(text) * 2) + 128
    except ValueError:
        return len(text.encode("utf-8")) + 128


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_usage(raw: Any, request: PreparedAgentCall) -> TokenUsage:
    """LangChain/OpenAI completion counts include their reasoning detail counts."""
    metadata = getattr(raw, "response_metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    native = metadata.get("token_usage", {})
    native = native if isinstance(native, Mapping) else {}
    usage = getattr(raw, "usage_metadata", {})
    usage = usage if isinstance(usage, Mapping) else {}
    input_tokens = _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    if input_tokens is None:
        input_tokens = _integer(native.get("prompt_tokens"))
    if output_tokens is None:
        output_tokens = _integer(native.get("completion_tokens"))
    details = usage.get("output_token_details") or native.get("completion_tokens_details") or {}
    details = details if isinstance(details, Mapping) else {}
    reasoning = _integer(details.get("reasoning"))
    if reasoning is None:
        reasoning = _integer(details.get("reasoning_tokens")) or 0
    total = _integer(usage.get("total_tokens"))
    if total is None:
        total = _integer(native.get("total_tokens"))
    # Some compatible providers expose reasoning as a separate, non-detail component.
    separate_reasoning = _integer(native.get("reasoning_tokens")) or 0
    detailed_reasoning = reasoning
    reasoning = max(reasoning, separate_reasoning)
    if input_tokens == 0 and output_tokens == 0 and not total:
        input_tokens, output_tokens = None, None
    actual = input_tokens is not None and output_tokens is not None
    input_tokens = request.input_estimate if input_tokens is None else input_tokens
    output_tokens = request.output_limit if output_tokens is None else output_tokens
    minimum = input_tokens + output_tokens
    if total is None:
        total = minimum + (separate_reasoning if not detailed_reasoning else 0)
    else:
        # Provider total is authoritative when it covers all observable components.
        total = max(total, minimum, input_tokens + reasoning)
    if not actual:
        total = max(total, minimum)
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens,
        reasoning_tokens=reasoning, total_tokens=total, source="actual" if actual else "estimated",
        estimator_version=None if actual else "cl100k-x2-or-utf8-framing-v1")


class AgentModelAdapter:
    def __init__(self, configurations: Mapping[str, Any], *, factory=None):
        self.configurations = configurations
        self.factory = factory

    def native_model(self, *, timeout: float | None = None, output_limit: int | None = None,
                     http_client=None, http_async_client=None, streaming: bool | None = None):
        """Create a provider chat model without the legacy JSON response format."""
        from materialsagent.infrastructure.llm.factory import create_chat_model

        config = self.configurations["agent_decision"]
        changes = {}
        if streaming is not None:
            changes["streaming"] = streaming
        if timeout is not None:
            changes["timeout_seconds"] = min(timeout, config.timeout_seconds)
        if output_limit is not None:
            changes["max_tokens"] = min(output_limit, config.max_tokens or output_limit)
        if changes:
            config = replace(config, **changes)
        return self.factory(config) if self.factory else create_chat_model(
            config, http_client=http_client, http_async_client=http_async_client,
        )

class MockAgentModel:
    """Deterministic offline model; real decisions use the same bounded protocol."""
    def __init__(self, responder=None):
        self.responder = responder

    def native_model(self, **_kwargs):
        return _MockNativeChatModel(responder=self.responder)

    @staticmethod
    def _respond(role, payload):
        draft = payload.get("draft")
        if draft and draft["issues"] and not (payload.get("user_inputs") and draft["tool_name"] == "ebsd_yield_strength_predictor"):
            text = " ".join(payload.get("user_inputs", []))
            values = dict(draft.get("normalized", {}))
            changed = False
            for field in draft["issues"]:
                match = re.search(re.escape(field) + r"\s*[:=：]\s*([^,，;；\s]+)", text)
                if match:
                    value = match.group(1)
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                    values[field] = value
                    changed = True
            if changed:
                return {"type": "CallTool", "tool_name": draft["tool_name"], "arguments": values}
            return {"type": "AskUser", "question": "请补充：" + "、".join(draft["issues"])}
        results = [o for o in payload["observations"] if o["kind"] == "TOOL_RESULT"]
        if results:
            return {"type": "Finish", "sources": [o["source"] for o in results],
                    "answer": "\n".join(o.get("presentation", {}).get("summary") or "工具已完成。" for o in results)}
        text = " ".join([payload["goal"], *payload.get("user_inputs", [])])
        if payload.get("retry_target"):
            target = payload["retry_target"]
            return {"type": "CallTool", "tool_name": target["tool_name"], "arguments": target["arguments"]}
        if payload.get("tool_execution_disabled"):
            return {"type": "Finish", "answer": "根据已有可信结果重新生成的回答。", "sources": []}
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*(MPa|GPa|Pa|mm|cm|m|°C|°F|K|h|min|s)\s*(?:换算|转换|转|到|to|为|成|=)+\s*(MPa|GPa|Pa|mm|cm|m|°C|°F|K|h|min|s)", text)
        if match:
            return {"type": "CallTool", "tool_name": "materials_unit_conversion",
                    "arguments": {"value": float(match[1]), "from_unit": match[2], "to_unit": match[3]}}
        if any(a.get("type") == "ebsd_image" for a in payload.get("attachments", [])) or "EBSD" in text.upper():
            image = next((item for item in payload.get("resource_context", {}).get("resources", [])
                          if item.get("resource_type") == "ebsd_image"), None)
            return {"type": "CallTool", "tool_name": "ebsd_yield_strength_predictor",
                "arguments": {"image_reference": {"resource_ref": image["resource_ref"]}}
                if image else {"image_reference": {"unresolved": True}}}
        if "ZTA35G" in text.upper():
            for item in reversed(payload.get("user_inputs", [])):
                try:
                    arguments = json.loads(item)
                    if isinstance(arguments, dict) and arguments.get("material") == "ZTA35G":
                        return {"type": "CallTool", "tool_name": "zta35g_sem_virtual_lab", "arguments": arguments}
                except ValueError:
                    pass
            return {"type": "AskUser", "question": "请提供工艺参数和输出类型。"}
        return {"type": "Finish", "answer": "当前为离线 Mock 模式。可以输入单位换算请求，或提供 ZTA35G 工艺参数进行工具流程验证。"}


class _MockNativeChatModel:
    """Test adapter only: translate deterministic fixtures to provider tool calls."""

    def __new__(cls, *, responder):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, SystemMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class Model(BaseChatModel):
            fixture_responder: Any = Field(default=None, exclude=True)

            @property
            def _llm_type(self) -> str:
                return "materialsagent-mock-native"

            def bind_tools(self, _tools, **_kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                prompt = next((m.content for m in reversed(messages) if isinstance(m, SystemMessage)), "")
                marker = "当前已校验任务上下文：\n"
                payload = json.loads(prompt.split(marker, 1)[1])
                value = (self.fixture_responder("agent_decision", payload)
                         if self.fixture_responder else MockAgentModel._respond("agent_decision", payload))
                kind = value.get("type")
                if kind == "CallTool":
                    from materialsagent.domain.models.agent import identifier
                    message = AIMessage(content="", tool_calls=[{
                        "id": value.get("tool_call_id") or identifier(), "name": value["tool_name"], "args": {
                            **value.get("arguments", {}),
                            "user_unit_assertions": value.get("user_unit_assertions", []),
                        },
                    }])
                elif kind == "AskUser":
                    from materialsagent.domain.models.agent import identifier
                    message = AIMessage(content="", tool_calls=[{
                        "id": value.get("tool_call_id") or identifier(), "name": "ask_user", "args": {"question": value["question"]},
                    }])
                elif kind == "Finish":
                    message = AIMessage(content=value.get("answer", ""), response_metadata={"finish_reason": "stop"})
                else:
                    message = AIMessage(content="", response_metadata={"finish_reason": "stop"})
                input_count = _count(payload)
                output_count = max(1, _count(value))
                message.usage_metadata = {"input_tokens": input_count, "output_tokens": output_count,
                                          "total_tokens": input_count + output_count}
                return ChatResult(generations=[ChatGeneration(message=message)])

        return Model(fixture_responder=responder)
