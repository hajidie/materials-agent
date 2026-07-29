from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

import numpy as np
import pytest

RUNTIME_SRC = (
    Path(__file__).resolve().parents[3] / "zta35g-runtime" / "src"
)
sys.path.insert(0, str(RUNTIME_SRC))

from materialsagent_zta35g_runtime.app import (
    close_runtime_server,
    create_runtime_server,
)
from materialsagent_zta35g_runtime.config import RuntimeSettings
from materialsagent_zta35g_runtime.inference import (
    ZTA35GInferenceEngine,
)
from materialsagent.application.image_payload import decode_image_payload
from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.ports.tool_execution import (
    ToolClientProtocolError,
    ToolClientRuntimeError,
    ToolRequestContext,
)
from materialsagent.infrastructure.tool_clients.local_zta35g import (
    LocalZTA35GToolClientAdapter,
)


TOKEN = "fake-real-runtime-adapter-token"


class _Components:
    model_bundle_id = "zta35g-sem-original-bundle"
    device_kind = "cpu"

    def __init__(self):
        self.load_count = 0
        self.generate_count = 0
        self.predict_count = 0
        self.load_error = None
        self.entered = None
        self.release = None
        self.image = np.linspace(
            -1.0, 1.0, 512 * 512, dtype="<f4"
        ).reshape((512, 512))

    def load(self):
        self.load_count += 1
        if self.load_error is not None:
            raise self.load_error

    def generate_sem(self, *_args):
        self.generate_count += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(timeout=5)
        return self.image

    def predict_mechanical(self, *_args):
        self.predict_count += 1
        return 650.0, 3.2

    def close(self):
        return


class _RawRuntime:
    def __init__(self, response_body):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                content_length = int(
                    self.headers.get("Content-Length", "0")
                )
                self.rfile.read(content_length)
                owner.execution_count += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Length", str(len(owner.response_body))
                )
                self.end_headers()
                self.wfile.write(owner.response_body)

        self.response_body = response_body
        self.execution_count = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.base_url = "http://127.0.0.1:%s" % (
            self.server.server_address[1],
        )

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        assert not self.thread.is_alive()


class _Runtime:
    def __init__(self, tmp_path, engine):
        settings = RuntimeSettings(
            token=TOKEN,
            port=8100,
            model_root=tmp_path,
        )
        self.server = create_runtime_server(settings, engine, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.base_url = "http://127.0.0.1:%s" % (
            self.server.server_address[1],
        )

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        close_runtime_server(self.server)
        assert not self.thread.is_alive()


def _registered(runtime):
    adapter = LocalZTA35GToolClientAdapter(
        base_url=runtime.base_url,
        token=TOKEN,
        timeout_seconds=2.0,
    )
    return build_tool_registry(adapter).resolve(
        "zta35g_sem_virtual_lab"
    )


def _input(outputs):
    return {
        "material": "ZTA35G",
        "solution_temperature": {"value": 1000, "unit": "°C"},
        "solution_time": {"value": 3.0, "unit": "h"},
        "aging_temperature": {"value": 730, "unit": "°C"},
        "aging_time": {"value": 3.0, "unit": "h"},
        "requested_outputs": list(outputs),
    }


def _context(suffix="normal"):
    return ToolRequestContext(
        request_id="req_%s" % suffix,
        conversation_id="conv_%s" % suffix,
        task_id="task_%s" % suffix,
        tool_run_id="trun_%s" % suffix,
        actor_id="actor_adapter",
        user_id=None,
        requested_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("outputs", "role", "requested"),
    [
        (("sem_image",), "generated_sem", True),
        (
            ("mechanical_properties",),
            "intermediate_sem",
            False,
        ),
        (
            ("sem_image", "mechanical_properties"),
            "generated_sem",
            True,
        ),
    ],
)
def test_existing_adapter_accepts_three_real_runtime_contract_modes(
    tmp_path, outputs, role, requested
):
    components = _Components()
    runtime = _Runtime(
        tmp_path, ZTA35GInferenceEngine(components)
    )
    try:
        registered = _registered(runtime)
        assert registered.tool.health_check() == "AVAILABLE"
        validated = registered.tool.validate_input(
            _input(outputs), seed=12345
        )

        output = registered.tool.execute(
            validated, _context(outputs[0])
        )
    finally:
        runtime.close()

    assert output.requested_outputs == outputs
    assert output.completed_outputs == outputs
    assert output.actual_runtime_parameters == {
        "seed": 12345,
        "num_samples": 1,
        "guide_scale": 2.0,
        "timesteps": 1000,
    }
    assert output.model_bundle_id == "zta35g-sem-original-bundle"
    assert output.images[0].image_role == role
    assert output.images[0].requested_output is requested
    decoded = decode_image_payload(output.images[0])
    assert decoded.array.shape == (512, 512)
    assert components.load_count == 1
    assert components.generate_count == 1
    assert runtime.server.runtime_state.execution_count == 1


def test_existing_adapter_maps_runtime_busy_without_retry_and_ready_degrades(
    tmp_path,
):
    components = _Components()
    components.entered = threading.Event()
    components.release = threading.Event()
    runtime = _Runtime(
        tmp_path, ZTA35GInferenceEngine(components)
    )
    registered = _registered(runtime)
    first_result = []

    def execute_first():
        validated = registered.tool.validate_input(
            _input(("sem_image",)), seed=11
        )
        first_result.append(
            registered.tool.execute(validated, _context("first"))
        )

    thread = threading.Thread(target=execute_first)
    thread.start()
    assert components.entered.wait(timeout=3)
    try:
        assert registered.tool.health_check() == "DEGRADED"
        validated = registered.tool.validate_input(
            _input(("sem_image",)), seed=22
        )
        with pytest.raises(ToolClientRuntimeError) as raised:
            registered.tool.execute(validated, _context("busy"))
    finally:
        components.release.set()
        thread.join(timeout=5)
        runtime.close()

    assert not thread.is_alive()
    assert raised.value.code == "RUNTIME_BUSY"
    assert raised.value.retryable is True
    assert len(first_result) == 1
    assert components.generate_count == 1
    assert runtime.server.runtime_state.execution_count == 1


def test_existing_adapter_maps_model_load_failure_safely(tmp_path):
    components = _Components()
    components.load_error = RuntimeError(
        "secret traceback C:\\private\\weight"
    )
    runtime = _Runtime(
        tmp_path, ZTA35GInferenceEngine(components)
    )
    try:
        registered = _registered(runtime)
        assert registered.tool.health_check() == "UNAVAILABLE"
        validated = registered.tool.validate_input(
            _input(("sem_image",)), seed=33
        )
        with pytest.raises(ToolClientRuntimeError) as raised:
            registered.tool.execute(validated, _context("load"))
    finally:
        runtime.close()

    assert raised.value.code == "MODEL_LOAD_FAILED"
    assert "secret" not in str(raised.value)
    assert runtime.server.runtime_state.execution_count == 0


def test_existing_adapter_rejects_malformed_real_runtime_protocol(tmp_path):
    runtime = _RawRuntime(b'{"runtime_contract_version":"1.0"}')
    try:
        registered = _registered(runtime)
        validated = registered.tool.validate_input(
            _input(("sem_image",)), seed=44
        )
        with pytest.raises(ToolClientProtocolError):
            registered.tool.execute(validated, _context("malformed"))
    finally:
        runtime.close()

    assert runtime.execution_count == 1


def test_existing_adapter_rejects_response_larger_than_four_mib(
    tmp_path,
):
    runtime = _RawRuntime(
        b"{" + (b"x" * (4 * 1024 * 1024))
    )
    try:
        registered = _registered(runtime)
        validated = registered.tool.validate_input(
            _input(("sem_image",)), seed=55
        )
        with pytest.raises(ToolClientProtocolError):
            registered.tool.execute(validated, _context("oversized"))
    finally:
        runtime.close()

    assert runtime.execution_count == 1
