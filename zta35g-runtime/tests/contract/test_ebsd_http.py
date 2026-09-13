from hashlib import sha256
import http.client
from io import BytesIO
import json
import threading

from PIL import Image
import pytest

from materialsagent_zta35g_runtime.app import create_runtime_server, close_runtime_server
from materialsagent_zta35g_runtime.config import RuntimeSettings
from materialsagent_zta35g_runtime.ebsd import parse_request, TOOL_ID, HEADER
from materialsagent_zta35g_runtime.contracts import ContractError


def picture(mode="RGB"):
    output = BytesIO()
    Image.new(mode, (200, 200)).save(output, "PNG")
    return output.getvalue()


def metadata(payload):
    return {"request_id": "req", "task_id": "task", "tool_run_id": "run", "asset_id": "asset_input",
        "sha256": sha256(payload).hexdigest(), "seed": 1, "tool_id": TOOL_ID, "schema_version": "1.0"}


@pytest.mark.parametrize("mutation", ["hash", "extra", "seed", "duplicate", "gray", "broken", "empty"])
def test_binary_contract_rejects_invalid_input(mutation):
    payload = picture()
    value = metadata(payload)
    if mutation == "hash": value["sha256"] = "0" * 64
    if mutation == "extra": value["url"] = "http://example.invalid/image.png"
    if mutation == "seed": value["seed"] = True
    if mutation == "gray": payload = picture("L"); value = metadata(payload)
    if mutation == "broken": payload = b"broken"; value = metadata(payload)
    if mutation == "empty": payload = b""; value = metadata(payload)
    header = json.dumps(value)
    if mutation == "duplicate": header = header[:-1] + ', "seed": 1}'
    with pytest.raises(ContractError):
        parse_request(header, payload, "application/octet-stream")


def test_http_auth_busy_failure_recovery_and_unconfigured_sem_isolation(tmp_path):
    class Sem:
        model_bundle_id = "zta35g-sem-original-bundle"
        device_kind = "cpu"
        def load(self): pass
        def is_loaded(self): return True
        def close(self): pass
    class Ebsd:
        failed = False
        calls = 0
        def execute(self, request, payload):
            self.calls += 1
            if self.failed: raise ValueError("must not expose internal details")
            return {"status": "SUCCEEDED", "tool_run_id": request["tool_run_id"]}
        def close(self): pass
    server = create_runtime_server(RuntimeSettings(token="test-token", model_root=tmp_path), Sem(), port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    def call(path, body=None, auth=True, extra=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            conn.request("POST" if body is not None else "GET", path, body,
                {**({"X-ZTA35G-Runtime-Token": "test-token"} if auth else {}), **(extra or {})})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally: conn.close()
    try:
        assert call("/internal/v1/ebsd/health/ready", auth=False)[0] == 401
        assert call("/internal/v1/ebsd/health/ready")[0] == 503
        assert call("/internal/v1/health/ready")[0] == 200
        server.ebsd_engine = Ebsd()
        payload = picture()
        headers = {HEADER: json.dumps(metadata(payload)), "Content-Type": "application/octet-stream"}
        with server.runtime_state.execution_lock:
            assert call("/internal/v1/ebsd/execute", payload, extra=headers)[1]["error"]["code"] == "RUNTIME_BUSY"
        server.ebsd_engine.failed = True
        status, error = call("/internal/v1/ebsd/execute", payload, extra=headers)
        assert status == 500 and "internal details" not in json.dumps(error)
        assert not server.runtime_state.execution_lock.locked()
        server.ebsd_engine.failed = False
        assert call("/internal/v1/ebsd/execute", payload, extra=headers)[0] == 200
        assert call("/internal/v1/ebsd/execute", payload, extra={**headers, "Content-Length": str(10 * 1024 * 1024 + 1)})[0] == 413
    finally:
        server.shutdown()
        close_runtime_server(server)
        thread.join(timeout=5)
