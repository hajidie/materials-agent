"""MCP transport adapter. Domain identity belongs to each authenticated call, not a session."""
from dataclasses import dataclass
import json
import logging
import re

import anyio
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings, TransportSecurityMiddleware
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse, Response
from starlette.requests import Request

from materials_ml import EngineError
from .domain import DatasetAsset, TrainingRun, ModelAsset, Prediction, TERMINAL, ServiceError, canonical, digest, text_id
from .schemas import Strict, TrainingRequest, PredictionRequest, DatasetRequest, RunRequest, resource_view
from .prediction_application import CallCancellation
from .request_execution import execute_prediction, signal_prediction_cancel

LIMIT = 65536
CONTRACT = "materials-ml-tools-v2"
IDEMPOTENCY_META = "materials-ml/idempotency-key"
DIGEST_META = "materials-ml/request-digest"


class ToolOutput(Strict):
    contract_version: str
    resource: dict | None = None
    error: dict[str, str] | None = None


@dataclass(frozen=True)
class TransportContext:
    role: str
    scope_id: str
    session_id: str
    request_id: str
    cancellation: CallCancellation
    tool: str


class MCPAdapter:
    def __init__(self, service):
        self.service = service
        self.calls = {}
        self.server = Server("materials-ml", version="1.1.0")
        self.prediction_wait_seconds = 45
        self.contracts = {
            "analyze_tabular_dataset": (DatasetRequest, "Inspect an uploaded numeric tabular dataset and its explicit units."),
            "train_tabular_regression": (TrainingRequest, "Submit LR/RF training; return a durable TrainingRun receipt without waiting for training."),
            "get_training_run": (RunRequest, "Read a TrainingRun state and its published model and evaluation summary."),
            "predict_with_model": (PredictionRequest, "Predict with an existing model and uploaded feature dataset. Request cancellation or its POST disconnect cancels this prediction."),
        }
        self.tools = []
        for name, (schema, description) in self.contracts.items():
            input_schema, output_schema = schema.model_json_schema(), ToolOutput.model_json_schema()
            self.tools.append(types.Tool(name=name, description=description, inputSchema=input_schema,
                outputSchema=output_schema, annotations=types.ToolAnnotations(
                    readOnlyHint=name in ("analyze_tabular_dataset", "get_training_run"),
                    destructiveHint=False, idempotentHint=True, openWorldHint=False),
                _meta={"contract_version": CONTRACT, "schema_sha256": digest({"input": input_schema, "output": output_schema})}))

        @self.server.list_tools()
        async def list_tools():
            return self.tools

        # The public request-handler extension avoids SDK decorators echoing validation values.
        self.server.request_handlers[types.CallToolRequest] = self.call_tool
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*"],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*"])
        self.security = TransportSecurityMiddleware(security)
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True, stateless=False,
            max_request_body_size=LIMIT, max_sessions=32, session_idle_timeout=300, security_settings=security)
        # SDK validation/debug exceptions can contain caller data. Only controlled adapter errors leave this boundary.
        logger = logging.getLogger("mcp")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False

    async def call_tool(self, request):
        name = request.params.name
        if name not in self.contracts:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="UNKNOWN_TOOL"))
        context = self.server.request_context.request.scope["ml_call"]
        try:
            body = self.contracts[name][0].model_validate(request.params.arguments or {})
            if request.params.task is not None:
                raise ServiceError("MCP_TASKS_NOT_SUPPORTED", 422)
            if name in ("train_tabular_regression", "predict_with_model"):
                meta = request.params.meta.model_dump() if request.params.meta else {}
                key = text_id(meta.get(IDEMPOTENCY_META, ""), 255)
                expected_digest = meta.get(DIGEST_META)
                if expected_digest is not None and (type(expected_digest) is not str or
                        re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None):
                    raise ServiceError("INVALID_REQUEST_DIGEST", 422)
            if name == "predict_with_model":
                try:
                    with anyio.fail_after(self.prediction_wait_seconds):
                        prediction = await execute_prediction(self.service, context.scope_id, key, body,
                            context.cancellation, expected_digest=expected_digest)
                        while prediction.status not in TERMINAL:
                            await anyio.sleep(.1)
                            prediction = await anyio.to_thread.run_sync(lambda: self.service.get(
                                Prediction, context.scope_id, prediction.id))
                        resource = resource_view(prediction)
                except TimeoutError:
                    # Replay callers never obtain the original computation's cancellation ownership.
                    await signal_prediction_cancel(self.service, context.cancellation)
                    raise ServiceError("PREDICTION_OUTCOME_UNKNOWN", 503) from None
            else:
                def execute():
                    if name == "analyze_tabular_dataset":
                        result = self.service.get(DatasetAsset, context.scope_id, body.dataset_id)
                        if result.status != "AVAILABLE":
                            raise ServiceError("DATASET_NOT_AVAILABLE")
                    elif name == "train_tabular_regression":
                        result = self.service.submit_training(context.scope_id, key, body.dataset_id,
                            body.model_dump(exclude={"dataset_id"}),
                            **({"expected_digest": expected_digest} if expected_digest is not None else {}))
                    else:
                        result = self.service.get(TrainingRun, context.scope_id, body.training_run_id)
                    public = resource_view(result)
                    if isinstance(result, TrainingRun) and result.model_id:
                        public["metrics"] = self.service.get(ModelAsset, context.scope_id, result.model_id).data["metrics"]
                    return public
                # Cancellation may stop delivery, but cannot detach/revoke a committed TrainingRun.
                resource = await anyio.to_thread.run_sync(execute, abandon_on_cancel=False)
            output = ToolOutput(contract_version=CONTRACT, resource=resource).model_dump()
            error = name == "predict_with_model" and resource["status"] in ("FAILED", "CANCELLED")
            if error:
                output["error"] = {"code": resource.get("error_code") or "PREDICTION_FAILED"}
            if len(canonical(output)) > LIMIT:
                identity = {k: v for k, v in resource.items() if k in
                    {"id", "scope_id", "status", "dataset_id", "model_id", "input_dataset_id"}}
                output = ToolOutput(contract_version=CONTRACT, resource=identity,
                    error={"code": "TOOL_RESULT_TOO_LARGE"}).model_dump()
                error = True
        except ValidationError:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="INVALID_TOOL_ARGUMENTS")) from None
        except (ServiceError, EngineError) as exc:
            output = ToolOutput(contract_version=CONTRACT, error={"code": exc.code}).model_dump()
            error = True
        except SQLAlchemyError:
            output = ToolOutput(contract_version=CONTRACT, error={"code": "DATABASE_UNAVAILABLE"}).model_dump()
            error = True
        except Exception:
            output = ToolOutput(contract_version=CONTRACT, error={"code": "ML_SERVICE_UNAVAILABLE"}).model_dump()
            error = True
        return types.ServerResult(types.CallToolResult(isError=error, structuredContent=output,
            content=[types.TextContent(type="text", text=canonical(output).decode("utf-8"))]))

    async def __call__(self, scope, receive, send):
        denied = await self.security.validate_request(Request(scope, receive), scope["method"] == "POST")
        if denied:
            return await denied(scope, receive, send)
        if scope["method"] != "POST":
            # No unrelated GET streams and no session-wide cancellation endpoint.
            return await Response(status_code=405)(scope, receive, send)
        headers = dict(scope["headers"])
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > LIMIT:
                return await JSONResponse({"error": {"code": "REQUEST_TOO_LARGE"}}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        try:
            raw = json.loads(body)
            types.JSONRPCMessage.model_validate(raw)
        except (ValueError, TypeError):
            return await JSONResponse({"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "INVALID_REQUEST"}}, 400)(scope, receive, send)
        method = raw.get("method")
        if method not in {"initialize", "ping", "tools/list", "tools/call", "notifications/initialized", "notifications/cancelled"}:
            return await JSONResponse({"jsonrpc": "2.0", "id": raw.get("id"),
                "error": {"code": -32601, "message": "METHOD_NOT_FOUND"}})(scope, receive, send)
        try:
            if "id" in raw:
                types.ClientRequest.model_validate(raw)
            else:
                types.ClientNotification.model_validate(raw)
        except ValueError:
            return await JSONResponse({"jsonrpc": "2.0", "id": raw.get("id"),
                "error": {"code": -32602, "message": "INVALID_PARAMS"}})(scope, receive, send)
        context = None
        if method in ("tools/call", "notifications/cancelled"):
            try:
                domain_scope = text_id(headers.get(b"x-ml-scope-id", b"").decode("utf-8"))
                session = headers.get(b"mcp-session-id", b"").decode("ascii")
            except (ServiceError, UnicodeError):
                return await JSONResponse({"error": {"code": "INVALID_TRANSPORT_CONTEXT"}}, 400)(scope, receive, send)
            if method == "tools/call":
                identity = (session, str(raw["id"]))
                if identity in self.calls:
                    return await JSONResponse({"error": {"code": "DUPLICATE_REQUEST_ID"}}, 409)(scope, receive, send)
                context = TransportContext("mcp_client", domain_scope, session, str(raw["id"]),
                                           CallCancellation(), raw["params"]["name"])
                self.calls[identity] = context
                scope = {**scope, "ml_call": context}
            else:
                target = self.calls.get((session, str(raw["params"]["requestId"])))
                if target and target.scope_id != domain_scope:
                    return await JSONResponse({"error": {"code": "CANCEL_SCOPE_MISMATCH"}}, 403)(scope, receive, send)
                if target and target.tool == "predict_with_model":
                    await signal_prediction_cancel(self.service, target.cancellation)
        delivered = False
        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # This adapter alone consumes the underlying receive after the complete body.
            await anyio.sleep_forever()
        try:
            async with anyio.create_task_group() as group:
                async def watch_disconnect():
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            if context and context.tool == "predict_with_model":
                                await signal_prediction_cancel(self.service, context.cancellation)
                            return
                if context:
                    group.start_soon(watch_disconnect)
                try:
                    await self.manager.handle_request(scope, replay_receive, send)
                finally:
                    group.cancel_scope.cancel()
        finally:
            if context:
                # The handler performs shielded cleanup. In-flight tracking is per call, never per scope/session globally.
                self.calls.pop((context.session_id, context.request_id), None)
