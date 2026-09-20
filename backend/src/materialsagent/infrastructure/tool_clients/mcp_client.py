"""Official SDK sessions, exclusively leased per invocation; no SDK internals."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import json
import logging
from time import monotonic
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import anyio
from anyio.from_thread import start_blocking_portal
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from materialsagent.application.execution_deadline import remaining_timeout
from materialsagent.application.materials_ml_tools import CONTRACTS, WRITES, canonical, plain
from materialsagent.domain.ports.mcp import MCPFailure, identity_receipt

WIRE_LIMIT = 256 * 1024
COMMON_FIELDS = {"id", "scope_id", "status", "version", "created_at", "updated_at"}
RESOURCE_FIELDS = {
    "analyze_tabular_dataset": COMMON_FIELDS | {"identity", "raw_sha256", "analysis", "units", "display_name", "parser_contract"},
    "get_training_run": COMMON_FIELDS | {"dataset_id", "model_id", "cancel_requested", "recovery_required", "error_code", "units", "warnings", "spec", "metrics"},
    "predict_with_model": COMMON_FIELDS | {"model_id", "input_dataset_id", "cancel_requested", "error_code", "row_count", "target", "target_unit", "feature_order", "model_units", "input_units", "unit_verification", "input_identity", "model_manifest_sha256", "warnings", "result_ref"},
}
RESOURCE_FIELDS["train_tabular_regression"] = RESOURCE_FIELDS["get_training_run"]


class BoundedTransport(httpx.AsyncBaseTransport):
    """Bound wire bytes before the SDK parses JSON; refuse SDK-followed redirects."""
    def __init__(self, allowed_url):
        self.allowed_url = allowed_url
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if str(request.url) != self.allowed_url:
            raise MCPFailure("MCP_ENDPOINT_REJECTED")
        response = await self.inner.handle_async_request(request)
        try:
            if 300 <= response.status_code < 400:
                # Even a same-URL 307 would resend the original POST. Never let SDK redirect policy replay it.
                raise MCPFailure("MCP_REDIRECT_REJECTED", unknown=True)
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > WIRE_LIMIT:
                    raise MCPFailure("MCP_RESPONSE_TOO_LARGE", unknown=True)
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(payload),
                                  request=request)
        finally:
            await response.aclose()

    async def aclose(self):
        await self.inner.aclose()


@dataclass(frozen=True)
class Call:
    binding: object
    arguments: dict
    scope: str
    operation: dict | None
    timeout: float
    check_only: bool = False


class SessionSlot:
    def __init__(self, pool):
        self.pool = pool
        self.commands = asyncio.Queue(maxsize=1)
        self.task = asyncio.create_task(self.serve())
        self.context = None
        self.sent = False
        self.request_id = None
        self.cancel_ack = asyncio.Event()
        self.session_expired = False

    async def request_hook(self, request):
        # Public httpx request API; each slot has exactly one immutable lease owner.
        if request.method != "POST":
            return
        body = json.loads(await request.aread())
        method = body.get("method")
        if method not in ("tools/call", "notifications/cancelled"):
            return
        if self.context is None:
            raise MCPFailure("MCP_SCOPE_UNAVAILABLE")
        if method == "notifications/cancelled" and body["params"]["requestId"] != self.request_id:
            raise MCPFailure("MCP_CANCEL_TARGET_MISMATCH")
        request.headers["X-ML-Scope-ID"] = self.context.scope
        if method == "tools/call":
            if len(await request.aread()) > 65536:
                raise MCPFailure("MCP_REQUEST_TOO_LARGE")
            self.sent = True
            self.request_id = body["id"]

    async def response_hook(self, response):
        if response.request.method == "POST":
            body = json.loads(await response.request.aread())
            if body.get("method") == "tools/list" and response.status_code == 404:
                # Only a read-only preflight can authorize session renewal. A tool POST
                # (including one returning 404) must never be replayed here.
                self.session_expired = True
            if body.get("method") == "notifications/cancelled" and response.status_code == 202:
                self.cancel_ack.set()

    async def cancel(self, session):
        if not self.sent or self.request_id is None:
            return
        # Best effort protocol notification, then mandatory slot/POST closure by the caller.
        with anyio.move_on_after(2, shield=True):
            await session.send_notification(types.ClientNotification(types.CancelledNotification(
                params=types.CancelledNotificationParams(requestId=self.request_id, reason="Invocation stopped"))))
            await self.cancel_ack.wait()

    async def validate(self, session, initialized, binding):
        if (initialized.protocolVersion != binding.protocol_version or
                initialized.serverInfo.name != binding.server_name or
                initialized.serverInfo.version != binding.server_version):
            raise MCPFailure("MCP_BINDING_MISMATCH")
        catalog = await session.list_tools()
        matches = [t for t in catalog.tools if t.name == binding.remote_tool_name]
        if len(matches) != 1:
            raise MCPFailure("MCP_BINDING_MISMATCH")
        tool = matches[0]
        actual = sha256(canonical({"input": tool.inputSchema, "output": tool.outputSchema})).hexdigest()
        if ((tool.meta or {}).get("contract_version") != binding.contract_version or
                (tool.meta or {}).get("schema_sha256") != actual or actual != binding.remote_schema_hash):
            raise MCPFailure("MCP_BINDING_MISMATCH")

    async def serve(self):
        pending = None
        try:
            while True:
                if pending is None:
                    pending = await self.commands.get()
                    deadline = monotonic() + pending[0].timeout
                    renewed = False
                self.context = pending[0]
                self.sent = False
                self.session_expired = False
                pending_failure = None
                try:
                    headers = {"Authorization": "Bearer " + self.pool.token}
                    async with httpx.AsyncClient(headers=headers, timeout=60, trust_env=False,
                            transport=BoundedTransport(self.pool.url),
                            event_hooks={"request": [self.request_hook], "response": [self.response_hook]}) as http:
                        async with streamable_http_client(self.pool.url, http_client=http, terminate_on_close=False) as (read, write, _):
                            async with ClientSession(read, write) as session:
                                with anyio.fail_after(min(10, max(0, deadline - monotonic()))):
                                    initialized = await session.initialize()
                                self.pool.created_sessions += 1
                                while True:
                                    call, future = pending
                                    self.context, self.sent, self.request_id = call, False, None
                                    self.session_expired = False
                                    self.cancel_ack.clear()
                                    try:
                                        with anyio.fail_after(max(0, deadline - monotonic())):
                                            await self.validate(session, initialized, call.binding)
                                            if call.check_only:
                                                result = None
                                            else:
                                                meta = None
                                                if call.operation:
                                                    meta = {"materials-ml/idempotency-key": call.operation["idempotency_key"],
                                                        "materials-ml/request-digest": call.operation["request_digest"]}
                                                result = await session.call_tool(call.binding.remote_tool_name,
                                                    call.arguments, meta=meta,
                                                    read_timeout_seconds=timedelta(seconds=call.timeout))
                                                result = self.pool.validate_result(call, result)
                                        future.set_result(result)
                                    except BaseException as error:
                                        pending_failure = error if isinstance(error, MCPFailure) else MCPFailure(
                                            "MCP_OUTCOME_UNKNOWN" if self.sent else "MCP_UNAVAILABLE", unknown=self.sent)
                                        await self.cancel(session)
                                        raise
                                    self.context = None
                                    pending = None
                                    pending = await self.commands.get()
                                    deadline = monotonic() + pending[0].timeout
                                    renewed = False
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    if (pending and self.session_expired and not self.sent and not renewed
                            and monotonic() < deadline):
                        # The old SDK/HTTP contexts have exited before opening a fresh
                        # session. Keep the same lease, scope and remaining time budget.
                        renewed = True
                        continue
                    if pending and not pending[1].done():
                        # Transport/protocol ambiguity after dispatch is never a safe retry.
                        failure = pending_failure or (error if isinstance(error, MCPFailure) else MCPFailure(
                            "MCP_OUTCOME_UNKNOWN" if self.sent else "MCP_UNAVAILABLE", unknown=self.sent))
                        pending[1].set_exception(failure)
                    pending, self.context = None, None
        finally:
            if pending and not pending[1].done():
                pending[1].set_exception(MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True))


class MCPClient:
    def __init__(self, *, url, token, resource_token, slots=4):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("MCP endpoint must be an explicitly configured local /mcp endpoint.")
        if not token or not resource_token or token == resource_token:
            raise ValueError("Separate MCP and Resource credentials are required.")
        if not 1 <= slots <= 4:
            raise ValueError("MCP pool must contain 1 to 4 slots.")
        self.url, self.token, self.resource_token = url, token, resource_token
        self.base = url[:-4]
        self.endpoint_digest = sha256(url.encode()).hexdigest()
        self.created_sessions = 0
        self._readiness_cache = {}
        # SDK debug/validation exceptions can include untrusted arguments. Never propagate them to logs.
        logger = logging.getLogger("mcp")
        logger.addHandler(logging.NullHandler()); logger.propagate = False
        self.portal_context = start_blocking_portal()
        self.portal = self.portal_context.__enter__()
        self.portal.call(self.start, slots)

    async def start(self, count):
        self.available = asyncio.Queue(maxsize=count)
        self.slots = [SessionSlot(self) for _ in range(count)]
        for slot in self.slots:
            self.available.put_nowait(slot)

    async def request(self, call):
        try:
            slot = self.available.get_nowait()
        except asyncio.QueueEmpty:
            raise MCPFailure("MCP_CLIENT_BUSY") from None
        try:
            future = asyncio.get_running_loop().create_future()
            await slot.commands.put((call, future))
            return await future
        finally:
            self.available.put_nowait(slot)

    def invoke(self, binding, arguments, context, *, check_only=False):
        try:
            timeout = remaining_timeout(60)
        except TimeoutError:
            raise MCPFailure("MCP_DEADLINE_EXCEEDED") from None
        return self.portal.call(self.request, Call(binding, plain(arguments), context.conversation_id,
            plain(context.remote_operation), timeout, check_only))

    def prepare(self, binding, arguments, context):
        self.invoke(binding, arguments, context, check_only=True)
        operation = WRITES.get(binding.remote_tool_name)
        if operation is None:
            return None
        response = self.resource_request(context.conversation_id, "operation-identities/prepare",
                                         {"operation": operation, "arguments": plain(arguments)})
        expected = {"training.submit": "training-submit-v1", "prediction.submit": "prediction-request-v1"}[operation]
        import re
        if (set(response) != {"scope_id", "operation", "request_digest", "digest_version"} or
                response["scope_id"] != context.conversation_id or response["operation"] != operation or
                response["digest_version"] != expected or
                re.fullmatch(r"[0-9a-f]{64}", str(response["request_digest"])) is None):
            raise MCPFailure("MCP_IDENTITY_INVALID")
        return {k: v for k, v in response.items() if k != "scope_id"} | {
            "idempotency_key": "ml-invocation:" + context.invocation_run_id}

    def readiness(self, binding):
        cache_key = (binding.remote_tool_name, binding.remote_schema_hash)
        now = monotonic()
        cached = self._readiness_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        context = SimpleNamespace(
            conversation_id="mcp-readiness",
            invocation_run_id="mcp-readiness",
            remote_operation=None,
        )
        try:
            self.invoke(binding, {}, context, check_only=True)
            status = "AVAILABLE"
        except Exception:
            status = "UNAVAILABLE"
        self._readiness_cache[cache_key] = (now + 5, status)
        return status

    def call(self, binding, arguments, context):
        return self.invoke(binding, arguments, context)

    def resource_request(self, scope, path, body):
        try:
            with httpx.Client(timeout=remaining_timeout(10), trust_env=False, follow_redirects=False) as client:
                with client.stream("POST", self.base + "/api/v1/scopes/" + quote(scope, safe="") + "/" + path,
                        headers={"Authorization": "Bearer " + self.resource_token}, json=body) as response:
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 65536:
                            raise MCPFailure("MCP_RECEIPT_INVALID")
                    if response.status_code == 409:
                        raise MCPFailure("MCP_RECEIPT_CONFLICT")
                    if response.status_code == 422 and path == "operation-identities/prepare":
                        # This endpoint cannot dispatch work. Preserve only pinned
                        # domain codes; never propagate arbitrary remote messages.
                        error = json.loads(payload).get("error", {})
                        code = error.get("code") if isinstance(error, dict) else None
                        if code in {"UNIT_CONFLICT", "INVALID_UNITS", "MISSING_COLUMNS", "DATASET_NOT_AVAILABLE",
                                    "UNSUPPORTED_ALGORITHM"}:
                            raise MCPFailure("ML_" + code)
                    if response.status_code != 200:
                        raise MCPFailure("MCP_RESOURCE_UNAVAILABLE")
                    return json.loads(payload)
        except MCPFailure:
            raise
        except Exception:
            raise MCPFailure("MCP_RESOURCE_UNAVAILABLE") from None

    def lookup(self, binding, scope_id, operation):
        if binding.endpoint_digest != self.endpoint_digest:
            raise MCPFailure("MCP_BINDING_MISMATCH")
        result = self.resource_request(scope_id, "operation-receipts/lookup", plain(operation))
        if (not isinstance(result, dict) or result.get("scope_id") != scope_id or
                any(result.get(k) != operation[k] for k in ("operation", "digest_version", "request_digest")) or
                result.get("lookup_status") not in {"FOUND", "NOT_FOUND"}):
            raise MCPFailure("MCP_RECEIPT_INVALID")
        resource = result.get("resource")
        if result["lookup_status"] == "FOUND":
            name = {"training.submit": "train_tabular_regression", "prediction.submit": "predict_with_model"}[operation["operation"]]
            if (not isinstance(resource, dict) or resource.get("scope_id") != scope_id
                    or not isinstance(resource.get("id"), str) or not resource["id"]
                    or resource.get("status") not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}
                    or set(resource) - RESOURCE_FIELDS[name]):
                raise MCPFailure("MCP_RECEIPT_INVALID")
        if result["lookup_status"] == "NOT_FOUND" and resource is not None:
            raise MCPFailure("MCP_RECEIPT_INVALID")
        if len(canonical(result)) > 15872:  # Reserve bounded local checked_at / last_check metadata.
            raise MCPFailure("MCP_RECEIPT_TOO_LARGE")
        return result

    @staticmethod
    def validate_result(call, result):
        from jsonschema import Draft202012Validator
        structured = result.structuredContent
        try:
            Draft202012Validator(CONTRACTS[call.binding.remote_tool_name]["outputSchema"]).validate(structured)
            if len(canonical(structured)) > 65536 or structured["contract_version"] != call.binding.contract_version:
                raise ValueError()
            if len(result.content) != 1 or result.content[0].type != "text" or json.loads(result.content[0].text) != structured:
                raise ValueError()
            resource = structured.get("resource")
            if resource is not None:
                if (resource.get("scope_id") != call.scope or not isinstance(resource.get("id"), str)
                        or not 1 <= len(resource["id"]) <= 255):
                    raise ValueError()
                name = call.binding.remote_tool_name
                if set(resource) - RESOURCE_FIELDS[name]:
                    raise ValueError()
                states = {"AVAILABLE"} if name == "analyze_tabular_dataset" else {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}
                if resource.get("status") not in states:
                    raise ValueError()
                identities = {"analyze_tabular_dataset": {"id": "dataset_id"}, "get_training_run": {"id": "training_run_id"},
                    "train_tabular_regression": {"dataset_id": "dataset_id"},
                    "predict_with_model": {"model_id": "model_id", "input_dataset_id": "input_dataset_id"}}[name]
                if any(resource.get(k) != call.arguments[v] for k, v in identities.items()):
                    raise ValueError()
        except Exception:
            raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True) from None
        if result.isError:
            code = (structured.get("error") or {}).get("code")
            if code == "TOOL_RESULT_TOO_LARGE" and resource:
                raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True, receipt=identity_receipt(resource))
            # These are pre-write, deterministic rejections in the pinned service contract.
            rejected = {"REQUEST_DIGEST_MISMATCH", "INVALID_REQUEST_DIGEST", "IDEMPOTENCY_CONFLICT", "RESOURCE_NOT_FOUND",
                "MISSING_COLUMNS", "UNIT_CONFLICT", "INVALID_UNITS", "DATASET_NOT_AVAILABLE", "PREDICTION_RESOURCE_UNAVAILABLE",
                "PREDICTION_SIZE_LIMIT", "PREDICTION_COLUMNS_MISMATCH", "PREDICTION_BUSY", "INVALID_IDENTIFIER"}
            terminal = (call.binding.remote_tool_name == "predict_with_model" and resource and
                        resource.get("status") in {"FAILED", "CANCELLED"})
            if code in rejected or terminal:
                raise MCPFailure("ML_" + (code if code in rejected else "PREDICTION_" + resource["status"]),
                    receipt={"lookup_status": "FOUND", "resource": resource} if terminal else None)
            raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True)
        if not resource or structured.get("error") is not None:
            raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True)
        if call.binding.remote_tool_name == "predict_with_model" and resource.get("status") != "SUCCEEDED":
            raise MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True)
        return structured

    async def stop(self):
        for slot in self.slots:
            slot.task.cancel()
        await asyncio.gather(*(slot.task for slot in self.slots), return_exceptions=True)

    def close(self):
        self.portal.call(self.stop)
        self.portal_context.__exit__(None, None, None)
