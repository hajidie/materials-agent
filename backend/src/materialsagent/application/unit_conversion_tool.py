from __future__ import annotations

from collections.abc import Mapping
import math

from materialsagent.domain.ports.tool_execution import ToolMetadata
from materialsagent.domain.ports.tool_registry import (
    ExecutionMode,
    ExecutionPolicy,
    PresentationMode,
    RegisteredTool,
    ToolDefinition,
    ToolExecutionBinding,
    ToolExecutionPolicy,
    ToolExecutionProfile,
    ToolStatus,
)


UNIT_CONVERSION_TOOL_ID = "materials_unit_conversion"
_DIMENSION = {
    "°C": "temperature",
    "K": "temperature",
    "°F": "temperature",
    "s": "time",
    "min": "time",
    "h": "time",
    "Pa": "pressure",
    "MPa": "pressure",
    "GPa": "pressure",
}


def _validate(arguments: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(arguments, Mapping) or set(arguments) != {
        "value",
        "from_unit",
        "to_unit",
    }:
        raise ValueError("Unit conversion arguments are invalid.")
    value = arguments["value"]
    from_unit = arguments["from_unit"]
    to_unit = arguments["to_unit"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or type(from_unit) is not str
        or type(to_unit) is not str
        or from_unit not in _DIMENSION
        or to_unit not in _DIMENSION
        or _DIMENSION[from_unit] != _DIMENSION[to_unit]
    ):
        raise ValueError("Unit conversion arguments are invalid.")
    return {
        "value": float(value),
        "from_unit": from_unit,
        "to_unit": to_unit,
    }


def _to_base(value: float, unit: str) -> float:
    if unit == "°C":
        return value + 273.15
    if unit == "°F":
        return (value - 32.0) * 5.0 / 9.0 + 273.15
    if unit == "K":
        return value
    if unit == "s":
        return value
    if unit == "min":
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    if unit == "Pa":
        return value
    if unit == "MPa":
        return value * 1_000_000.0
    if unit == "GPa":
        return value * 1_000_000_000.0
    raise ValueError("Unsupported unit.")


def _from_base(value: float, unit: str) -> float:
    if unit == "°C":
        return value - 273.15
    if unit == "°F":
        return (value - 273.15) * 9.0 / 5.0 + 32.0
    if unit == "K":
        return value
    if unit == "s":
        return value
    if unit == "min":
        return value / 60.0
    if unit == "h":
        return value / 3600.0
    if unit == "Pa":
        return value
    if unit == "MPa":
        return value / 1_000_000.0
    if unit == "GPa":
        return value / 1_000_000_000.0
    raise ValueError("Unsupported unit.")


class MaterialsUnitConversionTool:
    def invoke(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validated = _validate(arguments)
        converted = _from_base(
            _to_base(float(validated["value"]), str(validated["from_unit"])),
            str(validated["to_unit"]),
        )
        return {
            "input_value": validated["value"],
            "input_unit": validated["from_unit"],
            "value": converted,
            "unit": validated["to_unit"],
        }


def _codec(result: object) -> Mapping[str, object]:
    if not isinstance(result, Mapping) or set(result) != {
        "input_value",
        "input_unit",
        "value",
        "unit",
    }:
        raise ValueError("Unit conversion result is invalid.")
    return dict(result)


def _present(result: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "title": "单位换算完成",
        "summary": (
            f"{result['input_value']:g} {result['input_unit']} = "
            f"{result['value']:g} {result['unit']}"
        ),
        "data": dict(result),
    }


def build_unit_conversion_registered_tool() -> RegisteredTool:
    target = MaterialsUnitConversionTool()
    metadata = ToolMetadata(
        tool_id=UNIT_CONVERSION_TOOL_ID,
        tool_version="1",
        schema_version="1",
        display_name="材料单位换算",
        description="换算材料实验常用的温度、时间与压力单位。",
        material_scope="GENERAL_MATERIALS",
        enabled=True,
        supported_outputs=("converted_value",),
        supported_asset_types=(),
        execution_mode="IN_PROCESS_SYNC",
        requires_gpu=False,
        input_fields=(),
        output_summary=(),
        limitations=("仅支持同量纲单位之间的确定性换算。",),
    )
    units = list(_DIMENSION)
    definition = ToolDefinition(
        tool_id=UNIT_CONVERSION_TOOL_ID,
        version="1",
        status=ToolStatus.ACTIVE,
        display_name=metadata.display_name,
        description=metadata.description,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value", "from_unit", "to_unit"],
            "properties": {
                "value": {"type": "number"},
                "from_unit": {"type": "string", "enum": units},
                "to_unit": {"type": "string", "enum": units},
            },
        },
        proposal_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value", "from_unit", "to_unit"],
            "properties": {
                "value": {"type": "number"},
                "from_unit": {"type": "string", "enum": units},
                "to_unit": {"type": "string", "enum": units},
            },
        },
        output_schema={
            "type": "object",
            "required": ["input_value", "input_unit", "value", "unit"],
        },
        runtime_metadata=metadata,
        supported_outputs=metadata.supported_outputs,
        supported_asset_types=(),
        limitations=metadata.limitations,
        execution_profile=ToolExecutionProfile.STANDARD,
        execution_mode=ExecutionMode.SYNC,
        executor_id="standard_sync",
        tool_execution_policy=ToolExecutionPolicy(
            lifecycle_policy=ExecutionPolicy.ANY_TASK,
            required_permissions=(),
            confirmation_required=False,
        ),
        presentation_mode=PresentationMode.DETERMINISTIC,
        presenter_id="unit_conversion",
    )
    return RegisteredTool(
        definition=definition,
        binding=ToolExecutionBinding(
            execution_target=target,
            validator=_validate,
            codec=_codec,
            presenter=_present,
            health_probe=lambda: "AVAILABLE",
        ),
    )
