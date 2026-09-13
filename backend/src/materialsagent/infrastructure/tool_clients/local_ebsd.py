"""Binary transport to the EBSD endpoint in the existing model Runtime."""
import json

import urllib3

from materialsagent.application.context import ActorContext
from materialsagent.application.ebsd_assets import require_asset
from materialsagent.application.errors import ApplicationError
from materialsagent.domain.models.managed_contracts import validate_performance
from materialsagent.domain.ports.tool_execution import (
    ToolExecutionOutput, ToolClientProtocolError, ToolClientRuntimeError,
    ToolClientTimeoutError, ToolClientUnavailableError,
)
from .local_zta35g import LocalZTA35GToolClientAdapter, _runtime_error


class LocalEBSDToolClientAdapter(LocalZTA35GToolClientAdapter):
    def __init__(self, *, asset_service=None, **kwargs):
        super().__init__(**kwargs)
        self.asset_service = asset_service

    def readiness(self, metadata):
        try:
            result = self._request_json("GET", "/internal/v1/ebsd/health/ready", None)
            if result.get("status") != "READY" or result.get("tool_id") != metadata.tool_id or result.get("schema_version") != "1.0":
                return "UNAVAILABLE"
            return "DEGRADED" if result.get("busy") else "AVAILABLE"
        except Exception:
            return "UNAVAILABLE"

    def execute(self, metadata, validated_input, request_context):
        if self.asset_service is None:
            raise ToolClientUnavailableError()
        actor = ActorContext(request_context.actor_id, request_context.user_id)
        try:
            asset = require_asset(self.asset_service, actor, request_context.conversation_id,
                validated_input.input_assets["ebsd_asset_id"])
            image = self.asset_service.get_content(actor, asset.asset_id)
        except ApplicationError:
            raise ToolClientRuntimeError(code="EBSD_INPUT_UNAVAILABLE", safe_message="EBSD 输入图片不可用，请重新上传。", retryable=False) from None
        request = {"request_id": request_context.request_id, "task_id": request_context.task_id,
            "tool_run_id": request_context.tool_run_id, "asset_id": asset.asset_id,
            "sha256": asset.sha256, "seed": validated_input.runtime_parameters["seed"],
            "tool_id": metadata.tool_id, "schema_version": metadata.schema_version}
        try:
            response = self._pool.request("POST", self._base_url + "/internal/v1/ebsd/execute",
                body=image.payload, headers={"Content-Type": "application/octet-stream",
                    "X-ZTA35G-Runtime-Token": self._token, "X-EBSD-Request": json.dumps(request)},
                timeout=self._request_timeout(), retries=False, preload_content=False)
        except (ConnectionRefusedError, urllib3.exceptions.NewConnectionError):
            # Connection creation failed before an HTTP request could be sent.
            raise ToolClientRuntimeError(code="RUNTIME_NOT_READY", safe_message="EBSD Runtime is unavailable.", retryable=True) from None
        except (TimeoutError, urllib3.exceptions.TimeoutError):
            raise ToolClientTimeoutError() from None
        except (OSError, urllib3.exceptions.HTTPError):
            raise ToolClientUnavailableError() from None
        try:
            body = response.read(65537)
            if len(body) > 65536:
                raise ToolClientProtocolError()
            result = json.loads(body)
            if response.status != 200:
                raise _runtime_error(result)
            return parse_result(result, request)
        except (ValueError, TypeError, KeyError):
            raise ToolClientProtocolError() from None
        finally:
            response.close()
            response.release_conn()


def parse_result(result, request):
    expected = {"request_id", "task_id", "tool_run_id", "tool_id", "schema_version", "asset_id", "sha256",
        "status", "requested_outputs", "completed_outputs", "failed_outputs", "data", "images", "warnings",
        "diagnostics", "error", "model_bundle_id", "actual_runtime_parameters"}
    if not isinstance(result, dict) or set(result) != expected:
        raise ToolClientProtocolError()
    if any(result[key] != request[key] for key in ("request_id", "task_id", "tool_run_id", "tool_id", "schema_version", "asset_id", "sha256")):
        raise ToolClientProtocolError()
    if (result["status"] != "SUCCEEDED" or result["requested_outputs"] != ["yield_strength"]
            or result["completed_outputs"] != ["yield_strength"] or result["failed_outputs"] != []
            or result["images"] != [] or result["diagnostics"] != [] or result["error"] is not None
            or result["model_bundle_id"] not in {"inconel625-cnn1-v1", "mock-ebsd-bundle"}):
        raise ToolClientProtocolError()
    validate_performance(request["tool_id"], result["data"], result["completed_outputs"])
    actual = result["actual_runtime_parameters"]
    if (not isinstance(actual, dict) or set(actual) != {"seed", "fp32", "cudnn_tf32", "matmul_tf32"}
            or any(type(v) is not int for v in actual.values()) or actual["seed"] != request["seed"]
            or actual["fp32"] != 1 or actual["cudnn_tf32"] not in (0, 1) or actual["matmul_tf32"] not in (0, 1)):
        raise ToolClientProtocolError()
    warnings = result["warnings"]
    if (not isinstance(warnings, list) or len(warnings) > 5 or any(not isinstance(w, dict)
            or set(w) != {"code", "message"} or any(not isinstance(v, str) or not v.isprintable() or len(v) > 256 for v in w.values()) for w in warnings)):
        raise ToolClientProtocolError()
    return ToolExecutionOutput("SUCCEEDED", ("yield_strength",), ("yield_strength",), (), result["data"], (),
        tuple(warnings), (), actual, result["model_bundle_id"], None)
