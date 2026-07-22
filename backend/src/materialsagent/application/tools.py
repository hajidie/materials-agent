from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from materialsagent.domain.ports.tool_execution import (
    MaterialTool,
    ToolClientPort,
    ToolClientUnavailableError,
    ToolExecutionInput,
    ToolExecutionOutput,
    ToolMetadata,
    ToolRequestContext,
)


ZTA35G_TOOL_ID: Final = "zta35g_sem_virtual_lab"
SUPPORTED_OUTPUTS: Final = ("sem_image", "mechanical_properties")
PROCESS_FIELDS: Final = (
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
)


class UnknownToolError(LookupError):
    """Requested Tool is not in the static Registry."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    metadata: ToolMetadata
    tool: MaterialTool


class StaticToolRegistry:
    def __init__(self, registrations: tuple[RegisteredTool, ...]) -> None:
        if not registrations:
            raise ValueError("At least one Tool registration is required.")
        self._registrations: dict[str, RegisteredTool] = {}
        for registration in registrations:
            tool_id = registration.metadata.tool_id
            if tool_id in self._registrations:
                raise ValueError("Duplicate Tool registration.")
            if registration.tool.metadata != registration.metadata:
                raise ValueError("Tool metadata must be Registry-owned.")
            self._registrations[tool_id] = registration

    def resolve(self, tool_id: str) -> RegisteredTool:
        registration = self._registrations.get(tool_id)
        if registration is None:
            raise UnknownToolError("Unknown Tool.")
        return registration

    def list_registered(self) -> tuple[RegisteredTool, ...]:
        return tuple(
            self._registrations[tool_id]
            for tool_id in sorted(self._registrations)
        )


class _UnavailableToolClient:
    def execute(self, *_args: object, **_kwargs: object) -> ToolExecutionOutput:
        raise ToolClientUnavailableError()

    def readiness(self, _metadata: ToolMetadata) -> str:
        return "UNAVAILABLE"


class ZTA35GMaterialTool:
    def __init__(
        self,
        metadata: ToolMetadata,
        client: ToolClientPort,
    ) -> None:
        self.metadata = metadata
        self._client = client

    def validate_input(
        self,
        normalized_input: dict[str, object],
        *,
        seed: int,
    ) -> ToolExecutionInput:
        expected_keys = {"material", *PROCESS_FIELDS, "requested_outputs"}
        if not isinstance(normalized_input, dict) or set(normalized_input) != expected_keys:
            raise ValueError("Normalized Tool input is invalid.")
        if normalized_input.get("material") != "ZTA35G":
            raise ValueError("Normalized Tool input is invalid.")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("seed must be a nonnegative integer.")

        process_parameters: dict[str, int | float] = {}
        expected_units = {
            "solution_temperature": "°C",
            "solution_time": "h",
            "aging_temperature": "°C",
            "aging_time": "h",
        }
        ranges = {
            "solution_temperature": (Decimal("900"), Decimal("1100"), True),
            "solution_time": (Decimal("1"), Decimal("5"), False),
            "aging_temperature": (Decimal("670"), Decimal("790"), True),
            "aging_time": (Decimal("1"), Decimal("5"), False),
        }
        for field_name in PROCESS_FIELDS:
            payload = normalized_input.get(field_name)
            if not isinstance(payload, dict) or set(payload) != {"value", "unit"}:
                raise ValueError("Normalized Tool input is invalid.")
            if payload.get("unit") != expected_units[field_name]:
                raise ValueError("Normalized Tool input is invalid.")
            value = payload.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Normalized Tool input is invalid.")
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError("Normalized Tool input is invalid.") from None
            lower, upper, integer_required = ranges[field_name]
            if not decimal_value.is_finite() or not lower <= decimal_value <= upper:
                raise ValueError("Normalized Tool input is invalid.")
            if integer_required and decimal_value != decimal_value.to_integral_value():
                raise ValueError("Normalized Tool input is invalid.")
            if not integer_required and decimal_value * 10 != (
                decimal_value * 10
            ).to_integral_value():
                raise ValueError("Normalized Tool input is invalid.")
            process_parameters[field_name] = value

        outputs = normalized_input.get("requested_outputs")
        if (
            not isinstance(outputs, list)
            or not outputs
            or any(not isinstance(item, str) for item in outputs)
            or len(set(outputs)) != len(outputs)
            or not set(outputs) <= set(SUPPORTED_OUTPUTS)
        ):
            raise ValueError("Normalized Tool input is invalid.")
        return ToolExecutionInput(
            process_parameters=process_parameters,
            requested_outputs=tuple(outputs),
            runtime_parameters={
                "seed": seed,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
        )

    def execute(
        self,
        validated_input: ToolExecutionInput,
        request_context: ToolRequestContext,
    ) -> ToolExecutionOutput:
        return self._client.execute(
            self.metadata,
            validated_input,
            request_context,
        )

    def health_check(self) -> str:
        return self._client.readiness(self.metadata)


def _zta35g_metadata() -> ToolMetadata:
    return ToolMetadata(
        tool_id=ZTA35G_TOOL_ID,
        tool_version="0.1.0",
        schema_version="1.0",
        display_name="ZTA35G SEM 虚拟实验室",
        description="根据四维热处理工艺参数生成 SEM 并按需预测力学性能。",
        material_scope="ZTA35G",
        enabled=True,
        supported_outputs=SUPPORTED_OUTPUTS,
        supported_asset_types=("sem_image",),
        execution_mode="LOCAL_RUNTIME",
        requires_gpu=True,
        input_fields=(
            {"name": "solution_temperature", "unit": "°C", "minimum": 900, "maximum": 1100, "precision": 0},
            {"name": "solution_time", "unit": "h", "minimum": 1, "maximum": 5, "precision": 1},
            {"name": "aging_temperature", "unit": "°C", "minimum": 670, "maximum": 790, "precision": 0},
            {"name": "aging_time", "unit": "h", "minimum": 1, "maximum": 5, "precision": 1},
        ),
        output_summary=(
            {"name": "sem_image", "kind": "asset"},
            {"name": "mechanical_properties", "kind": "structured_data"},
        ),
        limitations=(
            "仅适用于 ZTA35G 钛合金。",
            "不支持上传真实 SEM 后预测。",
            "只请求力学性能时仍会生成中间 SEM。",
        ),
    )


def build_tool_registry(
    client: ToolClientPort | None = None,
) -> StaticToolRegistry:
    metadata = _zta35g_metadata()
    tool = ZTA35GMaterialTool(metadata, client or _UnavailableToolClient())
    return StaticToolRegistry((RegisteredTool(metadata=metadata, tool=tool),))


class ToolCatalogService:
    def __init__(self, registry: StaticToolRegistry) -> None:
        self._registry = registry

    def list_entries(self) -> list[dict[str, object]]:
        return [
            self._project(registration)
            for registration in self._registry.list_registered()
        ]

    def get_entry(self, tool_id: str) -> dict[str, object]:
        return self._project(self._registry.resolve(tool_id))

    @staticmethod
    def _project(registration: RegisteredTool) -> dict[str, object]:
        metadata = registration.metadata
        try:
            availability = registration.tool.health_check()
        except Exception:
            availability = "UNAVAILABLE"
        if availability not in {"AVAILABLE", "UNAVAILABLE", "DEGRADED"}:
            availability = "UNAVAILABLE"
        return {
            "tool_id": metadata.tool_id,
            "display_name": metadata.display_name,
            "description": metadata.description,
            "material_scope": metadata.material_scope,
            "enabled": metadata.enabled,
            "availability": availability,
            "tool_version": metadata.tool_version,
            "schema_version": metadata.schema_version,
            "supported_outputs": list(metadata.supported_outputs),
            "input_fields": [dict(item) for item in metadata.input_fields],
            "output_summary": [dict(item) for item in metadata.output_summary],
            "supported_asset_types": list(metadata.supported_asset_types),
            "limitations": list(metadata.limitations),
        }
