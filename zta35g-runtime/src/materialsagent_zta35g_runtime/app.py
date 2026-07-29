from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
from threading import Lock
from typing import Any, Optional

from .config import RuntimeSettings
from .constants import (
    HOST,
    MAX_REQUEST_BYTES,
    MODEL_BUNDLE_ID,
    RUNTIME_CONTRACT_VERSION,
    SCHEMA_VERSION,
    TOKEN_HEADER,
    TOOL_ID,
    TOOL_VERSION,
)
from .contracts import (
    ContractError,
    build_execute_response,
    parse_execute_request,
    safe_error,
    serialize_response,
)

_LOGGER = logging.getLogger("materialsagent_zta35g_runtime")
_LOG_FIELDS = frozenset(
    (
        "request_id",
        "task_id",
        "tool_run_id",
        "tool_id",
        "tool_version",
        "schema_version",
        "step",
        "status",
        "duration_ms",
        "error_code",
        "model_bundle_id",
        "device",
    )
)


def _utc_text(value=None):
    # type: (Optional[datetime]) -> str
    resolved = value or datetime.now(timezone.utc)
    return resolved.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _log_event(event, **fields):
    # type: (str, Any) -> None
    payload = {
        "timestamp": _utc_text(),
        "event": event,
    }
    for field_name, value in fields.items():
        if field_name in _LOG_FIELDS and value is not None:
            payload[field_name] = value
    _LOGGER.info(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


@dataclass
class RuntimeState:
    process_started_at: datetime
    execution_lock: Lock
    close_lock: Lock
    execution_count: int = 0
    model_loaded: bool = False
    model_load_failed: bool = False
    load_error_code: Optional[str] = None
    closed: bool = False
    engine_closed: bool = False


class ZTA35GRuntimeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, settings, engine, state):
        # type: (Any, RuntimeSettings, Any, RuntimeState) -> None
        self.settings = settings
        self.engine = engine
        self.runtime_state = state
        super().__init__(server_address, _RuntimeRequestHandler)


class _RuntimeRequestHandler(BaseHTTPRequestHandler):
    server_version = "MaterialsAgentZTA35GRuntime/0.1"
    sys_version = ""

    def log_message(self, _format, *_args):
        # type: (str, Any) -> None
        return

    def _authorized(self):
        # type: () -> bool
        supplied = self.headers.get(TOKEN_HEADER)
        if supplied is None or not secrets.compare_digest(
            supplied, self.server.settings.token
        ):
            self._write_error(
                401,
                ContractError(
                    code="INVALID_RUNTIME_REQUEST",
                    http_status=401,
                    safe_message="Runtime authentication failed.",
                ),
            )
            return False
        return True

    def _write_json(self, status, payload, head_only=False):
        # type: (int, Any, bool) -> None
        try:
            body = serialize_response(payload)
        except ContractError:
            status = 500
            body = serialize_response(
                safe_error(
                    code="INTERNAL_RUNTIME_ERROR",
                    safe_message="Runtime response is invalid.",
                )
            )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    def _write_error(self, status, error):
        # type: (int, ContractError) -> None
        try:
            payload = safe_error(
                code=error.code,
                safe_message=error.safe_message,
                retryable=error.retryable,
                failed_step=error.failed_step,
                details=error.details,
            )
        except ValueError:
            status = 500
            payload = safe_error(
                code="INTERNAL_RUNTIME_ERROR",
                safe_message="Runtime internal processing failed.",
            )
        self._write_json(status, payload)

    def _method_not_allowed(self, head_only=False):
        # type: (bool) -> None
        payload = safe_error(
            code="INVALID_RUNTIME_REQUEST",
            safe_message="Runtime method is not allowed.",
        )
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        body = serialize_response(payload)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_GET(self):
        # type: () -> None
        if not self._authorized():
            return
        if self.path == "/internal/v1/health/live":
            self._live()
            return
        if self.path == "/internal/v1/health/ready":
            self._ready()
            return
        if self.path == "/internal/v1/execute":
            self._method_not_allowed()
            return
        self._write_error(
            404,
            ContractError(
                http_status=404,
                safe_message="Runtime path was not found.",
            ),
        )

    def do_POST(self):
        # type: () -> None
        if not self._authorized():
            return
        if self.path != "/internal/v1/execute":
            if self.path in (
                "/internal/v1/health/live",
                "/internal/v1/health/ready",
            ):
                self._method_not_allowed()
            else:
                self._write_error(
                    404,
                    ContractError(
                        http_status=404,
                        safe_message="Runtime path was not found.",
                    ),
                )
            return
        self._execute()

    def do_PUT(self):
        # type: () -> None
        if self._authorized():
            self._method_not_allowed()

    def do_DELETE(self):
        # type: () -> None
        if self._authorized():
            self._method_not_allowed()

    def do_PATCH(self):
        # type: () -> None
        if self._authorized():
            self._method_not_allowed()

    def do_OPTIONS(self):
        # type: () -> None
        if self._authorized():
            self._method_not_allowed()

    def do_HEAD(self):
        # type: () -> None
        if self._authorized():
            self._method_not_allowed(head_only=True)

    def _live(self):
        # type: () -> None
        state = self.server.runtime_state
        self._write_json(
            200,
            {
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "status": "LIVE",
                "process_started_at": _utc_text(
                    state.process_started_at
                ),
                "checked_at": _utc_text(),
            },
        )

    def _ready(self):
        # type: () -> None
        state = self.server.runtime_state
        busy = state.execution_lock.locked()
        load_error = (
            safe_error(
                code="MODEL_LOAD_FAILED",
                safe_message="Model bundle could not be loaded.",
                failed_step="model_loading",
            )["error"]
            if state.model_load_failed
            else None
        )
        model_bundle_id = (
            getattr(
                self.server.engine,
                "model_bundle_id",
                MODEL_BUNDLE_ID,
            )
            if state.model_loaded
            else None
        )
        self._write_json(
            200 if state.model_loaded else 503,
            {
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "status": (
                    "READY" if state.model_loaded else "NOT_READY"
                ),
                "model_files": {
                    "status": (
                        "AVAILABLE"
                        if state.model_loaded
                        else "INVALID"
                    )
                },
                "model_loaded": state.model_loaded,
                "device": {
                    "status": (
                        "AVAILABLE"
                        if state.model_loaded
                        else "UNAVAILABLE"
                    ),
                    "kind": getattr(
                        self.server.engine,
                        "device_kind",
                        "unknown",
                    )
                    if state.model_loaded
                    else "unknown",
                },
                "can_accept_execution": (
                    state.model_loaded and not state.closed and not busy
                ),
                "busy": busy,
                "supported_tool": {
                    "tool_id": TOOL_ID,
                    "tool_version": TOOL_VERSION,
                    "schema_version": SCHEMA_VERSION,
                },
                "model_bundle_id": model_bundle_id,
                "checked_at": _utc_text(),
                "error": load_error,
            },
        )

    def _request_body(self):
        # type: () -> bytes
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            raise ContractError(
                http_status=411,
                safe_message="Runtime request length is required.",
            )
        try:
            length = int(content_length)
        except ValueError:
            raise ContractError(
                http_status=400,
                safe_message="Runtime request length is invalid.",
            ) from None
        if length < 0:
            raise ContractError(
                http_status=400,
                safe_message="Runtime request length is invalid.",
            )
        if length > MAX_REQUEST_BYTES:
            raise ContractError(
                http_status=413,
                safe_message="Runtime request is too large.",
            )
        if self.headers.get("Transfer-Encoding") is not None:
            raise ContractError(
                http_status=400,
                safe_message="Runtime transfer encoding is invalid.",
            )
        return self.rfile.read(length)

    def _execute(self):
        # type: () -> None
        state = self.server.runtime_state
        if self._reject_closed_execution(state):
            return
        if not state.model_loaded:
            code = (
                "MODEL_LOAD_FAILED"
                if state.model_load_failed
                else "RUNTIME_NOT_READY"
            )
            self._write_error(
                503,
                ContractError(
                    code=code,
                    http_status=503,
                    safe_message=(
                        "Model bundle could not be loaded."
                        if state.model_load_failed
                        else "Runtime is not ready."
                    ),
                    retryable=not state.model_load_failed,
                    failed_step=(
                        "model_loading"
                        if state.model_load_failed
                        else None
                    ),
                ),
            )
            return
        try:
            body = self._request_body()
            request = parse_execute_request(
                body,
                content_type=self.headers.get("Content-Type", ""),
            )
        except ContractError as error:
            self._write_error(error.http_status, error)
            return
        if self._reject_closed_execution(state):
            return
        event_fields = {
            "request_id": request.request_id,
            "task_id": request.task_id,
            "tool_run_id": request.tool_run_id,
            "tool_id": TOOL_ID,
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
        _log_event(
            "execution_received",
            status="RECEIVED",
            **event_fields
        )
        if not state.execution_lock.acquire(blocking=False):
            _log_event(
                "runtime_busy",
                status="BUSY",
                error_code="RUNTIME_BUSY",
                **event_fields
            )
            self._write_error(
                503,
                ContractError(
                    code="RUNTIME_BUSY",
                    http_status=503,
                    safe_message="ZTA35G Runtime is busy.",
                    retryable=True,
                ),
            )
            return
        try:
            if self._reject_closed_execution(state):
                return
            state.execution_count += 1
            result = self.server.engine.execute(
                request.process_parameters,
                request.requested_outputs,
                request.runtime_parameters,
            )
            payload = build_execute_response(request, result)
            device_kind = getattr(
                self.server.engine,
                "device_kind",
                "unknown",
            )
            if device_kind not in ("cpu", "cuda"):
                device_kind = "unknown"
            for diagnostic in payload["diagnostics"]:
                _log_event(
                    diagnostic["step"],
                    step=diagnostic["step"],
                    status=diagnostic["status"],
                    duration_ms=diagnostic["duration_ms"],
                    error_code=diagnostic["error_code"],
                    model_bundle_id=MODEL_BUNDLE_ID,
                    device=device_kind,
                    **event_fields
                )
            terminal_event = (
                "execution_failed"
                if payload["status"] == "FAILED"
                else "execution_completed"
            )
            _log_event(
                terminal_event,
                status=payload["status"],
                error_code=(
                    payload["error"]["code"]
                    if payload["error"] is not None
                    else None
                ),
                model_bundle_id=MODEL_BUNDLE_ID,
                device=device_kind,
                **event_fields
            )
            self._write_json(200, payload)
        except ContractError as error:
            _log_event(
                "execution_failed",
                status="FAILED",
                error_code=error.code,
                **event_fields
            )
            self._write_error(error.http_status, error)
        except Exception:
            _log_event(
                "execution_failed",
                status="FAILED",
                error_code="INTERNAL_RUNTIME_ERROR",
                **event_fields
            )
            self._write_error(
                500,
                ContractError(
                    code="INTERNAL_RUNTIME_ERROR",
                    http_status=500,
                    safe_message="Runtime internal processing failed.",
                ),
            )
        finally:
            state.execution_lock.release()

    def _reject_closed_execution(self, state):
        # type: (RuntimeState) -> bool
        if not state.closed:
            return False
        self._write_error(
            503,
            ContractError(
                code="RUNTIME_NOT_READY",
                http_status=503,
                safe_message="Runtime is shutting down.",
                retryable=True,
            ),
        )
        return True


def create_runtime_server(
    settings,  # type: RuntimeSettings
    engine,  # type: Any
    port=None,  # type: Optional[int]
):
    # type: (...) -> ZTA35GRuntimeHTTPServer
    if not isinstance(settings, RuntimeSettings) or settings.host != HOST:
        raise ValueError("Runtime settings are invalid.")
    resolved_port = settings.port if port is None else port
    if (
        isinstance(resolved_port, bool)
        or not isinstance(resolved_port, int)
        or not 0 <= resolved_port <= 65535
    ):
        raise ValueError("Runtime port is invalid.")
    state = RuntimeState(
        process_started_at=datetime.now(timezone.utc),
        execution_lock=Lock(),
        close_lock=Lock(),
    )
    server = ZTA35GRuntimeHTTPServer(
        (HOST, resolved_port),
        settings,
        engine,
        state,
    )
    _log_event("runtime_started", status="LIVE")
    _log_event("model_loading", step="model_loading", status="STARTED")
    try:
        engine.load()
        loaded = engine.is_loaded()
        model_bundle_id = getattr(engine, "model_bundle_id", None)
        device_kind = getattr(engine, "device_kind", "unknown")
        if (
            loaded is not True
            or model_bundle_id != MODEL_BUNDLE_ID
            or device_kind not in ("cpu", "cuda")
        ):
            raise RuntimeError("Engine identity is invalid.")
        state.model_loaded = True
        _log_event(
            "model_loaded",
            step="model_loading",
            status="SUCCEEDED",
            model_bundle_id=MODEL_BUNDLE_ID,
            device=device_kind,
        )
    except Exception:
        state.model_loaded = False
        state.model_load_failed = True
        state.load_error_code = "MODEL_LOAD_FAILED"
        if not state.engine_closed:
            try:
                engine.close()
            except Exception:
                pass
            finally:
                state.engine_closed = True
        _log_event(
            "model_load_failed",
            step="model_loading",
            status="FAILED",
            error_code="MODEL_LOAD_FAILED",
            device="unknown",
        )
    return server


def close_runtime_server(server):
    # type: (ZTA35GRuntimeHTTPServer) -> None
    state = server.runtime_state
    with state.close_lock:
        if state.closed:
            return
        state.closed = True
        _log_event(
            "runtime_stopping",
            status="STOPPING",
            model_bundle_id=(
                MODEL_BUNDLE_ID
                if state.model_loaded
                else None
            ),
            device=(
                getattr(server.engine, "device_kind", "unknown")
                if state.model_loaded
                else "unknown"
            ),
        )
        try:
            with state.execution_lock:
                if not state.engine_closed:
                    try:
                        server.engine.close()
                    finally:
                        state.engine_closed = True
        finally:
            server.server_close()
