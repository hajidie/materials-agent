"""Single-image Inconel 625 prediction registration."""
import re
from collections.abc import Mapping

from materialsagent.domain.ports.tool_execution import ToolExecutionInput, ToolMetadata, ToolClientUnavailableError
from materialsagent.domain.ports.tool_registry import (
    ToolDefinition, ToolExecutionBinding, RegisteredTool, ToolStatus, ToolExecutionProfile,
    ExecutionMode, ToolExecutionPolicy, ExecutionPolicy, PresentationMode,
    ReadyNormalization, NeedsInputNormalization, InvalidNormalization,
)

TOOL_ID = "ebsd_yield_strength_predictor"
PROPERTIES = {
    "material": {"const": "Inconel 625"},
    "ebsd_asset_id": {"type": "string", "pattern": "^asset_[A-Za-z0-9_-]{1,90}$"},
    "requested_outputs": {"type": "array", "items": {"const": "yield_strength"}, "minItems": 1, "maxItems": 1},
}


def normalize(candidate, prior_normalized_input=None):
    if not isinstance(candidate, Mapping) or set(candidate) - set(PROPERTIES):
        return InvalidNormalization({"ebsd_asset_id": None}, ({"field": "ebsd_asset_id", "code": "INVALID_INPUT"},))
    value = {"material": "Inconel 625", "ebsd_asset_id": None, "requested_outputs": ["yield_strength"]}
    value.update(prior_normalized_input or {})
    value.update(candidate)
    issues = []
    if value["material"] != "Inconel 625":
        issues.append({"field": "material", "code": "UNSUPPORTED_MATERIAL"})
    if isinstance(value["requested_outputs"], tuple):
        value["requested_outputs"] = list(value["requested_outputs"])
    if value["requested_outputs"] != ["yield_strength"]:
        issues.append({"field": "requested_outputs", "code": "UNSUPPORTED_OUTPUT"})
    asset = value["ebsd_asset_id"]
    if asset is not None and (not isinstance(asset, str) or not re.fullmatch(r"asset_[A-Za-z0-9_-]{1,90}", asset)):
        issues.append({"field": "ebsd_asset_id", "code": "INVALID_ASSET"})
    if issues:
        return InvalidNormalization(value, tuple(issues))
    if asset is None:
        return NeedsInputNormalization(value, ("ebsd_asset_id",), (), "请上传一张 Inconel 625 EBSD 图片。")
    return ReadyNormalization(value, ("yield_strength",))


class EBSDTool:
    def __init__(self, metadata, client):
        self.metadata, self.client = metadata, client

    def validate_input(self, normalized_input, *, seed):
        checked = normalize(normalized_input)
        if not isinstance(checked, ReadyNormalization) or set(normalized_input) != set(PROPERTIES):
            raise ValueError("Invalid normalized EBSD input.")
        return ToolExecutionInput({}, ("yield_strength",), {"seed": seed},
            {"ebsd_asset_id": normalized_input["ebsd_asset_id"]})

    def execute(self, validated_input, request_context):
        if self.client is None:
            raise ToolClientUnavailableError()
        return self.client.execute(self.metadata, validated_input, request_context)

    def health_check(self):
        return self.client.readiness(self.metadata) if self.client else "UNAVAILABLE"


def project(value):
    return {key: value[key] for key in ("status", "requested_outputs", "completed_outputs", "failed_outputs", "data", "warnings", "error") if key in value}


def present(result):
    if result["status"] != "SUCCEEDED":
        return {}
    value = result["data"]["yield_strength"]["value"]
    model = result["provenance"]["model_version"]
    warning = "Mock 模拟结果，不是真实模型预测。" if model == "mock-ebsd-bundle" else "实验性预测；图像编码要求与适用数据分布尚未核实。"
    return {"title": "EBSD 屈服强度预测完成", "summary": f"Inconel 625 屈服强度预测值为 {value:.2f} MPa。{warning}"}


def build_ebsd_tool(client=None):
    metadata = ToolMetadata(tool_id=TOOL_ID, tool_version="0.1.0", schema_version="1.0",
        display_name="EBSD 屈服强度预测", description="使用已上传 EBSD 图片预测 Inconel 625 屈服强度；需要 ebsd_asset_id，不接受 SEM 图片。",
        material_scope="Inconel 625", enabled=True, supported_outputs=("yield_strength",),
        supported_asset_types=("ebsd_image",), execution_mode="LOCAL_RUNTIME", requires_gpu=True,
        input_fields=({"name": "ebsd_asset_id", "type": "asset"},),
        output_summary=({"name": "yield_strength", "kind": "structured_data", "unit": "MPa"},),
        limitations=("实验性预测，仅适用于 Inconel 625。", "图像编码要求与适用数据分布尚未核实。"))
    tool = EBSDTool(metadata, client)
    definition = ToolDefinition(tool_id=TOOL_ID, version="1", status=ToolStatus.ACTIVE,
        display_name=metadata.display_name, description=metadata.description,
        input_schema={"type": "object", "additionalProperties": False, "required": list(PROPERTIES), "properties": PROPERTIES},
        proposal_schema={"type": "object", "additionalProperties": False, "properties": PROPERTIES},
        output_schema={"type": "object", "additionalProperties": False, "required": ["yield_strength"],
            "properties": {"yield_strength": {"type": "object", "additionalProperties": False,
                "required": ["value", "unit"], "properties": {"value": {"type": "number"}, "unit": {"const": "MPa"}}}}},
        runtime_metadata=metadata, supported_outputs=metadata.supported_outputs,
        supported_asset_types=metadata.supported_asset_types, limitations=metadata.limitations,
        execution_profile=ToolExecutionProfile.MANAGED, execution_mode=ExecutionMode.SYNC,
        executor_id="managed_runtime", tool_execution_policy=ToolExecutionPolicy(lifecycle_policy=ExecutionPolicy.ANY_TASK),
        presentation_mode=PresentationMode.CUSTOM, presenter_id="ebsd_result", context_projection_version="ebsd-result-v1")
    return RegisteredTool(definition, ToolExecutionBinding(execution_target=tool, normalizer=normalize,
        context_projector=project, presenter=present, health_probe=tool.health_check))
