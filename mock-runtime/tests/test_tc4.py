from hashlib import sha256
from io import BytesIO
import json
import struct
import zlib

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from materialsagent_mock_runtime.main import create_app, MockRuntimeSettings


def picture(case):
    if case == "rgb16":
        def chunk(kind, data):
            return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
        header = struct.pack("!IIBBBBB", 128, 160, 16, 2, 0, 0, 0)
        row = b"\x00" + struct.pack("!HHH", 12345, 23456, 34567) * 128
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(row * 160)) + chunk(b"IEND", b""))
    gray = case.startswith("gray")
    image = Image.new("L" if gray else "RGB", (128, 160), 83 if gray else (17, 83, 201))
    transparent = 0 if gray else (0, 0, 0)
    if case.endswith("transparent"):
        image.putpixel((127, 159), transparent)
    output = BytesIO()
    image.save(output, "PNG", transparency=transparent)
    return output.getvalue()


@pytest.mark.parametrize("case,expected", [("rgb-transparent", 422), ("gray-transparent", 422),
    ("rgb16", 422), ("rgb-opaque", 200), ("gray-opaque", 200)])
def test_tc4_validates_encoded_bit_depth_and_actual_transparency(case, expected):
    payload = picture(case)
    request = {"request_id": "req", "conversation_id": "conv", "task_id": "task", "tool_run_id": "run",
        "item_id": "item", "asset_id": "asset_input", "sha256": sha256(payload).hexdigest()}
    app = create_app(MockRuntimeSettings(token="tc4-contract"))
    with TestClient(app) as client:
        response = client.post("/internal/v1/tc4/execute", content=payload, headers={
            "X-ZTA35G-Runtime-Token": "tc4-contract", "X-TC4-Request": json.dumps(request),
            "Content-Type": "application/octet-stream"})
        assert response.status_code == expected, response.text
        state = app.state.runtime_state
        assert state.execution_count == (1 if expected == 200 else 0)
        if expected != 200:
            assert not state.tc4_receipts and not state.tc4_artifacts
