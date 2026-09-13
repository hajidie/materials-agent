"""Deterministic EBSD HTTP mock with the real binary request boundary."""
from hashlib import sha256
from io import BytesIO
import json
import re

from PIL import Image
from fastapi import Request

TOOL_ID = "ebsd_yield_strength_predictor"
MAX_BYTES = 10 * 1024 * 1024


def install(app, state, authorize, error_type):
    def invalid():
        return error_type(status_code=422, code="INVALID_RUNTIME_REQUEST", safe_message="Invalid EBSD request.")

    @app.get("/internal/v1/ebsd/health/ready")
    def ready(request: Request):
        authorize(request)
        return {"status": "READY", "tool_id": TOOL_ID, "schema_version": "1.0",
            "model_bundle_id": "mock-ebsd-bundle", "busy": state.execution_lock.locked(), "device": "mock"}

    @app.post("/internal/v1/ebsd/execute")
    async def execute(request: Request):
        authorize(request)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_BYTES:
                raise error_type(status_code=413, code="INVALID_RUNTIME_REQUEST", safe_message="Runtime request is too large.")
        try:
            header = request.headers.get("X-EBSD-Request", "")
            if len(header) > 4096 or request.headers.get("Content-Type") != "application/octet-stream":
                raise ValueError()
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError()
                return value
            value = json.loads(header, object_pairs_hook=unique)
            if set(value) != {"request_id", "task_id", "tool_run_id", "asset_id", "sha256", "seed", "tool_id", "schema_version"}:
                raise ValueError()
            if any(not isinstance(value[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[k]) for k in ("request_id", "task_id", "tool_run_id", "asset_id")):
                raise ValueError()
            if (value["tool_id"] != TOOL_ID or value["schema_version"] != "1.0"
                    or type(value["seed"]) is not int or not 0 <= value["seed"] < 2 ** 32
                    or sha256(payload).hexdigest() != value["sha256"]):
                raise ValueError()
            with Image.open(BytesIO(payload)) as image:
                if (image.mode != "RGB" or image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1
                        or image.width != image.height or not 128 <= image.width <= 4096):
                    raise ValueError()
                image.load()
        except Exception:
            raise invalid() from None
        if not state.execution_lock.acquire(blocking=False):
            raise error_type(status_code=503, code="RUNTIME_BUSY", safe_message="Runtime is busy.", retryable=True)
        try:
            state.execution_count += 1
            return {**{key: value[key] for key in ("request_id", "task_id", "tool_run_id", "tool_id", "schema_version", "asset_id", "sha256")},
                "status": "SUCCEEDED", "requested_outputs": ["yield_strength"], "completed_outputs": ["yield_strength"],
                "failed_outputs": [], "data": {"yield_strength": {"value": 400.0, "unit": "MPa"}},
                "images": [], "warnings": [{"code": "MOCK_RESULT", "message": "Mock 模拟结果，不是 CNN 真实预测。"}],
                "diagnostics": [], "error": None, "model_bundle_id": "mock-ebsd-bundle",
                "actual_runtime_parameters": {"seed": value["seed"], "fp32": 1, "cudnn_tf32": 1, "matmul_tf32": 0}}
        finally:
            state.execution_lock.release()
