"""TC4 inference and durable single-image receipts; serialized by the GPU lock."""
import hashlib
import io
import json
import os
import re
from pathlib import Path

from .contracts import ContractError

TOOL_ID = "tc4_primary_alpha_segmentation"
BUNDLE_ID = "tc4-primary-alpha-resnet50-unet-v1"
PREPROCESSING_VERSION = "rgb-letterbox512-softmax-resize-argmax-v1"
WEIGHT_SHA256 = "d8cfdde273432bef29e3c5e7cd01f857d8e8a6222c57b167f6c198fb14290c7b"
HEADER = "X-TC4-Request"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def decode_image(payload):
    from PIL import Image
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("Invalid image size.")
    with Image.open(io.BytesIO(payload)) as image:
        if (image.format not in ("PNG", "JPEG") or image.mode not in ("RGB", "L", "RGBA")
                or getattr(image, "n_frames", 1) != 1
                or not (128 <= image.width <= 4096 and 128 <= image.height <= 4096)):
            raise ValueError("Unsupported TC4 image.")
        # Pillow exposes 16-bit RGB PNGs as RGB, so validate the encoded IHDR.
        if image.format == "PNG" and (payload[8:16] != b"\x00\x00\x00\rIHDR" or payload[24] != 8):
            raise ValueError("TC4 requires an 8-bit PNG.")
        image.load()
        if ((image.mode == "RGBA" or "transparency" in image.info)
                and image.convert("RGBA").getchannel("A").getextrema() != (255, 255)):
            raise ValueError("Transparent TC4 images are unsupported.")
        return image.convert("RGB")


def parse_request(header, payload, content_type):
    try:
        if not header or len(header) > 4096 or content_type != "application/octet-stream":
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        request = json.loads(header, object_pairs_hook=unique)
        if set(request) != {"request_id", "conversation_id", "task_id", "tool_run_id", "item_id", "asset_id", "sha256"}:
            raise ValueError()
        if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
               for key, value in request.items() if key != "sha256"):
            raise ValueError()
        if hashlib.sha256(payload).hexdigest() != request["sha256"]:
            raise ValueError()
        decode_image(payload)
        return request
    except Exception:
        raise ContractError(safe_message="Invalid TC4 image or request.") from None


def png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class TC4Engine:
    def __init__(self, weights):
        self.weights = Path(weights)
        self.model = None

    def load(self):
        import torch
        from .tc4_model import TC4UNet
        content = self.weights.read_bytes()
        if hashlib.sha256(content).hexdigest() != WEIGHT_SHA256 or not torch.cuda.is_available():
            raise ValueError("TC4 model is unavailable.")
        model = TC4UNet()
        model.load_state_dict(torch.load(io.BytesIO(content), map_location="cpu"), strict=True)
        self.model = model.to(device="cuda", dtype=torch.float32).eval()

    def execute(self, payload):
        import cv2
        import numpy as np
        import torch
        from PIL import Image
        from torch.nn import functional as F
        image = decode_image(payload)
        width, height = image.size
        scale = min(512 / width, 512 / height)
        nw, nh = int(width * scale), int(height * scale)
        canvas = Image.new("RGB", (512, 512), (128, 128, 128))
        canvas.paste(image.resize((nw, nh), Image.Resampling.BICUBIC), ((512 - nw) // 2, (512 - nh) // 2))
        array = np.array(canvas, np.float32) / 255.0
        tensor = torch.from_numpy(np.expand_dims(np.transpose(array, (2, 0, 1)), 0)).to(device="cuda", dtype=torch.float32)
        with torch.no_grad():
            scores = F.softmax(self.model(tensor)[0].permute(1, 2, 0), dim=-1).cpu().numpy()
        if not np.isfinite(scores).all():
            raise ValueError("Invalid model output.")
        y, x = (512 - nh) // 2, (512 - nw) // 2
        mask = cv2.resize(scores[y:y + nh, x:x + nw], (width, height), interpolation=cv2.INTER_LINEAR).argmax(axis=-1).astype(np.uint8)
        colors = np.array([(0, 0, 0), (128, 0, 0)], np.uint8)
        overlay = Image.blend(image, Image.fromarray(colors[mask]), 0.5)
        foreground = int(np.count_nonzero(mask))
        return {"width": width, "height": height, "foreground_pixels": foreground,
                "total_pixels": width * height, "area_fraction": foreground / (width * height)}, {
                "overlay": png(overlay), "mask": png(Image.fromarray(mask * 255))}

    def close(self):
        self.model = None


class ReceiptStore:
    """Only terminal receipts are replayable; a persisted dispatch is never rerun."""
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, request_id):
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ValueError("Invalid request identity.")
        return self.root / request_id

    @staticmethod
    def write(path, content):
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))

    def read(self, request_id):
        path = self.directory(request_id) / "receipt.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def save(self, request_id, value):
        self.write(self.directory(request_id) / "receipt.json", json.dumps(value, allow_nan=False).encode("utf-8"))

    def cleanup(self, request):
        previous = self.read(request["request_id"])
        if previous is None:
            # Preserve the identity even for an absent operation.
            self.directory(request["request_id"]).mkdir(exist_ok=True)
        elif previous["request"] != request or previous["status"] not in ("SUCCEEDED", "FAILED", "DELETED"):
            raise ContractError(http_status=409, safe_message="Original operation is not safe to delete.")
        # Tombstone first: a crash cannot authorize re-execution or artifact reads.
        self.save(request["request_id"], {"request": request, "status": "DELETED"})
        for role in ("overlay", "mask"):
            path = self.directory(request["request_id"]) / (role + ".png")
            if path.exists():
                path.unlink()
        return {"status": "DELETED"}

    def execute(self, engine, request, payload):
        directory = self.directory(request["request_id"])
        previous = self.read(request["request_id"])
        if previous:
            if previous["request"] != request:
                raise ContractError(http_status=409, safe_message="Request identity conflict.")
            return previous
        directory.mkdir(exist_ok=True)
        receipt = {"request": request, "status": "OUTCOME_UNKNOWN", "tool_id": TOOL_ID,
                   "model_version": BUNDLE_ID, "preprocessing_version": PREPROCESSING_VERSION}
        # This record must reach disk before inference. A crash leaves it unknown.
        self.save(request["request_id"], receipt)
        try:
            data, artifacts = engine.execute(payload)
            descriptors = {}
            for role, content in artifacts.items():
                self.write(directory / (role + ".png"), content)
                descriptors[role] = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
            receipt.update(status="SUCCEEDED", data=data, artifacts=descriptors)
        except Exception:
            receipt.update(status="FAILED", error={"code": "TC4_INFERENCE_FAILED", "message": "图像分割运行失败，请检查 Runtime 后核查原操作。"})
        self.save(request["request_id"], receipt)
        return receipt


def ready(handler):
    loaded = getattr(handler.server, "tc4_engine", None) is not None
    state = handler.server.runtime_state
    handler._write_json(200 if loaded and not state.closed else 503, {
        "status": "READY" if loaded and not state.closed else "NOT_READY", "tool_id": TOOL_ID,
        "model_version": BUNDLE_ID, "preprocessing_version": PREPROCESSING_VERSION,
        "busy": state.execution_lock.locked(), "device": "cuda" if loaded else None})


def execute(handler):
    state = handler.server.runtime_state
    if handler._reject_closed_execution(state):
        return
    try:
        if getattr(handler.server, "tc4_engine", None) is None:
            raise ContractError(code="RUNTIME_NOT_READY", http_status=503, safe_message="TC4 model is unavailable.")
        payload = handler._request_body(MAX_IMAGE_BYTES)
        request = parse_request(handler.headers.get(HEADER), payload, handler.headers.get("Content-Type"))
        if not state.execution_lock.acquire(blocking=False):
            raise ContractError(code="RUNTIME_BUSY", http_status=503, safe_message="Runtime is busy.", retryable=True)
        try:
            if handler._reject_closed_execution(state):
                return
            if handler.server.tc4_receipts.read(request["request_id"]) is None:
                state.execution_count += 1
            result = handler.server.tc4_receipts.execute(handler.server.tc4_engine, request, payload)
            handler._write_json(200, result)
        finally:
            state.execution_lock.release()
    except ContractError as error:
        handler._write_error(error.http_status, error)
    except Exception:
        handler._write_error(500, ContractError(code="INTERNAL_RUNTIME_ERROR", http_status=500,
            safe_message="TC4 receipt could not be confirmed."))


def get(handler):
    """Authenticated read-only lookup and bounded artifact access."""
    try:
        parts = handler.path.split("/")
        if len(parts) not in (6, 7) or parts[4] != "receipts":
            raise ValueError()
        store = getattr(handler.server, "tc4_receipts", None)
        receipt = store.read(parts[5]) if store else None
        if receipt is None:
            handler._write_json(404, {"status": "NOT_FOUND"})
            return
        if len(parts) == 6:
            handler._write_json(200, receipt)
            return
        role = parts[6]
        if role not in ("overlay", "mask") or receipt["status"] != "SUCCEEDED":
            raise ValueError()
        content = (store.directory(parts[5]) / (role + ".png")).read_bytes()
        if hashlib.sha256(content).hexdigest() != receipt["artifacts"][role]["sha256"]:
            raise ValueError()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(content)
    except (ValueError, OSError):
        handler._write_json(404, {"status": "NOT_FOUND"})


def cleanup(handler):
    state = handler.server.runtime_state
    try:
        value = json.loads(handler._request_body(4096))
        if (set(value) != {"request_id", "conversation_id", "task_id", "tool_run_id", "item_id", "asset_id", "sha256"}
                or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for k, v in value.items() if k != "sha256")
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
            raise ValueError()
        store = getattr(handler.server, "tc4_receipts", None)
        if store is None:
            raise ContractError(http_status=503, code="RUNTIME_NOT_READY", safe_message="Receipt storage is unavailable.")
        if not state.execution_lock.acquire(blocking=False):
            raise ContractError(http_status=503, code="RUNTIME_BUSY", safe_message="Runtime is busy.", retryable=True)
        try:
            handler._write_json(200, store.cleanup(value))
        finally:
            state.execution_lock.release()
    except ContractError as error:
        handler._write_error(error.http_status, error)
    except Exception:
        handler._write_error(409, ContractError(http_status=409, safe_message="Cleanup identity could not be confirmed."))
