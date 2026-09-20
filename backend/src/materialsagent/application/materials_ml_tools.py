"""Local approval manifest. No ML package or server SDK is imported here."""
from hashlib import sha256
from importlib.resources import files
import json

from jsonschema import Draft202012Validator

from materialsagent.domain.ports.mcp import MCPBinding
from materialsagent.domain.ports.tool_execution import ToolMetadata
from materialsagent.domain.ports.tool_registry import (RegisteredTool, ToolDefinition, ToolExecutionBinding,
    ToolExecutionProfile, ToolExecutionPolicy, ToolLifecyclePolicy, ToolStatus, ExecutionMode, PresentationMode)
from materialsagent.domain.ports.tool_registry import ResourceParameterSpec, ResourceProvider, ResourceType
from materialsagent.domain.ports.tool_authorization import ToolAuthorizationDecision, EmptyPermissionAuthorizationService

CONTRACTS = json.loads(files("materialsagent.application").joinpath("materials_ml_contracts.json").read_text(encoding="utf-8"))
WRITES = {"train_tabular_regression": "training.submit", "predict_with_model": "prediction.submit"}
TITLES = {"analyze_tabular_dataset": "分析材料表格数据", "get_training_run": "查询材料回归训练",
          "train_tabular_regression": "提交材料回归训练", "predict_with_model": "使用材料模型预测"}
RESOURCE_PARAMETERS = {
    "analyze_tabular_dataset": (
        ResourceParameterSpec("dataset_reference", "dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE),
    ),
    "train_tabular_regression": (
        ResourceParameterSpec("dataset_reference", "dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE),
    ),
    "get_training_run": (
        ResourceParameterSpec("training_reference", "training_run_id", ResourceType.TRAINING_RUN, ResourceProvider.ML_RESOURCE),
    ),
    "predict_with_model": (
        ResourceParameterSpec("model_reference", "model_id", ResourceType.MODEL, ResourceProvider.ML_RESOURCE),
        ResourceParameterSpec("input_dataset_reference", "input_dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE),
    ),
}


def plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validator_for(schema):
    validator = Draft202012Validator(schema)
    def validate(arguments):
        arguments = plain(arguments)
        try:
            validator.validate(arguments)
            if len(canonical(arguments)) > 16384:
                raise ValueError()
        except Exception:
            raise ValueError("Invalid ML Tool arguments.") from None
        return arguments
    return validate


def present(result):
    resource = result.get("resource") or {}
    return {"title": result["title"], "summary": result["summary"],
            "resource_id": resource.get("id"), "status": resource.get("status")}


def build_ml_tools(*, binding_version, endpoint_digest, health_probe=None):
    registrations = []
    for name, contract in CONTRACTS.items():
        tool_id = "materials_ml_" + name
        write = name in WRITES
        description = TITLES[name] + ("；仅提交训练，不等待模型完成。" if name == "train_tabular_regression" else
            "；同步返回预测终态。" if name == "predict_with_model" else "；仅查询已上传的同对话资源。")
        if name == "analyze_tabular_dataset":
            description += "提供表格概况、列类型、缺失值、重复行和单位诊断。一般数据分析可直接使用，无需目标列、算法或额外分析选项；不会训练模型。"
        metadata = ToolMetadata(tool_id=tool_id, tool_version="1", schema_version="1", display_name=TITLES[name],
            description=description, material_scope="GENERAL_MATERIALS", enabled=True,
            supported_outputs=("ml_receipt",), supported_asset_types=(), execution_mode="MCP_SYNC",
            requires_gpu=False, input_fields=(), output_summary=(), limitations=("需显式提供同 scope 的资源 ID。",))
        definition = ToolDefinition(tool_id=tool_id, version="1", status=ToolStatus.ACTIVE,
            display_name=TITLES[name], description=description, input_schema=contract["inputSchema"],
            output_schema={"type": "object", "required": ["resource", "title", "summary"],
                "properties": {"resource": {"type": "object"}, "title": {"type": "string"}, "summary": {"type": "string"}},
                "additionalProperties": False}, runtime_metadata=metadata, supported_outputs=("ml_receipt",),
            supported_asset_types=(), limitations=metadata.limitations,
            execution_profile=ToolExecutionProfile.SIDE_EFFECT if write else ToolExecutionProfile.STANDARD,
            execution_mode=ExecutionMode.SYNC, executor_id="mcp", presenter_id="materials_ml_v1",
            presentation_mode=PresentationMode.DETERMINISTIC,
            resource_parameters=RESOURCE_PARAMETERS[name],
            tool_execution_policy=ToolExecutionPolicy(lifecycle_policy=ToolLifecyclePolicy.ANY_TASK,
                required_permissions=("materials_ml." + name,) if write else (), confirmation_required=write),
            confirmation_prompt=("确认" + TITLES[name] + "？此操作会创建持久化 ML 资源。") if write else None)
        binding = MCPBinding(server_id="materials_ml", binding_version=binding_version, endpoint_digest=endpoint_digest,
            remote_tool_name=name, remote_schema_hash=sha256(canonical(
                {"input": contract["inputSchema"], "output": contract["outputSchema"]})).hexdigest())
        registrations.append(RegisteredTool(definition, ToolExecutionBinding(execution_target=binding,
            validator=validator_for(contract["inputSchema"]), codec=lambda value: value, presenter=present,
            health_probe=(None if health_probe is None else
                lambda binding=binding: health_probe(binding)))))
    return tuple(registrations)


class LocalMLAuthorization:
    """Exact identity/version/schema/permission grant, never a general permission wildcard."""
    def __init__(self, approved, fallback=None):
        self.allowed = {r.ref: r.definition.tool_execution_policy.required_permissions for r in approved
                        if r.execution_profile is ToolExecutionProfile.SIDE_EFFECT}
        self.fallback = fallback or EmptyPermissionAuthorizationService()

    def authorize(self, request):
        if request.tool_ref in self.allowed and request.required_permissions == self.allowed[request.tool_ref]:
            return ToolAuthorizationDecision(True, "LOCAL_ML_EXACT_IDENTITY_GRANT")
        return self.fallback.authorize(request)
