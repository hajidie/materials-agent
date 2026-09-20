"""MCP is an executor, not a new governance profile or Agent decision path."""
from materialsagent.application.materials_ml_tools import TITLES, canonical, plain
from materialsagent.application.tool_invocations import ToolExecutionResult
from materialsagent.domain.ports.mcp import MCPBinding, MCPFailure, identity_receipt
from materialsagent.domain.ports.tool_registry import ToolExecutionProfile


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
        output = self.client.call(binding, registration.binding.validator(arguments), context)
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
