from __future__ import annotations

from materialsagent.application.tool_registry import ToolRegistry, UnknownToolError
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.domain.ports.tool_execution import ToolClientPort
from materialsagent.domain.ports.tool_registry import (
    ExecutionPolicy,
    ToolDefinition,
    ToolStatus,
)


StaticToolRegistry = ToolRegistry


def build_tool_registry(client: ToolClientPort | None = None) -> ToolRegistry:
    """Explicit production composition root: this MVP registers ZTA35G only."""
    return ToolRegistry((build_zta35g_tool_definition(client),))


class ToolCatalogService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def list_entries(self) -> list[dict[str, object]]:
        return [self._project(definition) for definition in self._registry.list_registered()]

    def get_entry(self, tool_id: str) -> dict[str, object]:
        return self._project(self._registry.resolve(tool_id))

    @staticmethod
    def _project(definition: ToolDefinition) -> dict[str, object]:
        metadata = definition.runtime_metadata
        if definition.status is ToolStatus.DISABLED:
            availability = "NOT_APPLICABLE"
        else:
            try:
                availability = definition.tool.health_check()
            except Exception:
                availability = "UNAVAILABLE"
            if availability not in {"AVAILABLE", "UNAVAILABLE", "DEGRADED"}:
                availability = "UNAVAILABLE"
        return {
            "tool_id": definition.tool_id,
            "display_name": definition.display_name,
            "description": definition.description,
            "material_scope": metadata.material_scope,
            "enabled": definition.execution_policy is not ExecutionPolicy.NONE,
            "status": definition.status.value,
            "execution_policy": definition.execution_policy.value,
            "version": definition.version,
            "schema_hash": definition.schema_hash,
            "availability": availability,
            "tool_version": metadata.tool_version,
            "schema_version": metadata.schema_version,
            "supported_outputs": list(definition.supported_outputs),
            "input_fields": [dict(item) for item in metadata.input_fields],
            "output_summary": [dict(item) for item in metadata.output_summary],
            "supported_asset_types": list(definition.supported_asset_types),
            "limitations": list(definition.limitations),
        }
