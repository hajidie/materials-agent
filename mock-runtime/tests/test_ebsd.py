from hashlib import sha256
from io import BytesIO
import json
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from materialsagent_mock_runtime.main import create_app, MockRuntimeSettings


def picture():
    buffer = BytesIO()
    Image.new("RGB", (200, 200)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("case,expected", [("valid", 200), ("unauthorized", 401), ("hash", 422),
    ("path", 422), ("broken", 422), ("large", 413), ("busy", 503)])
def test_ebsd_binary_http_contract(case, expected):
    app = create_app(MockRuntimeSettings(token="ebsd-contract"))
    payload = picture()
    if case == "broken": payload = b"broken"
    if case == "large": payload = b"x" * (10 * 1024 * 1024 + 1)
    metadata = {"request_id": "req", "task_id": "task", "tool_run_id": "run", "asset_id": "asset_input",
        "sha256": sha256(payload).hexdigest(), "seed": 1, "tool_id": "ebsd_yield_strength_predictor", "schema_version": "1.0"}
    if case == "hash": metadata["sha256"] = "0" * 64
    if case == "path": metadata["path"] = "untrusted.png"
    headers = {"X-ZTA35G-Runtime-Token": "ebsd-contract", "X-EBSD-Request": json.dumps(metadata),
        "Content-Type": "application/octet-stream"}
    if case == "unauthorized": del headers["X-ZTA35G-Runtime-Token"]
    with TestClient(app) as client:
        if case == "busy": app.state.runtime_state.execution_lock.acquire()
        try:
            result = client.post("/internal/v1/ebsd/execute", content=payload, headers=headers)
            assert result.status_code == expected
            if case == "valid":
                assert result.json()["data"] == {"yield_strength": {"value": 400, "unit": "MPa"}}
                assert result.json()["images"] == []
                assert result.json()["model_bundle_id"] == "mock-ebsd-bundle"
            else:
                assert app.state.runtime_state.execution_count == 0
        finally:
            if case == "busy": app.state.runtime_state.execution_lock.release()
