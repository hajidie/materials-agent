"""EBSD-only binary HTTP contract and inference; no global precision mutations."""
import hashlib
import io
import json
import math
import re
from pathlib import Path

from .contracts import ContractError

TOOL_ID = "ebsd_yield_strength_predictor"
BUNDLE_ID = "inconel625-cnn1-v1"
WEIGHT_SHA256 = "714eb8099e4c97076b5f1f62393bb2e95a2847b3663714cf2ba5dfbc83b269cf"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
HEADER = "X-EBSD-Request"


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
        if set(request) != {"request_id", "task_id", "tool_run_id", "asset_id", "sha256", "seed", "tool_id", "schema_version"}:
            raise ValueError()
        for key in ("request_id", "task_id", "tool_run_id", "asset_id"):
            if not isinstance(request[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request[key]):
                raise ValueError()
        if request["tool_id"] != TOOL_ID or request["schema_version"] != "1.0":
            raise ValueError()
        if type(request["seed"]) is not int or not 0 <= request["seed"] < 2 ** 32:
            raise ValueError()
        if not payload or len(payload) > MAX_IMAGE_BYTES or hashlib.sha256(payload).hexdigest() != request["sha256"]:
            raise ValueError()
        decode_image(payload)
        return request
    except Exception:
        raise ContractError(safe_message="Invalid EBSD image or request.") from None


def decode_image(payload):
    from PIL import Image
    with Image.open(io.BytesIO(payload)) as image:
        if (image.format not in ("PNG", "JPEG") or image.mode != "RGB"
                or getattr(image, "n_frames", 1) != 1 or image.width != image.height
                or not 128 <= image.width <= 4096):
            raise ValueError("Unsupported EBSD image.")
        image.load()
        return image.copy()


def build_cnn():
    from torch import nn
    class CNN(nn.Module):
        def __init__(self):
            super().__init__()
            layers = []
            channels = 3
            for index, output in enumerate((16, 16, 32, 32, 64, 64)):
                layers.extend((nn.Conv2d(channels, output, 3, 1, 1), nn.BatchNorm2d(output), nn.ReLU()))
                if index in (1, 3):
                    layers.append(nn.MaxPool2d(2, 2))
                channels = output
            self.features = nn.Sequential(*layers)
            self.gmp = nn.AdaptiveAvgPool2d((1, 1))
            self.linear_1 = nn.Linear(64, 20)
            self.drop_1 = nn.Dropout(0.3)
            self.linear_3 = nn.Linear(20, 1)
            self.flat = nn.Flatten()

        def forward(self, value):
            return self.linear_3(self.drop_1(self.linear_1(self.flat(self.gmp(self.features(value))))))
    return CNN()


class EBSDEngine:
    def __init__(self, root):
        self.root = Path(root)
        self.model = None

    def load(self):
        import torch
        from torchvision import transforms
        weights = self.root / "model" / "save" / "CNN_1.pt"
        weight_bytes = weights.read_bytes()
        if hashlib.sha256(weight_bytes).hexdigest() != WEIGHT_SHA256 or not torch.cuda.is_available():
            raise ValueError("EBSD model is unavailable.")
        model = build_cnn()
        model.load_state_dict(torch.load(io.BytesIO(weight_bytes), map_location="cpu"), strict=True)
        self.model = model.to(device="cuda", dtype=torch.float32).eval()
        self.transform = transforms.Compose([transforms.ToTensor(), transforms.Resize(128)])

    def execute(self, request, payload):
        import torch
        image = decode_image(payload)
        tensor = self.transform(image).unsqueeze(0).to(device="cuda", dtype=torch.float32)
        with torch.no_grad():
            value = float(self.model(tensor).item())
        if not math.isfinite(value):
            raise ValueError("Invalid EBSD prediction.")
        return {
            "request_id": request["request_id"], "task_id": request["task_id"],
            "tool_run_id": request["tool_run_id"], "tool_id": TOOL_ID, "schema_version": "1.0",
            "asset_id": request["asset_id"], "sha256": request["sha256"],
            "status": "SUCCEEDED", "requested_outputs": ["yield_strength"],
            "completed_outputs": ["yield_strength"], "failed_outputs": [],
            "data": {"yield_strength": {"value": value, "unit": "MPa"}}, "images": [],
            "warnings": [{"code": "EXPERIMENTAL_MODEL", "message": "仅适用于 Inconel 625；图像编码与适用数据分布尚未核实。"}],
            "diagnostics": [], "error": None, "model_bundle_id": BUNDLE_ID,
            "actual_runtime_parameters": {"seed": request["seed"], "fp32": 1,
                "cudnn_tf32": int(torch.backends.cudnn.allow_tf32),
                "matmul_tf32": int(torch.backends.cuda.matmul.allow_tf32)},
        }

    def close(self):
        self.model = None


def ready(handler):
    loaded = getattr(handler.server, "ebsd_engine", None) is not None
    state = handler.server.runtime_state
    handler._write_json(200 if loaded and not state.closed else 503, {
        "status": "READY" if loaded and not state.closed else "NOT_READY",
        "tool_id": TOOL_ID, "schema_version": "1.0", "model_bundle_id": BUNDLE_ID if loaded else None,
        "busy": state.execution_lock.locked(), "device": "cuda" if loaded else None,
    })


def execute(handler):
    state = handler.server.runtime_state
    if handler._reject_closed_execution(state):
        return
    try:
        if getattr(handler.server, "ebsd_engine", None) is None:
            raise ContractError(code="RUNTIME_NOT_READY", http_status=503, safe_message="EBSD model is unavailable.", retryable=True)
        payload = handler._request_body(MAX_IMAGE_BYTES)
        request = parse_request(handler.headers.get(HEADER), payload, handler.headers.get("Content-Type"))
        if not state.execution_lock.acquire(blocking=False):
            raise ContractError(code="RUNTIME_BUSY", http_status=503, safe_message="Runtime is busy.", retryable=True)
        try:
            if handler._reject_closed_execution(state):
                return
            state.execution_count += 1
            result = handler.server.ebsd_engine.execute(request, payload)
            handler._write_json(200, result)
        finally:
            state.execution_lock.release()
    except ContractError as error:
        handler._write_error(error.http_status, error)
    except Exception:
        handler._write_error(500, ContractError(code="INTERNAL_RUNTIME_ERROR", http_status=500,
            safe_message="EBSD inference failed."))
