"""Model-facing SDK tools and validation before the business Tool gateway."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from materialsagent.domain.models.ml_resource_context import proposal_catalog, resource_specs
from materialsagent.domain.models.semantic_units import UNIT_DIMENSIONS
from materialsagent.domain.ports.agent import AgentFailure

from .context_framework import ContextFramework, ContextFrame, protect_text


UNIT_ASSERTIONS_SCHEMA = {
    "type": "array",
    "maxItems": 16,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["resource_parameter", "column", "unit", "source_message", "evidence"],
        "properties": {
            "resource_parameter": {"type": "string", "minLength": 1},
            "column": {"type": "string", "minLength": 1},
            "unit": {"enum": list(UNIT_DIMENSIONS)},
            "source_message": {"type": "string", "minLength": 1},
            "evidence": {"type": "string", "minLength": 1},
        },
    },
}


async def _unreachable_tool(**_kwargs: Any) -> str:
    # Every call must be intercepted by the Agent Runtime middleware. A missing
    # execution hook must fail closed instead of bypassing the Registry/Gateway.
    raise AgentFailure("TOOL_GATEWAY_REQUIRED")


def sdk_tools(catalog: list[dict[str, Any]], *, ask_user: bool = True) -> list[StructuredTool]:
    """Expose only the Registry's model-safe schemas, with no execution route."""
    tools = []
    for entry in proposal_catalog(catalog):
        schema = deepcopy(entry["schema"])
        schema.setdefault("properties", {})["user_unit_assertions"] = deepcopy(UNIT_ASSERTIONS_SCHEMA)
        tools.append(StructuredTool.from_function(
            coroutine=_unreachable_tool,
            name=entry["tool_name"],
            description=entry.get("description") or entry["tool_name"],
            args_schema=schema,
            infer_schema=False,
        ))
    if ask_user:
        tools.append(StructuredTool.from_function(
            coroutine=_unreachable_tool,
            name="ask_user",
            description="缺少用户意图、必要科学条件或明确的资源选择时，向用户提一个具体问题并暂停。",
            args_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["question"],
                "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2048}},
            },
            infer_schema=False,
        ))
    return tools


def decode_tool_arguments(
    frame: ContextFrame, run: Any, tool_name: str, arguments: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Decode current-frame handles and verify assertions from the latest user input."""
    if run.tool_execution_disabled:
        raise AgentFailure("TOOL_EXECUTION_DISABLED")
    tool = frame.tool_contracts.get(tool_name)
    if tool is None:
        raise AgentFailure("UNKNOWN_TOOL")
    if not isinstance(arguments, dict):
        raise AgentFailure("TOOL_ARGUMENT_FIELD_INVALID")
    values = deepcopy(arguments)
    assertions = values.pop("user_unit_assertions", [])
    projected = proposal_catalog([tool])[0]["schema"]
    properties = projected.get("properties", {})
    if not set(values) <= set(properties):
        raise AgentFailure("TOOL_ARGUMENT_FIELD_INVALID")
    # The resolver owns missing-field diagnostics. Validate only fields supplied
    # by the model so a partial call can lead to a targeted clarification.
    partial_schema = deepcopy(projected)
    partial_schema["required"] = []
    try:
        Draft202012Validator(partial_schema).validate(values)
    except Exception:
        raise AgentFailure("TOOL_ARGUMENT_FIELD_INVALID") from None
    try:
        Draft202012Validator(UNIT_ASSERTIONS_SCHEMA).validate(assertions)
    except Exception:
        raise AgentFailure("INVALID_UNIT_ASSERTION") from None
    allowed_resources = {item["model_argument"] for item in resource_specs(tool)}
    current = run.user_messages[-1] if run.user_messages else None
    for assertion in assertions:
        source = frame.inputs.get(assertion["source_message"])
        evidence = assertion["evidence"]
        if (
            assertion["resource_parameter"] not in allowed_resources
            or current is None
            or source != current["message_id"]
            or evidence not in current["text"]
            or protect_text(evidence, frame.private) != evidence
        ):
            raise AgentFailure("INVALID_UNIT_ASSERTION")
        assertion["source_message"] = source
    if protect_text(values, frame.private) != values:
        raise AgentFailure("MODEL_PRIVATE_VALUE_REJECTED")
    decoded = ContextFramework.arguments(tool_name, values, tool, run, frame.resource_map)
    return decoded, assertions


def validate_model_message(message: AIMessage, *, tools_allowed: bool,
                           allowed_names: set[str] | None = None) -> str | None:
    """Fail closed on unsupported calls; return text only for a complete final turn."""
    if message.invalid_tool_calls:
        raise AgentFailure("INVALID_TOOL_CALL")
    calls = message.tool_calls
    if len(calls) > 1:
        raise AgentFailure("PARALLEL_TOOL_CALLS_NOT_ALLOWED")
    if calls:
        if not tools_allowed:
            raise AgentFailure("TOOL_EXECUTION_DISABLED")
        call = calls[0]
        if not call.get("id") or not call.get("name") or not isinstance(call.get("args"), dict):
            raise AgentFailure("INVALID_TOOL_CALL")
        if allowed_names is not None and call["name"] not in allowed_names:
            raise AgentFailure("UNKNOWN_TOOL")
        return None
    reason = message.response_metadata.get("finish_reason") or message.response_metadata.get("stop_reason")
    if reason in {"length", "max_tokens", "content_filter"}:
        raise AgentFailure("FINAL_ANSWER_TRUNCATED")
    if not isinstance(message.content, str) or not message.content.strip():
        raise AgentFailure("FINAL_ANSWER_INVALID")
    return message.content.strip()
