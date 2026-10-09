from hashlib import sha256
import http.client
from io import BytesIO
import json
import struct
import threading
import zlib
import pytest
from PIL import Image
from materialsagent_zta35g_runtime.app import create_runtime_server, close_runtime_server
from materialsagent_zta35g_runtime.config import RuntimeSettings
from materialsagent_zta35g_runtime.tc4 import ReceiptStore, parse_request, decode_image, png, TOOL_ID, HEADER
from materialsagent_zta35g_runtime.contracts import ContractError


def picture(mode="L", size=(128, 160), fmt="PNG", color=None):
    output = BytesIO()
    Image.new(mode, size, color=color).save(output, fmt)
    return output.getvalue()


def rgb16_bytes():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack("!IIBBBBB", 128, 160, 16, 2, 0, 0, 0)
    row = b"\x00" + struct.pack("!HHH", 12345, 23456, 34567) * 128
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(row * 160)) + chunk(b"IEND", b""))


def metadata(payload):
    return {"request_id": "req", "conversation_id": "conv", "task_id": "task", "tool_run_id": "run",
            "item_id": "item", "asset_id": "asset_input", "sha256": sha256(payload).hexdigest()}


class Engine:
    calls = 0
    crash = False
    def execute(self, payload):
        self.calls += 1
        if self.crash:
            raise KeyboardInterrupt()
        image = Image.open(BytesIO(payload))
        total = image.width * image.height
        return {"width": image.width, "height": image.height, "foreground_pixels": 0, "total_pixels": total,
                "area_fraction": 0.0}, {"overlay": png(image.convert("RGB")), "mask": png(image.convert("L"))}
    def close(self):
        pass


@pytest.mark.parametrize("mutation", ["hash", "extra", "duplicate", "rgba", "tiff", "small", "broken"])
def test_tc4_contract_rejects_unsupported_input(mutation):
    payload = picture()
    if mutation == "rgba": payload = picture("RGBA")
    if mutation == "tiff": payload = picture(fmt="TIFF")
    if mutation == "small": payload = picture(size=(127, 160))
    if mutation == "broken": payload = b"broken"
    value = metadata(payload)
    if mutation == "hash": value["sha256"] = "0" * 64
    if mutation == "extra": value["path"] = "private"
    header = json.dumps(value)
    if mutation == "duplicate": header = header[:-1] + ', "request_id":"req"}'
    with pytest.raises(ContractError):
        parse_request(header, payload, "application/octet-stream")


def test_receipt_replay_restart_unknown_and_cleanup(tmp_path):
    payload = picture()
    request = metadata(payload)
    engine = Engine()
    store = ReceiptStore(tmp_path)
    receipt = store.execute(engine, request, payload)
    assert ReceiptStore(tmp_path).execute(engine, request, payload) == receipt
    assert engine.calls == 1
    with pytest.raises(ContractError):
        store.execute(engine, dict(request, asset_id="other"), payload)
    assert store.cleanup(request) == {"status": "DELETED"}
    assert not (tmp_path / "req" / "mask.png").exists()
    store.execute(engine, request, payload)
    assert engine.calls == 1
    unknown = dict(request, request_id="crashed")
    engine.crash = True
    with pytest.raises(KeyboardInterrupt):
        store.execute(engine, unknown, payload)
    engine.crash = False
    assert ReceiptStore(tmp_path).execute(engine, unknown, payload)["status"] == "OUTCOME_UNKNOWN"
    assert engine.calls == 2
    with pytest.raises(ContractError):
        store.cleanup(unknown)


def test_opaque_rgba_conversion_preserves_rgb_pixels_and_request_digest():
    payload = picture("RGBA", color=(17, 83, 201, 255))
    decoded = decode_image(payload)
    expected = Image.new("RGB", decoded.size, (17, 83, 201))
    assert decoded.mode == "RGB" and decoded.tobytes() == expected.tobytes()
    request = metadata(payload)
    assert parse_request(json.dumps(request), payload, "application/octet-stream") == request


@pytest.mark.parametrize("alpha", [0, 254])
def test_tc4_rejects_even_one_transparent_pixel(alpha):
    image = Image.new("RGBA", (128, 160), (17, 83, 201, 255))
    image.putpixel((127, 159), (17, 83, 201, alpha))
    with pytest.raises(ContractError):
        payload = png(image)
        parse_request(json.dumps(metadata(payload)), payload, "application/octet-stream")


@pytest.mark.parametrize("mode,background,transparent", [("RGB", (17, 83, 201), (0, 0, 0)), ("L", 83, 0)])
@pytest.mark.parametrize("has_transparent_pixel", [False, True])
def test_tc4_checks_actual_trns_pixels(mode, background, transparent, has_transparent_pixel):
    image = Image.new(mode, (128, 160), background)
    if has_transparent_pixel:
        image.putpixel((127, 159), transparent)
    stream = BytesIO()
    image.save(stream, "PNG", transparency=transparent)
    payload = stream.getvalue()
    if has_transparent_pixel:
        with pytest.raises(ContractError):
            parse_request(json.dumps(metadata(payload)), payload, "application/octet-stream")
    else:
        assert parse_request(json.dumps(metadata(payload)), payload, "application/octet-stream") == metadata(payload)
        assert decode_image(payload).tobytes() == image.convert("RGB").tobytes()


def test_tc4_rejects_encoded_16_bit_rgb():
    payload = rgb16_bytes()
    with Image.open(BytesIO(payload)) as image:
        assert image.mode == "RGB"
    with pytest.raises(ContractError):
        parse_request(json.dumps(metadata(payload)), payload, "application/octet-stream")


@pytest.mark.parametrize("mode,color", [("L", None), ("RGBA", (17, 83, 201, 255))])
def test_tc4_http_auth_shared_gpu_lock_and_durable_artifacts(tmp_path, mode, color):
    class Sem:
        model_bundle_id = "zta35g-sem-original-bundle"
        device_kind = "cpu"
        def load(self): pass
        def is_loaded(self): return True
        def close(self): pass
    server = create_runtime_server(RuntimeSettings(token="test", model_root=tmp_path), Sem(), port=0)
    server.tc4_engine, server.tc4_receipts = Engine(), ReceiptStore(tmp_path / "receipts")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    def call(path, body=None, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            conn.request("POST" if body is not None else "GET", path, body,
                {**({"X-ZTA35G-Runtime-Token": "test"} if auth else {}), **(headers or {})})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()
    try:
        assert call("/internal/v1/tc4/health/ready", auth=False)[0] == 401
        payload = picture(mode, color=color)
        request = metadata(payload)
        headers = {HEADER: json.dumps(request), "Content-Type": "application/octet-stream"}
        with server.runtime_state.execution_lock:
            status, result = call("/internal/v1/tc4/execute", payload, headers=headers)
            assert status == 503 and json.loads(result)["error"]["code"] == "RUNTIME_BUSY"
        assert call("/internal/v1/tc4/execute", payload, headers=headers)[0] == 200
        assert call("/internal/v1/tc4/execute", payload, headers=headers)[0] == 200
        assert server.runtime_state.execution_count == 1
        status, receipt = call("/internal/v1/tc4/receipts/req")
        receipt = json.loads(receipt)
        assert status == 200 and receipt["tool_id"] == TOOL_ID
        for role in ("mask", "overlay"):
            status, content = call("/internal/v1/tc4/receipts/req/" + role)
            assert status == 200 and sha256(content).hexdigest() == receipt["artifacts"][role]["sha256"]
        assert call("/internal/v1/tc4/cleanup", json.dumps(request).encode())[0] == 200
        assert call("/internal/v1/tc4/receipts/req/mask")[0] == 404
    finally:
        server.shutdown()
        close_runtime_server(server)
        thread.join(timeout=5)
