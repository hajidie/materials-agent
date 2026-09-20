"""Acceptance-only official SDK pool probe, executed in the Backend environment."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from materialsagent.application.materials_ml_tools import build_ml_tools
from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.infrastructure.tool_clients.mcp_client import MCPClient


def main():
    cfg = json.loads(sys.stdin.readline())
    client = MCPClient(url=cfg["url"], token=cfg["token"], resource_token=cfg["resource_token"])
    try:
        registry = ToolRegistry(build_ml_tools(binding_version="1", endpoint_digest=client.endpoint_digest))
        binding = registry.resolve("materials_ml_analyze_tabular_dataset").binding.execution_target
        def invoke(index):
            scope, identity = cfg["datasets"][index % len(cfg["datasets"])]
            ctx = SimpleNamespace(conversation_id=scope, invocation_run_id="probe-" + str(index), remote_operation=None)
            result = client.call(binding, {"dataset_id": identity}, ctx)
            assert result["resource"]["scope_id"] == scope and result["resource"]["id"] == identity
        for start in range(0, 40, 4):
            with ThreadPoolExecutor(4) as executor:
                list(executor.map(invoke, range(start, start + 4)))
        assert client.created_sessions <= 4
        print(json.dumps({"calls": 40, "sessions": client.created_sessions}))
    finally:
        client.close()


if __name__ == "__main__":
    main()
