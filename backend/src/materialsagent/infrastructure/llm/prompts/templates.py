from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Final

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate

from materialsagent.domain.ports.chat_orchestration import ChatOrchestrationInput
from materialsagent.domain.ports.explanation import ExplanationInput
from materialsagent.domain.ports.tool_input_extraction import ToolInputExtractionInput


CHAT_ORCHESTRATION_PROMPT_ID: Final = "chat-orchestration"
CHAT_ORCHESTRATION_PROMPT_VERSION: Final = "5"
TOOL_INPUT_EXTRACTION_PROMPT_ID: Final = "tool-input-extraction"
TOOL_INPUT_EXTRACTION_PROMPT_VERSION: Final = "1"
TOOL_RESULT_EXPLANATION_PROMPT_ID: Final = "tool-result-explanation"
TOOL_RESULT_EXPLANATION_PROMPT_VERSION: Final = "2"

_CHAT_ORCHESTRATION_TEMPLATE: Final = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a single-call materials research router. Use only the supplied "
            "routing catalog. Return exactly one JSON object and no other text. "
            "For a knowledge question return "
            '{{"route":"KNOWLEDGE_ANSWER","answer_text":"..."}}. '
            "For Tool intent return one to five unique candidates as "
            '{{"route":"TOOL_CANDIDATES","candidates":[{{"tool_id":"...",'
            '"candidate_input":{{}}}}]}}. Candidate input must exactly follow that '
            "Tool's candidate_input_schema. Preserve missing values as null and "
            "explicit ambiguity as a controlled candidates array inside candidate_input. "
            "Never return task status, execution permission, Tool version, or schema hash. "
            "Routing catalog: {catalog}",
        ),
        ("human", "{content_text}"),
    ]
)
_TOOL_INPUT_EXTRACTION_TEMPLATE: Final = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Extract only newly supplied values for this already-bound Tool. "
            "Use only the supplied Tool context and candidate input schema. "
            "Return exactly one JSON object shaped as "
            '{{"candidate_input_delta":{{}}}} and no other text. Do not return '
            "a Tool ID, route, task status, execution permission, version, or "
            "schema hash. Bound Tool context: {context}",
        ),
        ("human", "{content_text}"),
    ]
)
_TOOL_RESULT_EXPLANATION_TEMPLATE: Final = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Explain only the supplied ToolResult facts in concise plain text. "
            "Do not invent values or expose internal reasoning.",
        ),
        ("human", "{result_payload}"),
    ]
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _controlled_messages(messages: list[BaseMessage]) -> list[dict[str, str]]:
    rendered: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, HumanMessage):
            role = "user"
        else:
            raise TypeError("Prompt template rendered an unsupported message type.")
        if not isinstance(message.content, str):
            raise TypeError("Prompt template rendered non-text content.")
        rendered.append({"role": role, "content": message.content})
    return rendered


def render_chat_orchestration_prompt(
    value: ChatOrchestrationInput,
) -> list[dict[str, str]]:
    catalog = [
        {
            "tool_id": entry.tool_id,
            "version": entry.version,
            "schema_hash": entry.schema_hash,
            "display_name": entry.display_name,
            "description": entry.description,
            "candidate_input_schema": _plain_json(entry.candidate_input_schema),
            "supported_outputs": list(entry.supported_outputs),
        }
        for entry in value.routing_catalog.entries
    ]
    return _controlled_messages(
        _CHAT_ORCHESTRATION_TEMPLATE.format_messages(
            catalog=json.dumps(
                catalog,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            content_text=value.content_text,
        )
    )


def render_tool_input_extraction_prompt(
    value: ToolInputExtractionInput,
) -> list[dict[str, str]]:
    context = {
        "tool_context_ref": {
            "tool_id": value.tool_context_ref.tool_id,
            "version": value.tool_context_ref.version,
            "schema_hash": value.tool_context_ref.schema_hash,
        },
        "candidate_input_schema": _plain_json(value.candidate_input_schema),
        "missing_fields": list(value.missing_fields),
        "ambiguous_fields": list(value.ambiguous_fields),
    }
    return _controlled_messages(
        _TOOL_INPUT_EXTRACTION_TEMPLATE.format_messages(
            context=json.dumps(
                context,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            content_text=value.content_text,
        )
    )


def render_tool_result_explanation_prompt(
    value: ExplanationInput,
) -> list[dict[str, str]]:
    projection = {
        "result_id": value.result_id,
        "status": value.status,
        "requested_outputs": list(value.requested_outputs),
        "completed_outputs": list(value.completed_outputs),
        "failed_outputs": list(value.failed_outputs),
        "data": _plain_json(value.data),
        "artifacts": [
            {
                "asset_id": item.asset_id,
                "role": item.role,
                "asset_type": item.asset_type,
            }
            for item in value.artifacts
        ],
        "warnings": _plain_json(value.warnings),
        "error": _plain_json(value.error),
        "process_parameters": _plain_json(value.process_parameters),
        "tool_id": value.tool_id,
        "tool_version": value.tool_version,
        "schema_hash": value.schema_hash,
    }
    return _controlled_messages(
        _TOOL_RESULT_EXPLANATION_TEMPLATE.format_messages(
            result_payload=json.dumps(
                projection,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    )
