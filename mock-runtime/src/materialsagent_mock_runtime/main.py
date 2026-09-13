from __future__ import annotations

from array import array
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
import secrets
import struct
import sys
import threading
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8100
RUNTIME_CONTRACT_VERSION: Final = "1.0"
TOOL_ID: Final = "zta35g_sem_virtual_lab"
TOOL_VERSION: Final = "0.1.0"
SCHEMA_VERSION: Final = "1.0"
MODEL_BUNDLE_ID: Final = "mock-zta35g-bundle"
TOKEN_HEADER: Final = "X-ZTA35G-Runtime-Token"
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
SUPPORTED_OUTPUTS: Final = frozenset(
    {"sem_image", "mechanical_properties"}
)
RUNTIME_ERROR_CODES: Final = frozenset(
    {
        "INVALID_RUNTIME_REQUEST",
        "UNSUPPORTED_TOOL",
        "SCHEMA_VERSION_MISMATCH",
        "TOOL_VERSION_MISMATCH",
        "RUNTIME_NOT_READY",
        "RUNTIME_BUSY",
        "MODEL_LOAD_FAILED",
        "SEM_GENERATION_FAILED",
        "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "INVALID_MODEL_OUTPUT",
        "INTERNAL_RUNTIME_ERROR",
    }
)


@dataclass(frozen=True, slots=True)
class MockRuntimeSettings:
    token: str = field(repr=False)
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("Runtime token is required.")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ValueError("Runtime port is invalid.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Runtime port is invalid.")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProcessParameters(StrictModel):
    solution_temperature: int
    solution_time: int | float
    aging_temperature: int
    aging_time: int | float


class RuntimeParameters(StrictModel):
    seed: int
    num_samples: int
    guide_scale: int | float
    timesteps: int


class ExecuteRequest(StrictModel):
    runtime_contract_version: str
    request_id: str
    task_id: str
    tool_run_id: str
    tool_id: str
    tool_version: str
    schema_version: str
    process_parameters: ProcessParameters
    requested_outputs: list[str]
    runtime_parameters: RuntimeParameters


class RuntimeRequestError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        safe_message: str,
        retryable: bool = False,
        failed_step: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        if code not in RUNTIME_ERROR_CODES:
            raise ValueError("Runtime error code is not defined by the contract.")
        super().__init__(safe_message)
        self.status_code = status_code
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.failed_step = failed_step
        self.details = dict(details or {})


@dataclass(slots=True)
class RuntimeState:
    process_started_at: datetime
    execution_lock: threading.Lock
    execution_count: int
    image_npy: bytes
    image_base64: str
    image_sha256: str


def _utc_text(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    return timestamp.isoformat().replace("+00:00", "Z")


def _deterministic_npy() -> bytes:
    header_text = (
        "{'descr': '<f4', 'fortran_order': False, "
        "'shape': (512, 512), }"
    )
    header = header_text.encode("latin1")
    padding = (-(10 + len(header) + 1)) % 16
    header = header + (b" " * padding) + b"\n"
    prefix = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header))
    pixel_count = 512 * 512
    pixels = array(
        "f",
        (-1.0 + (2.0 * index / (pixel_count - 1)) for index in range(pixel_count)),
    )
    if sys.byteorder != "little":
        pixels.byteswap()
    return prefix + header + pixels.tobytes()


def _safe_error_response(error: RuntimeRequestError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "error": {
                "code": error.code,
                "safe_message": error.safe_message,
                "retryable": error.retryable,
                "failed_step": error.failed_step,
                "details": error.details,
            },
        },
    )


def _invalid_request() -> RuntimeRequestError:
    return RuntimeRequestError(
        status_code=422,
        code="INVALID_RUNTIME_REQUEST",
        safe_message="Runtime 请求无效。",
    )


def _finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


def _validate_process_parameters(parameters: ProcessParameters) -> None:
    ranges = {
        "solution_temperature": (Decimal("900"), Decimal("1100"), True),
        "solution_time": (Decimal("1"), Decimal("5"), False),
        "aging_temperature": (Decimal("670"), Decimal("790"), True),
        "aging_time": (Decimal("1"), Decimal("5"), False),
    }
    for field_name, (lower, upper, integer_required) in ranges.items():
        value = _finite_decimal(getattr(parameters, field_name))
        if value is None or value < lower or value > upper:
            raise _invalid_request()
        if integer_required and value != value.to_integral_value():
            raise _invalid_request()
        if not integer_required and value * 10 != (value * 10).to_integral_value():
            raise _invalid_request()


def _validate_request(payload: ExecuteRequest) -> None:
    for field_name in ("request_id", "task_id", "tool_run_id"):
        value = getattr(payload, field_name)
        if not value or value != value.strip() or len(value) > 256:
            raise _invalid_request()
    if payload.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeRequestError(
            status_code=409,
            code="SCHEMA_VERSION_MISMATCH",
            safe_message="Runtime 协议版本不兼容。",
        )
    if payload.tool_id != TOOL_ID:
        raise RuntimeRequestError(
            status_code=422,
            code="UNSUPPORTED_TOOL",
            safe_message="Runtime 不支持该工具。",
        )
    if payload.tool_version != TOOL_VERSION:
        raise RuntimeRequestError(
            status_code=409,
            code="TOOL_VERSION_MISMATCH",
            safe_message="Tool 版本不兼容。",
        )
    if payload.schema_version != SCHEMA_VERSION:
        raise RuntimeRequestError(
            status_code=409,
            code="SCHEMA_VERSION_MISMATCH",
            safe_message="Schema 版本不兼容。",
        )
    if (
        not payload.requested_outputs
        or len(payload.requested_outputs) > len(SUPPORTED_OUTPUTS)
        or len(set(payload.requested_outputs)) != len(payload.requested_outputs)
        or not set(payload.requested_outputs) <= SUPPORTED_OUTPUTS
    ):
        raise _invalid_request()
    runtime_parameters = payload.runtime_parameters
    if (
        isinstance(runtime_parameters.seed, bool)
        or runtime_parameters.seed < 0
        or runtime_parameters.num_samples != 1
        or _finite_decimal(runtime_parameters.guide_scale) != Decimal("2.0")
        or runtime_parameters.timesteps != 1000
    ):
        raise _invalid_request()
    _validate_process_parameters(payload.process_parameters)


def _diagnostic(step: str) -> dict[str, object]:
    timestamp = _utc_text()
    return {
        "step": step,
        "status": "SUCCEEDED",
        "started_at": timestamp,
        "completed_at": timestamp,
        "duration_ms": 0,
        "error_code": None,
        "safe_error_message": None,
    }


def _success_payload(
    request: ExecuteRequest,
    state: RuntimeState,
) -> dict[str, object]:
    requested_outputs = list(request.requested_outputs)
    mechanical_requested = "mechanical_properties" in requested_outputs
    sem_requested = "sem_image" in requested_outputs
    diagnostics = [_diagnostic("sem_generation")]
    if mechanical_requested:
        diagnostics.append(_diagnostic("mechanical_property_prediction"))
    data: dict[str, object] = {}
    if mechanical_requested:
        data = {
            "yield_strength": {"value": 650.0, "unit": "MPa"},
            "elongation": {"value": 3.2, "unit": "%"},
        }
    return {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "request_id": request.request_id,
        "task_id": request.task_id,
        "tool_run_id": request.tool_run_id,
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "status": "SUCCEEDED",
        "requested_outputs": requested_outputs,
        "completed_outputs": requested_outputs,
        "failed_outputs": [],
        "data": data,
        "images": [
            {
                "image_role": (
                    "generated_sem" if sem_requested else "intermediate_sem"
                ),
                "requested_output": sem_requested,
                "dtype": "float32",
                "numpy_dtype": "<f4",
                "shape": [512, 512],
                "channel_layout": "GRAYSCALE_2D",
                "value_range": [-1.0, 1.0],
                "encoding": "base64+npy",
                "byte_order": "little",
                "array_order": "C",
                "sha256": state.image_sha256,
                "data_base64": state.image_base64,
            }
        ],
        "warnings": [],
        "diagnostics": diagnostics,
        "actual_runtime_parameters": request.runtime_parameters.model_dump(),
        "model_bundle_id": MODEL_BUNDLE_ID,
        "error": None,
    }


def create_app(
    settings: MockRuntimeSettings | None = None,
    *,
    execution_hook: Callable[[], None] | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    image_npy = _deterministic_npy()
    state = RuntimeState(
        process_started_at=datetime.now(timezone.utc),
        execution_lock=threading.Lock(),
        execution_count=0,
        image_npy=image_npy,
        image_base64=base64.b64encode(image_npy).decode("ascii"),
        image_sha256=sha256(image_npy).hexdigest(),
    )
    app = FastAPI(title="Materials Agent Mock ZTA35G Runtime", version="0.1.0")
    app.state.runtime_state = state

    def authorize(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER)
        if supplied is None or not secrets.compare_digest(
            supplied,
            resolved_settings.token,
        ):
            raise RuntimeRequestError(
                status_code=401,
                code="INVALID_RUNTIME_REQUEST",
                safe_message="Runtime authentication failed.",
            )

    @app.middleware("http")
    async def enforce_execute_transport(request: Request, call_next):
        if request.url.path == "/internal/v1/execute" and request.method == "POST":
            content_type = request.headers.get("content-type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                return _safe_error_response(
                    RuntimeRequestError(
                        status_code=415,
                        code="INVALID_RUNTIME_REQUEST",
                        safe_message="Runtime 请求无效。",
                    )
                )
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_REQUEST_BYTES:
                        return _safe_error_response(_invalid_request())
                except ValueError:
                    return _safe_error_response(_invalid_request())
        return await call_next(request)

    @app.exception_handler(RuntimeRequestError)
    async def runtime_error_handler(
        _request: Request,
        error: RuntimeRequestError,
    ) -> JSONResponse:
        return _safe_error_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _safe_error_response(_invalid_request())

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        _request: Request,
        _error: Exception,
    ) -> JSONResponse:
        return _safe_error_response(
            RuntimeRequestError(
                status_code=500,
                code="INTERNAL_RUNTIME_ERROR",
                safe_message="Runtime 内部处理失败。",
            )
        )

    @app.get("/internal/v1/health/live")
    def live(request: Request) -> dict[str, object]:
        authorize(request)
        return {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "status": "LIVE",
            "process_started_at": _utc_text(state.process_started_at),
            "checked_at": _utc_text(),
        }

    @app.get("/internal/v1/health/ready")
    def ready(request: Request) -> dict[str, object]:
        authorize(request)
        busy = state.execution_lock.locked()
        return {
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "status": "READY",
            "model_files": {"status": "AVAILABLE"},
            "model_loaded": True,
            "device": {"status": "AVAILABLE", "kind": "cpu"},
            "can_accept_execution": not busy,
            "busy": busy,
            "supported_tool": {
                "tool_id": TOOL_ID,
                "tool_version": TOOL_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
            "model_bundle_id": MODEL_BUNDLE_ID,
            "checked_at": _utc_text(),
            "error": None,
        }

    @app.post("/internal/v1/execute")
    def execute(payload: ExecuteRequest, request: Request) -> dict[str, object]:
        authorize(request)
        _validate_request(payload)
        if not state.execution_lock.acquire(blocking=False):
            raise RuntimeRequestError(
                status_code=503,
                code="RUNTIME_BUSY",
                safe_message="ZTA35G Runtime 正忙。",
                retryable=True,
            )
        try:
            state.execution_count += 1
            if execution_hook is not None:
                execution_hook()
            response = _success_payload(payload, state)
            response_size = len(
                json.dumps(
                    response,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if response_size > MAX_RESPONSE_BYTES:
                raise RuntimeRequestError(
                    status_code=500,
                    code="INTERNAL_RUNTIME_ERROR",
                    safe_message="Runtime 内部处理失败。",
                )
            return response
        finally:
            state.execution_lock.release()

    from .ebsd import install
    install(app, state, authorize, RuntimeRequestError)
    return app


def load_settings() -> MockRuntimeSettings:
    token = os.environ.get("ZTA35G_RUNTIME_TOKEN")
    port_text = os.environ.get("ZTA35G_RUNTIME_PORT", str(DEFAULT_PORT))
    if token is None or not token:
        raise RuntimeError("ZTA35G_RUNTIME_TOKEN is required.")
    try:
        port = int(port_text)
    except ValueError:
        raise RuntimeError("ZTA35G_RUNTIME_PORT is invalid.") from None
    try:
        return MockRuntimeSettings(token=token, port=port)
    except ValueError as error:
        raise RuntimeError(str(error)) from None


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=DEFAULT_HOST,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
