from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from materialsagent.domain.ports.tool_registry import (
    RoutingCatalogEntry,
    ToolDefinition,
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PublicToolCatalogItem:
    tool_id: str
    display_name: str
    description: str
    status: str
    execution_profile: str
    confirmation_required: bool
    supported_outputs: tuple[str, ...]
    limitations: tuple[str, ...]
    availability: str

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "display_name": self.display_name,
            "description": self.description,
            "status": self.status,
            "execution_profile": self.execution_profile,
            "confirmation_required": self.confirmation_required,
            "supported_outputs": list(self.supported_outputs),
            "limitations": list(self.limitations),
            "availability": self.availability,
        }


@dataclass(frozen=True, slots=True)
class LLMToolProjection:
    model_tool_name: str
    description: str
    proposal_schema: Mapping[str, object]

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "model_tool_name": self.model_tool_name,
            "description": self.description,
            "proposal_schema": _plain_json(self.proposal_schema),
        }

    def to_langchain_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.model_tool_name,
                "description": self.description,
                "parameters": _plain_json(self.proposal_schema),
            },
        }


@dataclass(frozen=True, slots=True)
class InvocationToolProjection:
    tool_id: str
    version: str
    display_name: str
    execution_profile: str
    confirmation_required: bool
    confirmation_prompt: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "display_name": self.display_name,
            "execution_profile": self.execution_profile,
            "confirmation_required": self.confirmation_required,
            "confirmation_prompt": self.confirmation_prompt,
        }


class ToolProjectionService:
    @staticmethod
    def for_catalog(
        definition: ToolDefinition,
        *,
        availability: str,
    ) -> PublicToolCatalogItem:
        return PublicToolCatalogItem(
            tool_id=definition.tool_id,
            display_name=definition.display_name,
            description=definition.description,
            status=definition.status.value,
            execution_profile=definition.execution_profile.value,
            confirmation_required=(
                definition.tool_execution_policy.confirmation_required
            ),
            supported_outputs=definition.supported_outputs,
            limitations=definition.limitations,
            availability=availability,
        )

    @staticmethod
    def for_llm(definition: ToolDefinition) -> LLMToolProjection:
        return LLMToolProjection(
            model_tool_name=definition.tool_id,
            description=definition.description,
            proposal_schema=definition.candidate_input_schema,
        )

    @staticmethod
    def for_llm_snapshot_entry(entry: RoutingCatalogEntry) -> LLMToolProjection:
        """Project an internal Routing Snapshot through the same LLM allowlist."""

        return LLMToolProjection(
            model_tool_name=entry.tool_id,
            description=entry.description,
            proposal_schema=entry.candidate_input_schema or {},
        )

    @staticmethod
    def for_invocation(definition: ToolDefinition) -> InvocationToolProjection:
        return InvocationToolProjection(
            tool_id=definition.tool_id,
            version=definition.version,
            display_name=definition.display_name,
            execution_profile=definition.execution_profile.value,
            confirmation_required=(
                definition.tool_execution_policy.confirmation_required
            ),
            confirmation_prompt=definition.confirmation_prompt,
        )
