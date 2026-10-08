"""MCP is an executor, not a new governance profile or Agent decision path."""
from materialsagent.application.materials_ml_tools import TITLES, canonical, plain
from materialsagent.application.tool_invocations import ToolExecutionResult
from materialsagent.domain.ports.mcp import MCPBinding, MCPFailure, identity_receipt
from materialsagent.domain.ports.tool_registry import ToolExecutionProfile
from langchain_core.runnables import RunnableLambda


class SafeReadTransientError(Exception):
    pass


def result_from_receipt(registration, run, receipt):
    """Only the pinned service's full resource view is a tool result."""
    if receipt.get("lookup_status") != "FOUND":
        return None
    resource = receipt.get("resource")
    if not isinstance(resource, dict) or resource.get("scope_id") != run.conversation_id:
        return None
    name = registration.binding.execution_target.remote_tool_name
    required = ({"dataset_id", "spec", "units", "warnings", "cancel_requested", "recovery_required"}
                if name == "train_tabular_regression" else
                {"model_id", "input_dataset_id", "row_count", "target", "target_unit", "feature_order",
                 "model_units", "input_units", "unit_verification", "input_identity", "model_manifest_sha256", "result_ref", "warnings"})
    if not required.issubset(resource) or not {"id", "version", "created_at", "updated_at", "status"}.issubset(resource):
        return None
    arguments = plain(run.proposed_arguments)
    if name == "train_tabular_regression":
        spec = resource["spec"]
        if (resource["dataset_id"] != arguments.get("dataset_id") or not isinstance(spec, dict)
                or any(spec.get(k) != arguments.get(k) for k in ("features", "target"))):
            return None
    elif (resource["status"] != "SUCCEEDED" or not resource["result_ref"] or
          any(resource.get(k) != arguments.get(k) for k in ("model_id", "input_dataset_id"))):
        return None
    from materialsagent.infrastructure.tool_clients.mcp_client import MCPClient, Call
    from mcp import types
    import json
    structured = {"contract_version": registration.binding.execution_target.contract_version,
                  "resource": resource, "error": None}
    try:
        MCPClient.validate_result(Call(registration.binding.execution_target, arguments, run.conversation_id,
            plain(run.remote_operation), 1), types.CallToolResult(content=[types.TextContent(type="text",
                text=json.dumps(structured))], structuredContent=structured))
    except MCPFailure:
        return None
    summary = "训练已提交；是否完成以训练状态查询为准。" if name == "train_tabular_regression" else "预测已完成。"
    data = {"resource": resource, "title": TITLES[name], "summary": summary}
    if len(canonical(data)) > 16384:
        return None
    return ToolExecutionResult(data, registration.binding.presenter(data))


class MCPExecutor:
    executor_id = "mcp"
    supported_profiles = frozenset({ToolExecutionProfile.STANDARD, ToolExecutionProfile.SIDE_EFFECT})

    def __init__(self, client):
        self.client = client

    def binding(self, registration, context):
        binding = registration.binding.execution_target
        if not isinstance(binding, MCPBinding) or binding.snapshot(registration) != context.binding_snapshot:
            raise MCPFailure("MCP_BINDING_MISMATCH")
        return binding

    def prepare(self, registration, arguments, context):
        arguments = registration.binding.validator(arguments)
        return self.client.prepare(self.binding(registration, context), arguments, context)

    def execute(self, registration, arguments, context):
        binding = self.binding(registration, context)
        if registration.execution_profile is ToolExecutionProfile.SIDE_EFFECT and not context.remote_operation:
            raise MCPFailure("MCP_IDENTITY_UNAVAILABLE")
        arguments = registration.binding.validator(arguments)
        if context.auto_retry_safe and registration.definition.auto_retry_safe:
            def read(_):
                try:
                    return self.client.call(binding, arguments, context)
                except MCPFailure as error:
                    if error.transient:
                        raise SafeReadTransientError() from None
                    raise
            try:
                output = RunnableLambda(read).with_retry(retry_if_exception_type=(SafeReadTransientError,),
                    stop_after_attempt=3, exponential_jitter_params={"initial": 0.5, "max": 1, "jitter": 0.1}).invoke(None)
            except SafeReadTransientError:
                raise MCPFailure("MCP_READ_TRANSIENT_FAILED") from None
        else:
            output = self.client.call(binding, arguments, context)
        resource = plain(output["resource"])
        name = binding.remote_tool_name
        summary = ("训练已提交；是否完成以训练状态查询为准。" if name == "train_tabular_regression" else
                   "预测已完成。" if name == "predict_with_model" else "查询完成；资源状态：" + resource["status"])
        data = {"resource": resource, "title": TITLES[name], "summary": summary}
        if len(canonical(data)) > 16384:
            raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True, receipt=identity_receipt(resource))
        return ToolExecutionResult(data, registration.binding.presenter(data))

    def lookup(self, registration, scope_id, operation):
        return self.client.lookup(registration.binding.execution_target, scope_id, operation)
