"""Explicitly labelled TC4 mock; preserves the single-image receipt protocol."""
from hashlib import sha256
from io import BytesIO
import json
import re
from fastapi import Request, Response
from PIL import Image

TOOL_ID = "tc4_primary_alpha_segmentation"
MODEL_VERSION = "mock-tc4-bundle"
PREPROCESSING_VERSION = "rgb-letterbox512-softmax-resize-argmax-v1"


def install(app, state, authorize, error_type):
    state.tc4_receipts = {}
    state.tc4_artifacts = {}

    def error(status=422, code="INVALID_RUNTIME_REQUEST"):
        return error_type(status_code=status, code=code, safe_message="TC4 mock request could not be completed.")

    @app.get("/internal/v1/tc4/health/ready")
    def ready(request: Request):
        authorize(request)
        return {"status": "READY", "tool_id": TOOL_ID, "model_version": MODEL_VERSION,
                "preprocessing_version": PREPROCESSING_VERSION, "busy": state.execution_lock.locked(), "device": "mock"}

    @app.post("/internal/v1/tc4/execute")
    async def execute(request: Request):
        authorize(request)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 10 * 1024 * 1024:
                raise error(413)
        try:
            header = request.headers.get("X-TC4-Request", "")
            if len(header) > 4096 or request.headers.get("Content-Type") != "application/octet-stream":
                raise ValueError()
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError()
                return value
            value = json.loads(header, object_pairs_hook=unique)
            if set(value) != {"request_id", "conversation_id", "task_id", "tool_run_id", "item_id", "asset_id", "sha256"}:
                raise ValueError()
            if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for k, v in value.items() if k != "sha256"):
                raise ValueError()
            if sha256(payload).hexdigest() != value["sha256"]:
                raise ValueError()
            with Image.open(BytesIO(payload)) as source:
                if (source.mode not in {"RGB", "L", "RGBA"} or source.format not in {"PNG", "JPEG"}
                        or getattr(source, "n_frames", 1) != 1
                        or not (128 <= source.width <= 4096 and 128 <= source.height <= 4096)):
                    raise ValueError()
                # Match the real Runtime's encoded bit-depth check.
                if source.format == "PNG" and (payload[8:16] != b"\x00\x00\x00\rIHDR" or payload[24] != 8):
                    raise ValueError()
                source.load()
                if ((source.mode == "RGBA" or "transparency" in source.info)
                        and source.convert("RGBA").getchannel("A").getextrema() != (255, 255)):
                    raise ValueError()
                image = source.convert("RGB")
        except Exception:
            raise error() from None
        if not state.execution_lock.acquire(blocking=False):
            raise error(503, "RUNTIME_BUSY")
        try:
            identity = value["request_id"]
            old = state.tc4_receipts.get(identity)
            if old is not None:
                if old["request"] != value:
                    raise error(409)
                return old
            state.execution_count += 1
            mask = image.convert("L").point(lambda x: 255 if x >= 128 else 0)
            color = Image.new("RGB", image.size)
            color.paste((128, 0, 0), mask=mask)
            overlay = Image.blend(image, color, 0.5)
            descriptors = {}
            for role, output in (("overlay", overlay), ("mask", mask)):
                stream = BytesIO()
                output.save(stream, format="PNG")
                content = stream.getvalue()
                state.tc4_artifacts[(identity, role)] = content
                descriptors[role] = {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
            total = image.width * image.height
            foreground = mask.histogram()[255]
            receipt = {"request": value, "status": "SUCCEEDED", "tool_id": TOOL_ID,
                "model_version": MODEL_VERSION, "preprocessing_version": PREPROCESSING_VERSION,
                "data": {"width": image.width, "height": image.height, "foreground_pixels": foreground,
                    "total_pixels": total, "area_fraction": foreground / total}, "artifacts": descriptors}
            state.tc4_receipts[identity] = receipt
            return receipt
        finally:
            state.execution_lock.release()

    @app.get("/internal/v1/tc4/receipts/{identity}")
    def lookup(identity: str, request: Request):
        authorize(request)
        receipt = state.tc4_receipts.get(identity)
        return receipt if receipt else Response(status_code=404)

    @app.get("/internal/v1/tc4/receipts/{identity}/{role}")
    def artifact(identity: str, role: str, request: Request):
        authorize(request)
        content = state.tc4_artifacts.get((identity, role))
        return Response(content=content, media_type="image/png") if content else Response(status_code=404)

    @app.post("/internal/v1/tc4/cleanup")
    async def cleanup(request: Request):
        authorize(request)
        value = await request.json()
        receipt = state.tc4_receipts.get(value.get("request_id"))
        if receipt is None:
            return {"status": "DELETED"}
        if receipt["request"] != value or receipt["status"] not in {"SUCCEEDED", "FAILED", "DELETED"}:
            raise error(409)
        for role in ("overlay", "mask"):
            state.tc4_artifacts.pop((value["request_id"], role), None)
        state.tc4_receipts[value["request_id"]] = {"request": value, "status": "DELETED"}
        return {"status": "DELETED"}
