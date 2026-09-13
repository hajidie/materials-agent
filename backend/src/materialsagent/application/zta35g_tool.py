from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Final

from materialsagent.application.zta35g_input import normalize_zta35g_candidate
from materialsagent.domain.ports.tool_execution import (
    MaterialTool,
    ToolClientPort,
    ToolClientUnavailableError,
    ToolExecutionInput,
    ToolExecutionOutput,
    ToolMetadata,
    ToolRequestContext,
)
from materialsagent.domain.ports.tool_registry import (
    ExecutionMode,
    ExecutionPolicy,
    InvalidNormalization,
    NeedsInputNormalization,
    PresentationMode,
    ReadyNormalization,
    RegisteredTool,
    ToolExecutionBinding,
    ToolExecutionPolicy,
    ToolExecutionProfile,
    ToolDefinition,
    ToolStatus,
)


ZTA35G_TOOL_ID: Final = "zta35g_sem_virtual_lab"
SUPPORTED_OUTPUTS: Final = ("sem_image", "mechanical_properties")
PROCESS_FIELDS: Final = (
    "solution_temperature", "solution_time", "aging_temperature", "aging_time"
)


class _UnavailableToolClient:
    def execute(self, *_args: object, **_kwargs: object) -> ToolExecutionOutput:
        raise ToolClientUnavailableError()

    def readiness(self, _metadata: ToolMetadata) -> str:
        return "UNAVAILABLE"


class ZTA35GMaterialTool:
    def __init__(self, metadata: ToolMetadata, client: ToolClientPort) -> None:
        self.metadata = metadata
        self._client = client

    def validate_input(self, normalized_input: dict[str, object], *, seed: int) -> ToolExecutionInput:
        expected_keys = {"material", *PROCESS_FIELDS, "requested_outputs"}
        if not isinstance(normalized_input, dict) or set(normalized_input) != expected_keys:
            raise ValueError("Normalized Tool input is invalid.")
        if normalized_input.get("material") != "ZTA35G":
            raise ValueError("Normalized Tool input is invalid.")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("seed must be a nonnegative integer.")

        process_parameters: dict[str, int | float] = {}
        expected_units = {
            "solution_temperature": "°C", "solution_time": "h",
            "aging_temperature": "°C", "aging_time": "h",
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
            if not integer_required and decimal_value * 10 != (decimal_value * 10).to_integral_value():
                raise ValueError("Normalized Tool input is invalid.")
            process_parameters[field_name] = value

        outputs = normalized_input.get("requested_outputs")
        if (
            not isinstance(outputs, list) or not outputs
            or any(not isinstance(item, str) for item in outputs)
            or len(set(outputs)) != len(outputs)
            or not set(outputs) <= set(SUPPORTED_OUTPUTS)
        ):
            raise ValueError("Normalized Tool input is invalid.")
        return ToolExecutionInput(
            process_parameters=process_parameters,
            requested_outputs=tuple(outputs),
            runtime_parameters={"seed": seed, "num_samples": 1, "guide_scale": 2.0, "timesteps": 1000},
        )

    def execute(self, validated_input: ToolExecutionInput, request_context: ToolRequestContext) -> ToolExecutionOutput:
        return self._client.execute(self.metadata, validated_input, request_context)

    def health_check(self) -> str:
        return self._client.readiness(self.metadata)


def _zta35g_metadata() -> ToolMetadata:
    return ToolMetadata(
        tool_id=ZTA35G_TOOL_ID, tool_version="0.1.0", schema_version="1.0",
        display_name="ZTA35G SEM 虚拟实验室", description="根据四维热处理工艺参数生成 SEM 并按需预测力学性能。",
        material_scope="ZTA35G", enabled=True, supported_outputs=SUPPORTED_OUTPUTS,
        supported_asset_types=("sem_image",), execution_mode="LOCAL_RUNTIME", requires_gpu=True,
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
        limitations=("仅适用于 ZTA35G 钛合金。", "不支持上传真实 SEM 后预测。", "只请求力学性能时仍会生成中间 SEM。"),
    )


def _normalizer(
    candidate_input: Mapping[str, object],
    prior_normalized_input: Mapping[str, object] | None = None,
) -> ReadyNormalization | NeedsInputNormalization | InvalidNormalization:
    if not isinstance(candidate_input, Mapping):
        raise ValueError("ZTA35G candidate input must be an object.")
    if prior_normalized_input is not None and not isinstance(
        prior_normalized_input,
        Mapping,
    ):
        raise ValueError("ZTA35G prior input must be an object.")
    validation = normalize_zta35g_candidate(
        candidate_input,
        prior_normalized_input=prior_normalized_input,
    )
    payloads = validation.to_revision_payloads()
    normalized_input = payloads["normalized_input"]
    if not isinstance(normalized_input, dict):
        raise ValueError("ZTA35G normalized input is invalid.")
    if validation.validation_errors:
        return InvalidNormalization(
            normalized_input=normalized_input,
            validation_errors=tuple(payloads["validation_errors"]),
        )
    if validation.missing_fields or validation.ambiguous_fields:
        return NeedsInputNormalization(
            normalized_input=normalized_input,
            missing_fields=validation.missing_fields,
            ambiguous_fields=tuple(
                item.field for item in validation.ambiguous_fields
            ),
            follow_up_suggestion="请补充或澄清 ZTA35G 工艺参数。",
        )
    return ReadyNormalization(
        normalized_input=normalized_input,
        requested_outputs=validation.normalized_input.requested_outputs,
    )


def _candidate_input_schema() -> dict[str, object]:
    ambiguity = {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "items": {"type": ["string", "number", "boolean", "null"]},
            }
        },
    }

    def scalar(*types: str) -> dict[str, object]:
        return {
            "anyOf": [
                {"type": list(types)},
                ambiguity,
            ]
        }

    def parameter(unit_values: list[str]) -> dict[str, object]:
        return {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["value", "unit"],
            "properties": {
                "value": scalar("number", "null"),
                "unit": {
                    "anyOf": [
                        {"type": "string", "enum": unit_values},
                        {"type": "null"},
                        ambiguity,
                    ]
                },
            },
        }

    properties: dict[str, object] = {
        "material": {
            "anyOf": [
                {"const": "ZTA35G"},
                {"type": "null"},
                ambiguity,
            ]
        },
        "solution_temperature": parameter(["°C"]),
        "solution_time": parameter(["h", "min"]),
        "aging_temperature": parameter(["°C"]),
        "aging_time": parameter(["h", "min"]),
        "requested_outputs": {
            "type": "array",
            "minItems": 1,
            "maxItems": len(SUPPORTED_OUTPUTS),
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(SUPPORTED_OUTPUTS)},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }


def _context_projection(value: Mapping[str, object]) -> Mapping[str, object]:
    data = value.get("data")
    mechanical_properties: dict[str, object] = {}
    if isinstance(data, Mapping):
        for field_name in ("yield_strength", "elongation"):
            item = data.get(field_name)
            if isinstance(item, Mapping) and set(item) == {"value", "unit"}:
                mechanical_properties[field_name] = {
                    "value": item["value"],
                    "unit": item["unit"],
                }
    error = value.get("error")
    safe_error = (
        {
            field_name: error[field_name]
            for field_name in ("code", "safe_message", "retryable")
        }
        if isinstance(error, Mapping)
        and set(error) == {"code", "safe_message", "retryable"}
        else None
    )
    return {
        "status": value["status"],
        "requested_outputs": value["requested_outputs"],
        "completed_outputs": value["completed_outputs"],
        "failed_outputs": value["failed_outputs"],
        "mechanical_properties": mechanical_properties,
        "error": safe_error,
    }


def build_zta35g_tool_definition(client: ToolClientPort | None = None) -> RegisteredTool:
    metadata = _zta35g_metadata()
    tool: MaterialTool = ZTA35GMaterialTool(metadata, client or _UnavailableToolClient())
    definition = ToolDefinition(
        tool_id=ZTA35G_TOOL_ID, version="1", status=ToolStatus.ACTIVE,
        display_name=metadata.display_name,
        description=metadata.description,
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["material", *PROCESS_FIELDS, "requested_outputs"],
            "properties": {
                "material": {"const": "ZTA35G"},
                **{name: {"type": "object", "additionalProperties": False,
                    "required": ["value", "unit"], "properties": {
                        "value": {"type": "number", "minimum": lower, "maximum": upper, "multipleOf": precision},
                        "unit": {"const": unit}}}
                    for name, unit, lower, upper, precision in (
                        ("solution_temperature", "°C", 900, 1100, 1),
                        ("solution_time", "h", 1, 5, 0.1),
                        ("aging_temperature", "°C", 670, 790, 1),
                        ("aging_time", "h", 1, 5, 0.1))},
                "requested_outputs": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
                    "items": {"enum": list(SUPPORTED_OUTPUTS)}},
            },
        },
        proposal_schema=_candidate_input_schema(),
        output_schema={"type": "object", "additionalProperties": False,
            "properties": {
                name: {"type": "object", "additionalProperties": False, "required": ["value", "unit"],
                    "properties": {"value": {"type": "number"}, "unit": {"const": unit}}}
                for name, unit in (("yield_strength", "MPa"), ("elongation", "%"))
            },
            "oneOf": [{"maxProperties": 0}, {"required": ["yield_strength", "elongation"]}]},
        runtime_metadata=metadata, supported_outputs=SUPPORTED_OUTPUTS,
        supported_asset_types=metadata.supported_asset_types, limitations=metadata.limitations,
        execution_profile=ToolExecutionProfile.MANAGED,
        execution_mode=ExecutionMode.SYNC,
        executor_id="managed_runtime",
        tool_execution_policy=ToolExecutionPolicy(
            lifecycle_policy=ExecutionPolicy.ANY_TASK,
        ),
        presentation_mode=PresentationMode.CUSTOM,
        presenter_id="zta35g_result",
        context_projection_version="zta35g-result-context-v1",
    )
    return RegisteredTool(
        definition=definition,
        binding=ToolExecutionBinding(
            execution_target=tool,
            normalizer=_normalizer,
            context_projector=_context_projection,
            health_probe=tool.health_check,
        ),
    )
