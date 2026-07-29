import http.client
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

import numpy as np
import pytest

from materialsagent_zta35g_runtime.app import (
    close_runtime_server,
    create_runtime_server,
)
from materialsagent_zta35g_runtime.config import RuntimeSettings
from materialsagent_zta35g_runtime.inference import (
    InferenceResult,
    MechanicalPredictionError,
    SemGenerationError,
    ZTA35GInferenceEngine,
)


TOKEN = "fake-http-runtime-token"
AUTH = {"X-ZTA35G-Runtime-Token": TOKEN}


def _payload(outputs=None, **overrides):
    value = {
        "runtime_contract_version": "1.0",
        "request_id": "req_http",
        "task_id": "task_http",
        "tool_run_id": "trun_http",
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_version": "1.0",
        "process_parameters": {
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        "requested_outputs": outputs
        or ["sem_image", "mechanical_properties"],
        "runtime_parameters": {
            "seed": 12345,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }
    value.update(overrides)
    return value


class _Components:
    model_bundle_id = "zta35g-sem-original-bundle"
    device_kind = "cpu"

    def __init__(self):
        self.load_count = 0
        self.generate_count = 0
        self.predict_count = 0
        self.close_count = 0
        self.load_error = None
        self.generate_error = None
        self.predict_error = None
        self.entered = None
        self.release = None
        self.image = np.zeros((512, 512), dtype="<f4")

    def load(self):
        self.load_count += 1
        if self.load_error is not None:
            raise self.load_error

    def generate_sem(self, *_args):
        self.generate_count += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(timeout=5)
        if self.generate_error is not None:
            raise self.generate_error
        return self.image

    def predict_mechanical(self, *_args):
        self.predict_count += 1
        if self.predict_error is not None:
            raise self.predict_error
        return 650.0, 3.2

    def close(self):
        self.close_count += 1


class _OversizedEngine:
    model_bundle_id = "zta35g-sem-original-bundle"
    device_kind = "cpu"

    def __init__(self):
        self.load_count = 0
        self.closed = False

    def load(self):
        self.load_count += 1

    def is_loaded(self):
        return True

    def execute(self, _process, requested, _runtime):
        return InferenceResult(
            status="SUCCEEDED",
            completed_outputs=tuple(requested),
            failed_outputs=(),
            data={},
            images=({"data_base64": "x" * (4 * 1024 * 1024)},),
            warnings=(),
            diagnostics=(),
            model_bundle_id=self.model_bundle_id,
            error=None,
        )

    def close(self):
        self.closed = True


class _StartupInvariantEngine:
    def __init__(self, model_bundle_id, device_kind, loaded):
        self.model_bundle_id = model_bundle_id
        self.device_kind = device_kind
        self.loaded = loaded
        self.load_count = 0
        self.close_count = 0

    def load(self):
        self.load_count += 1

    def is_loaded(self):
        return self.loaded

    def close(self):
        self.close_count += 1
        self.loaded = False


class _RunningServer:
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

    @property
    def port(self):
        return self.server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        payload = body
        if isinstance(body, dict):
            payload = json.dumps(body, allow_nan=True).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        try:
            connection.request(
                method,
                path,
                body=payload,
                headers=request_headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            decoded = (
                json.loads(response_body.decode("utf-8"))
                if response_body
                else None
            )
            return response.status, dict(response.headers), decoded, response_body
        finally:
            connection.close()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        close_runtime_server(self.server)
        assert not self.thread.is_alive()


def _start(tmp_path, components=None, engine=None):
    resolved_components = components or _Components()
    resolved_engine = engine or ZTA35GInferenceEngine(resolved_components)
    return _RunningServer(tmp_path, resolved_engine), resolved_components


def test_server_is_loopback_only_and_loads_engine_once_for_all_requests(tmp_path):
    running, components = _start(tmp_path)
    try:
        assert running.server.server_address[0] == "127.0.0.1"
        for _index in range(3):
            status, _headers, body, _raw = running.request(
                "GET",
                "/internal/v1/health/ready",
                headers=AUTH,
            )
            assert status == 200
            assert body["status"] == "READY"
            assert body["model_files"]["status"] == "AVAILABLE"
            assert body["model_loaded"] is True
            assert body["device"] == {
                "status": "AVAILABLE",
                "kind": "cpu",
            }
            assert body["can_accept_execution"] is True
            assert body["busy"] is False
            assert (
                body["model_bundle_id"]
                == "zta35g-sem-original-bundle"
            )
    finally:
        running.close()

    assert components.load_count == 1
    assert components.close_count == 1


@pytest.mark.parametrize("device_kind", ["cpu", "cuda"])
def test_ready_requires_fixed_bundle_and_known_loaded_device(
    tmp_path,
    device_kind,
):
    engine = _StartupInvariantEngine(
        "zta35g-sem-original-bundle",
        device_kind,
        True,
    )
    running, _components = _start(tmp_path, engine=engine)
    try:
        ready = running.request(
            "GET",
            "/internal/v1/health/ready",
            headers=AUTH,
        )
    finally:
        running.close()

    assert ready[0] == 200
    assert ready[2]["model_loaded"] is True
    assert ready[2]["device"]["kind"] == device_kind
    assert ready[2]["model_bundle_id"] == "zta35g-sem-original-bundle"
    assert engine.load_count == 1
    assert engine.close_count == 1


@pytest.mark.parametrize(
    ("model_bundle_id", "device_kind", "loaded"),
    [
        ("private-wrong-bundle", "cpu", True),
        ("zta35g-sem-original-bundle", "unknown", True),
        ("zta35g-sem-original-bundle", "cpu", False),
    ],
    ids=("wrong-bundle", "unknown-device", "not-loaded"),
)
def test_invalid_post_load_identity_fails_ready_and_closes_engine_once(
    tmp_path,
    caplog,
    model_bundle_id,
    device_kind,
    loaded,
):
    engine = _StartupInvariantEngine(
        model_bundle_id,
        device_kind,
        loaded,
    )
    with caplog.at_level(
        logging.INFO,
        logger="materialsagent_zta35g_runtime",
    ):
        running, _components = _start(tmp_path, engine=engine)
        try:
            ready = running.request(
                "GET",
                "/internal/v1/health/ready",
                headers=AUTH,
            )
            assert engine.close_count == 1
        finally:
            running.close()

    assert ready[0] == 503
    assert ready[2]["status"] == "NOT_READY"
    assert ready[2]["model_loaded"] is False
    assert ready[2]["model_bundle_id"] is None
    assert ready[2]["error"]["code"] == "MODEL_LOAD_FAILED"
    assert engine.close_count == 1
    assert "private-wrong-bundle" not in caplog.text


def test_execute_rejects_changed_bundle_identity_without_logging_it(
    tmp_path,
    caplog,
):
    running, components = _start(tmp_path)
    components.model_bundle_id = "private-runtime-bundle-canary"
    try:
        with caplog.at_level(
            logging.INFO,
            logger="materialsagent_zta35g_runtime",
        ):
            response = running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(),
                headers=AUTH,
            )
    finally:
        running.close()

    assert response[0] == 500
    assert response[2]["error"]["code"] == "INTERNAL_RUNTIME_ERROR"
    assert "private-runtime-bundle-canary" not in caplog.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/internal/v1/health/live", None),
        ("GET", "/internal/v1/health/ready", None),
        ("POST", "/internal/v1/execute", _payload()),
    ],
)
def test_every_internal_path_requires_exact_token(
    tmp_path, method, path, body
):
    running, _components = _start(tmp_path)
    try:
        for headers in (
            {},
            {"X-ZTA35G-Runtime-Token": "wrong"},
        ):
            status, _response_headers, response, raw = running.request(
                method, path, body=body, headers=headers
            )
            assert status == 401
            assert response["error"]["code"] == "INVALID_RUNTIME_REQUEST"
            assert TOKEN.encode("utf-8") not in raw
    finally:
        running.close()


def test_live_unknown_path_and_wrong_method_are_bounded(tmp_path):
    running, _components = _start(tmp_path)
    try:
        live = running.request(
            "GET", "/internal/v1/health/live", headers=AUTH
        )
        missing = running.request("GET", "/unknown", headers=AUTH)
        wrong_method = running.request(
            "PUT", "/internal/v1/execute", headers=AUTH
        )
    finally:
        running.close()

    assert live[0] == 200
    assert live[2]["status"] == "LIVE"
    assert missing[0] == 404
    assert wrong_method[0] == 405


def test_execute_rejects_content_type_size_json_and_schema_errors(tmp_path):
    running, components = _start(tmp_path)
    try:
        wrong_type = running.request(
            "POST",
            "/internal/v1/execute",
            body=b"{}",
            headers={**AUTH, "Content-Type": "text/plain"},
        )
        oversized = running.request(
            "POST",
            "/internal/v1/execute",
            body=b"x" * ((64 * 1024) + 1),
            headers={**AUTH, "Content-Type": "application/json"},
        )
        invalid_json = running.request(
            "POST",
            "/internal/v1/execute",
            body=b"{",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        schema = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(schema_version="9.9"),
            headers=AUTH,
        )
        tool = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(tool_id="other"),
            headers=AUTH,
        )
        tool_version = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(tool_version="9.9.9"),
            headers=AUTH,
        )
    finally:
        running.close()

    assert wrong_type[0] == 415
    assert oversized[0] == 413
    assert invalid_json[0] == 400
    assert schema[0] == 409
    assert schema[2]["error"]["code"] == "SCHEMA_VERSION_MISMATCH"
    assert tool[2]["error"]["code"] == "UNSUPPORTED_TOOL"
    assert tool_version[2]["error"]["code"] == "TOOL_VERSION_MISMATCH"
    assert components.generate_count == 0


def test_model_load_failure_keeps_live_but_blocks_ready_and_execute(
    tmp_path,
    caplog,
):
    components = _Components()
    components.load_error = RuntimeError(
        "secret traceback C:\\private\\weights"
    )
    with caplog.at_level(
        logging.INFO,
        logger="materialsagent_zta35g_runtime",
    ):
        running, _components = _start(
            tmp_path, components=components
        )
        try:
            live = running.request(
                "GET", "/internal/v1/health/live", headers=AUTH
            )
            ready = running.request(
                "GET", "/internal/v1/health/ready", headers=AUTH
            )
            execute = running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(),
                headers=AUTH,
            )
            assert components.close_count == 1
        finally:
            running.close()

    assert live[0] == 200
    assert ready[0] == 503
    assert ready[2]["status"] == "NOT_READY"
    assert ready[2]["model_files"]["status"] == "INVALID"
    assert ready[2]["model_loaded"] is False
    assert ready[2]["device"] == {
        "status": "UNAVAILABLE",
        "kind": "unknown",
    }
    assert ready[2]["can_accept_execution"] is False
    assert ready[2]["busy"] is False
    assert ready[2]["model_bundle_id"] is None
    assert ready[2]["error"]["code"] == "MODEL_LOAD_FAILED"
    assert execute[0] == 503
    assert execute[2]["error"]["code"] == "MODEL_LOAD_FAILED"
    public = json.dumps([live[2], ready[2], execute[2]])
    assert "secret" not in public
    assert "private" not in public
    assert "traceback" not in public.lower()
    assert components.generate_count == 0
    assert components.load_count == 1
    assert components.close_count == 1
    assert "model_load_failed" in caplog.text
    assert "secret traceback" not in caplog.text
    assert "C:\\private\\weights" not in caplog.text


@pytest.mark.parametrize(
    "outputs",
    [
        ["sem_image"],
        ["mechanical_properties"],
        ["sem_image", "mechanical_properties"],
    ],
)
def test_three_success_modes_echo_ids_and_runtime_parameters(tmp_path, outputs):
    running, components = _start(tmp_path)
    try:
        response = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(outputs),
            headers=AUTH,
        )
    finally:
        running.close()

    assert response[0] == 200
    body = response[2]
    assert body["request_id"] == "req_http"
    assert body["task_id"] == "task_http"
    assert body["tool_run_id"] == "trun_http"
    assert body["requested_outputs"] == outputs
    assert body["completed_outputs"] == outputs
    assert body["actual_runtime_parameters"] == _payload()[
        "runtime_parameters"
    ]
    assert body["model_bundle_id"] == "zta35g-sem-original-bundle"
    assert components.generate_count == 1


def test_partial_success_and_sem_failure_are_contract_responses(tmp_path):
    partial_components = _Components()
    partial_components.predict_error = MechanicalPredictionError()
    partial, _unused = _start(tmp_path, components=partial_components)
    try:
        partial_response = partial.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(),
            headers=AUTH,
        )
    finally:
        partial.close()

    sem_components = _Components()
    sem_components.generate_error = SemGenerationError()
    failed, _unused = _start(tmp_path, components=sem_components)
    try:
        failed_response = failed.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(),
            headers=AUTH,
        )
    finally:
        failed.close()

    assert partial_response[0] == 200
    assert partial_response[2]["status"] == "PARTIALLY_SUCCEEDED"
    assert partial_response[2]["completed_outputs"] == ["sem_image"]
    assert partial_response[2]["failed_outputs"] == [
        "mechanical_properties"
    ]
    assert failed_response[0] == 200
    assert failed_response[2]["status"] == "FAILED"
    assert failed_response[2]["images"] == []
    assert sem_components.predict_count == 0


def test_response_over_limit_returns_small_safe_error(tmp_path):
    engine = _OversizedEngine()
    running, _components = _start(tmp_path, engine=engine)
    try:
        response = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(["sem_image"]),
            headers=AUTH,
        )
    finally:
        running.close()

    assert response[0] == 500
    assert response[2]["error"]["code"] == "INTERNAL_RUNTIME_ERROR"
    assert len(response[3]) < 4 * 1024
    assert engine.load_count == 1
    assert engine.closed is True


def test_second_execute_is_immediately_busy_and_ready_is_degraded_semantic(
    tmp_path,
    caplog,
):
    components = _Components()
    components.entered = threading.Event()
    components.release = threading.Event()
    first_result = []

    with caplog.at_level(
        logging.INFO,
        logger="materialsagent_zta35g_runtime",
    ):
        running, _unused = _start(tmp_path, components=components)

        def execute_first():
            first_result.append(
                running.request(
                    "POST",
                    "/internal/v1/execute",
                    body=_payload(),
                    headers=AUTH,
                )
            )

        thread = threading.Thread(target=execute_first)
        thread.start()
        assert components.entered.wait(timeout=3)
        try:
            ready = running.request(
                "GET", "/internal/v1/health/ready", headers=AUTH
            )
            busy = running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(
                    request_id="req_busy",
                    task_id="task_busy",
                    tool_run_id="trun_busy",
                ),
                headers=AUTH,
            )
        finally:
            components.release.set()
            thread.join(timeout=5)
            running.close()

    assert not thread.is_alive()
    assert ready[0] == 200
    assert ready[2]["status"] == "READY"
    assert ready[2]["busy"] is True
    assert ready[2]["can_accept_execution"] is False
    assert ready[2]["device"]["kind"] == "cpu"
    assert busy[0] == 503
    assert busy[2]["error"]["code"] == "RUNTIME_BUSY"
    assert busy[2]["error"]["retryable"] is True
    assert first_result[0][0] == 200
    assert components.generate_count == 1
    assert "runtime_busy" in caplog.text
    assert TOKEN not in caplog.text


def test_close_waits_for_active_execute_before_closing_engine(tmp_path):
    components = _Components()
    components.entered = threading.Event()
    components.release = threading.Event()
    running, _unused = _start(tmp_path, components=components)
    response = []
    request_thread = threading.Thread(
        target=lambda: response.append(
            running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(),
                headers=AUTH,
            )
        )
    )
    close_completed = threading.Event()
    close_errors = []

    def close_after_serve_loop():
        try:
            close_runtime_server(running.server)
        except BaseException as error:
            close_errors.append(error)
        else:
            close_completed.set()

    close_thread = threading.Thread(target=close_after_serve_loop)
    request_thread.start()
    assert components.entered.wait(timeout=3)
    running.server.shutdown()
    running.thread.join(timeout=5)
    assert not running.thread.is_alive()
    close_thread.start()
    try:
        for _index in range(300):
            if running.server.runtime_state.closed:
                break
            close_completed.wait(timeout=0.01)

        assert running.server.runtime_state.closed is True
        assert close_completed.is_set() is False
        assert close_thread.is_alive()
        assert components.close_count == 0
        assert running.server.engine.is_loaded() is True
        assert request_thread.is_alive()
    finally:
        components.release.set()
        request_thread.join(timeout=5)
        close_thread.join(timeout=5)

    assert request_thread.is_alive() is False
    assert close_thread.is_alive() is False
    assert close_errors == []
    assert close_completed.is_set() is True
    assert response[0][0] == 200
    assert response[0][2]["status"] == "SUCCEEDED"
    assert components.close_count == 1
    close_runtime_server(running.server)
    assert components.close_count == 1


def test_closed_runtime_rejects_execute_without_starting_model(tmp_path):
    running, components = _start(tmp_path)
    state = running.server.runtime_state
    state.closed = True
    execution_count = state.execution_count
    try:
        response = running.request(
            "POST",
            "/internal/v1/execute",
            body=_payload(),
            headers=AUTH,
        )
    finally:
        state.closed = False
        running.close()

    assert response[0] == 503
    assert response[2]["error"]["code"] == "RUNTIME_NOT_READY"
    assert state.execution_count == execution_count
    assert components.generate_count == 0


def test_normal_execution_emits_only_safe_structured_lifecycle_events(
    tmp_path,
    caplog,
):
    with caplog.at_level(
        logging.INFO,
        logger="materialsagent_zta35g_runtime",
    ):
        running, _components = _start(tmp_path)
        try:
            response = running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(),
                headers=AUTH,
            )
        finally:
            running.close()

    events = []
    for record in caplog.records:
        if record.name == "materialsagent_zta35g_runtime":
            events.append(json.loads(record.getMessage()))
    names = [event["event"] for event in events]
    assert {
        "runtime_started",
        "model_loading",
        "model_loaded",
        "execution_received",
        "sem_generation",
        "mechanical_property_prediction",
        "execution_completed",
        "runtime_stopping",
    }.issubset(names)
    allowed_fields = {
        "timestamp",
        "event",
        "request_id",
        "task_id",
        "tool_run_id",
        "tool_id",
        "tool_version",
        "schema_version",
        "step",
        "status",
        "duration_ms",
        "error_code",
        "model_bundle_id",
        "device",
    }
    assert all(set(event).issubset(allowed_fields) for event in events)
    log_text = caplog.text
    assert TOKEN not in log_text
    assert response[2]["images"][0]["data_base64"] not in log_text
    assert "data_base64" not in log_text


def test_runtime_entrypoint_enables_info_json_logging_in_clean_python():
    source_root = (
        Path(__file__).resolve().parents[2] / "src"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(source_root),
            environment.get("PYTHONPATH", ""),
        )
    )
    probe = (
        "from materialsagent_zta35g_runtime.app import _log_event\n"
        "from materialsagent_zta35g_runtime.main import "
        "configure_runtime_logging\n"
        "configure_runtime_logging()\n"
        "configure_runtime_logging()\n"
        "_log_event('runtime_started', status='LIVE')\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.stdout == ""
    payload = json.loads(completed.stderr)
    assert payload["event"] == "runtime_started"
    assert payload["status"] == "LIVE"
    assert set(payload) == {"timestamp", "event", "status"}


def test_failed_execution_emits_safe_execution_failed_event(
    tmp_path,
    caplog,
):
    components = _Components()
    components.generate_error = RuntimeError(
        "private failure C:\\private\\weight"
    )
    with caplog.at_level(
        logging.INFO,
        logger="materialsagent_zta35g_runtime",
    ):
        running, _unused = _start(tmp_path, components=components)
        try:
            response = running.request(
                "POST",
                "/internal/v1/execute",
                body=_payload(),
                headers=AUTH,
            )
        finally:
            running.close()

    assert response[2]["status"] == "FAILED"
    assert "execution_failed" in caplog.text
    assert "SEM_GENERATION_FAILED" in caplog.text
    assert "private failure" not in caplog.text
    assert "C:\\private\\weight" not in caplog.text
