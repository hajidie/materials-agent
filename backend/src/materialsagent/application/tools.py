from __future__ import annotations

from materialsagent.application.tool_registry import ToolRegistry, UnknownToolError
from materialsagent.application.fake_side_effect_tool import (
    FakeSideEffectSink,
    build_fake_side_effect_registered_tool,
)
from materialsagent.application.tool_projections import ToolProjectionService
from materialsagent.application.unit_conversion_tool import (
    build_unit_conversion_registered_tool,
)
from materialsagent.application.ebsd_tool import build_ebsd_tool
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.domain.ports.tool_execution import ToolClientPort
from materialsagent.domain.ports.tool_registry import (
    RegisteredTool,
    ToolStatus,
)


StaticToolRegistry = ToolRegistry


def build_tool_registry(
    client: ToolClientPort | None = None,
    *,
    ebsd_client: ToolClientPort | None = None,
    enable_dev_fake_side_effect_tool: bool = False,
    fake_side_effect_sink: FakeSideEffectSink | None = None,
) -> ToolRegistry:
    """Explicit production composition root for the safe production catalog."""
    registrations = [
        build_zta35g_tool_definition(client),
        build_unit_conversion_registered_tool(),
        build_ebsd_tool(ebsd_client),
    ]
    if enable_dev_fake_side_effect_tool:
        registrations.append(
            build_fake_side_effect_registered_tool(
                fake_side_effect_sink or FakeSideEffectSink()
            )
        )
    return ToolRegistry(tuple(registrations))


class ToolCatalogService:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def list_entries(self) -> list[dict[str, object]]:
        return [self._project(definition) for definition in self._registry.list_registered()]

    def get_entry(self, tool_id: str) -> dict[str, object]:
        return self._project(self._registry.resolve(tool_id))

    @staticmethod
    def _project(registration: RegisteredTool) -> dict[str, object]:
        definition = registration.definition
        if definition.status is ToolStatus.DISABLED:
            availability = "NOT_APPLICABLE"
        else:
            try:
                probe = registration.binding.health_probe
                availability = "UNAVAILABLE" if probe is None else probe()
            except Exception:
                availability = "UNAVAILABLE"
            if availability not in {"AVAILABLE", "UNAVAILABLE", "DEGRADED"}:
                availability = "UNAVAILABLE"
        return ToolProjectionService.for_catalog(
            definition,
            availability=availability,
        ).to_dict()
