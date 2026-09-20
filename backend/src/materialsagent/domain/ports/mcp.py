"""Transport-neutral, explicitly approved MCP binding and controlled failures."""
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MCPBinding:
    server_id: str
    binding_version: str
    endpoint_digest: str
    remote_tool_name: str
    remote_schema_hash: str
    server_name: str = "materials-ml"
    server_version: str = "1.1.0"
    protocol_version: str = "2025-11-25"
    contract_version: str = "materials-ml-tools-v2"

    def snapshot(self, registration):
        from dataclasses import asdict
        return {**asdict(self), "executor_id": "mcp", "tool_id": registration.tool_id,
                "tool_version": registration.version, "schema_hash": registration.schema_hash}


class MCPFailure(Exception):
    def __init__(self, code: str, *, unknown: bool = False, receipt: Mapping | None = None):
        super().__init__(code)
        self.code, self.unknown, self.receipt = code, unknown, receipt


class MCPClientPort(Protocol):
    def prepare(self, binding, arguments, context) -> Mapping | None: ...
    def call(self, binding, arguments, context) -> Mapping: ...
    def lookup(self, binding, scope_id: str, operation: Mapping) -> Mapping: ...


def identity_receipt(resource):
    """Confirmed identity only; never a usable result for Agent orchestration."""
    return {"lookup_status": "CONFIRMED_IDENTITY", "resource": {k: v for k, v in resource.items()
        if k in {"id", "scope_id", "status", "dataset_id", "model_id", "input_dataset_id"}}}
