"""A single Managed call binds an ordered collection of TC4 images."""
from collections.abc import Mapping
import re
from materialsagent.domain.models.tc4 import TOOL_ID, OUTPUTS
from materialsagent.domain.ports.tool_execution import ToolExecutionInput, ToolMetadata, ToolClientUnavailableError
from materialsagent.domain.ports.tool_registry import (
    ToolDefinition, ToolExecutionBinding, RegisteredTool, ToolStatus, ToolExecutionProfile,
    ExecutionMode, ToolExecutionPolicy, PresentationMode, ReadyNormalization,
    NeedsInputNormalization, InvalidNormalization, ResourceParameterSpec, ResourceProvider, ResourceType)

PROPERTIES = {"image_asset_ids": {"type": "array", "items": {"type": "string", "pattern": "^asset_[A-Za-z0-9_-]{1,90}$"},
    "minItems": 1, "maxItems": 10, "uniqueItems": True}}


def normalize(candidate, prior_normalized_input=None):
    if not isinstance(candidate, Mapping) or set(candidate) - set(PROPERTIES):
        return InvalidNormalization({}, ({"field": "image_asset_ids", "code": "INVALID_INPUT"},))
    value = {**(prior_normalized_input or {}), **candidate}
    images = value.get("image_asset_ids")
    if images is None:
        return NeedsInputNormalization(value, ("image_asset_ids",), (), "请上传需要分割的 TC4 初生 α 相图片。")
    if (not isinstance(images, (tuple, list)) or not 1 <= len(images) <= 10
            or any(not isinstance(item, str) or not re.fullmatch(r"asset_[A-Za-z0-9_-]{1,90}", item) for item in images)
            or len(set(images)) != len(images)):
        return InvalidNormalization(value, ({"field": "image_asset_ids", "code": "INVALID_INPUT"},))
    return ReadyNormalization({"image_asset_ids": list(images)}, OUTPUTS)


class TC4Tool:
    def __init__(self, metadata, client):
        self.metadata, self.client = metadata, client

    def validate_input(self, value, *, seed):
        checked = normalize(value)
        if not isinstance(checked, ReadyNormalization):
            raise ValueError("Invalid TC4 input.")
        return ToolExecutionInput({}, OUTPUTS, {"seed": seed}, {"image_asset_ids": list(value["image_asset_ids"])})

    def execute(self, validated_input, request_context):
        if self.client is None:
            raise ToolClientUnavailableError()
        return self.client.execute(self.metadata, validated_input, request_context)

    def health_check(self):
        return self.client.readiness(self.metadata) if self.client else "UNAVAILABLE"


def present(result):
    items = result["data"]["items"]
    count = sum(item["status"] == "SUCCEEDED" for item in items)
    return {"title": "TC4 初生 α 相分割结果", "summary": f"完成 {count}/{len(items)} 张图像。面积占比按整张输入图像计算，包含文件中的边框。"}


def build_tc4_tool(client=None):
    metadata = ToolMetadata(TOOL_ID, "0.1.0", "1.0", "TC4 初生 α 相分割",
        "批量分割用户上传的 TC4 初生 α 相显微图像。一次调用处理全部选定图片（最多10张），逐张返回叠加图、二值掩膜与整图预测面积占比。仅接受8位PNG/JPEG，不裁边。",
        "TC4", True, OUTPUTS, ("image", "ebsd_image"), "LOCAL_RUNTIME", True,
        ({"name": "image_asset_ids", "type": "asset_collection"},),
        tuple({"name": name, "kind": "image" if name != "area_fraction" else "structured_data"} for name in OUTPUTS),
        ("仅用于TC4初生α相。", "面积占比为二维模型预测，不代表准确率或体积分数。"))
    tool = TC4Tool(metadata, client)
    definition = ToolDefinition(tool_id=TOOL_ID, version="1", status=ToolStatus.ACTIVE,
        display_name=metadata.display_name, description=metadata.description,
        input_schema={"type": "object", "additionalProperties": False, "required": list(PROPERTIES), "properties": PROPERTIES},
        proposal_schema={"type": "object", "additionalProperties": False, "properties": PROPERTIES},
        output_schema={"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "minItems": 1, "maxItems": 10}}, "additionalProperties": False},
        runtime_metadata=metadata, supported_outputs=OUTPUTS, supported_asset_types=metadata.supported_asset_types,
        limitations=metadata.limitations, execution_profile=ToolExecutionProfile.MANAGED, execution_mode=ExecutionMode.SYNC,
        executor_id="managed_runtime", tool_execution_policy=ToolExecutionPolicy(), presentation_mode=PresentationMode.CUSTOM,
        presenter_id="tc4_batch_result", context_projection_version="tc4-batch-v1",
        resource_parameters=(ResourceParameterSpec("image_references", "image_asset_ids", ResourceType.IMAGE, ResourceProvider.ASSET, collection=True),))
    return RegisteredTool(definition, ToolExecutionBinding(execution_target=tool, normalizer=normalize,
        context_projector=lambda value: {key: value[key] for key in ("status", "data", "warnings", "error") if key in value},
        presenter=present, health_probe=tool.health_check))
