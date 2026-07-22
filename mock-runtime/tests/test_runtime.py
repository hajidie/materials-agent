from __future__ import annotations

import ast
import base64
import json
import struct
import threading

from fastapi.testclient import TestClient
import pytest

import materialsagent_mock_runtime.main as runtime_main
from materialsagent_mock_runtime.main import (
    DEFAULT_HOST,
    MockRuntimeSettings,
    create_app,
)


TOKEN = "unit-test-runtime-token"
AUTH_HEADERS = {"X-ZTA35G-Runtime-Token": TOKEN}


def _valid_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "runtime_contract_version": "1.0",
        "request_id": "req_runtime",
        "task_id": "task_runtime",
        "tool_run_id": "trun_runtime",
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_version": "1.0",
        "process_parameters": {
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        "requested_outputs": ["sem_image", "mechanical_properties"],
        "runtime_parameters": {
            "seed": 123456789,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }
    payload.update(overrides)
    return payload


def _assert_deterministic_npy(data_base64: str) -> bytes:
    npy_bytes = base64.b64decode(data_base64, validate=True)
    assert npy_bytes[:6] == b"\x93NUMPY"
    assert npy_bytes[6:8] == b"\x01\x00"
    header_size = struct.unpack("<H", npy_bytes[8:10])[0]
    header = ast.literal_eval(
        npy_bytes[10 : 10 + header_size].decode("latin1").strip()
    )
    assert header == {
        "descr": "<f4",
        "fortran_order": False,
        "shape": (512, 512),
    }
    data = npy_bytes[10 + header_size :]
    assert len(data) == 512 * 512 * 4
    assert struct.unpack_from("<f", data, 0)[0] == pytest.approx(-1.0)
    assert struct.unpack_from("<f", data, len(data) - 4)[0] == pytest.approx(1.0)
    return npy_bytes


def test_runtime_official_host_is_loopback_only() -> None:
    assert DEFAULT_HOST == "127.0.0.1"


def test_all_internal_paths_require_token_and_ready_does_not_execute() -> None:
    app = create_app(MockRuntimeSettings(token=TOKEN))
    valid_request = _valid_request()

    with TestClient(app) as client:
        for method, path, json_body in (
            ("get", "/internal/v1/health/live", None),
            ("get", "/internal/v1/health/ready", None),
            ("post", "/internal/v1/execute", valid_request),
        ):
            missing = client.request(method, path, json=json_body)
            wrong = client.request(
                method,
                path,
                json=json_body,
                headers={"X-ZTA35G-Runtime-Token": "wrong-token"},
            )
            assert missing.status_code == wrong.status_code == 401
            assert missing.json()["error"]["code"] == "INVALID_RUNTIME_REQUEST"
            assert wrong.json()["error"]["code"] == "INVALID_RUNTIME_REQUEST"
            assert TOKEN not in missing.text
            assert TOKEN not in wrong.text

        live = client.get(
            "/internal/v1/health/live",
            headers=AUTH_HEADERS,
        )
        ready = client.get(
            "/internal/v1/health/ready",
            headers=AUTH_HEADERS,
        )

    assert live.status_code == 200
    assert live.json()["status"] == "LIVE"
    assert ready.status_code == 200
    assert ready.json()["status"] == "READY"
    assert ready.json()["can_accept_execution"] is True
    assert app.state.runtime_state.execution_count == 0


def test_execute_echoes_contract_and_returns_deterministic_base64_npy() -> None:
    app = create_app(MockRuntimeSettings(token=TOKEN))

    with TestClient(app) as client:
        first = client.post(
            "/internal/v1/execute",
            json=_valid_request(),
            headers=AUTH_HEADERS,
        )
        second = client.post(
            "/internal/v1/execute",
            json=_valid_request(
                request_id="req_second",
                task_id="task_second",
                tool_run_id="trun_second",
            ),
            headers=AUTH_HEADERS,
        )

    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["request_id"] == "req_runtime"
    assert body["task_id"] == "task_runtime"
    assert body["tool_run_id"] == "trun_runtime"
    assert body["runtime_contract_version"] == "1.0"
    assert body["tool_id"] == "zta35g_sem_virtual_lab"
    assert body["tool_version"] == "0.1.0"
    assert body["schema_version"] == "1.0"
    assert body["status"] == "SUCCEEDED"
    assert body["requested_outputs"] == ["sem_image", "mechanical_properties"]
    assert body["completed_outputs"] == ["sem_image", "mechanical_properties"]
    assert body["failed_outputs"] == []
    assert body["actual_runtime_parameters"] == {
        "seed": 123456789,
        "num_samples": 1,
        "guide_scale": 2.0,
        "timesteps": 1000,
    }
    assert len(body["images"]) == 1
    image = body["images"][0]
    assert image["image_role"] == "generated_sem"
    assert image["requested_output"] is True
    assert image["dtype"] == "float32"
    assert image["numpy_dtype"] == "<f4"
    assert image["shape"] == [512, 512]
    assert image["value_range"] == [-1.0, 1.0]
    first_npy = _assert_deterministic_npy(image["data_base64"])
    second_npy = _assert_deterministic_npy(
        second.json()["images"][0]["data_base64"]
    )
    assert first_npy == second_npy
    assert len(first.content) < 4 * 1024 * 1024
    assert app.state.runtime_state.execution_count == 2


@pytest.mark.parametrize(
    ("overrides", "expected_status", "expected_code"),
    [
        ({"tool_id": "unknown_tool"}, 422, "UNSUPPORTED_TOOL"),
        (
            {"runtime_contract_version": "2.0"},
            409,
            "SCHEMA_VERSION_MISMATCH",
        ),
        ({"tool_version": "9.9.9"}, 409, "TOOL_VERSION_MISMATCH"),
        ({"schema_version": "9.9"}, 409, "SCHEMA_VERSION_MISMATCH"),
        (
            {
                "runtime_parameters": {
                    "seed": 123456789,
                    "num_samples": 1,
                    "guide_scale": 3.0,
                    "timesteps": 1000,
                }
            },
            422,
            "INVALID_RUNTIME_REQUEST",
        ),
    ],
)
def test_execute_rejects_unknown_tool_version_mismatch_and_fixed_parameter_changes(
    overrides: dict[str, object],
    expected_status: int,
    expected_code: str,
) -> None:
    app = create_app(MockRuntimeSettings(token=TOKEN))

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/execute",
            json=_valid_request(**overrides),
            headers=AUTH_HEADERS,
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert app.state.runtime_state.execution_count == 0


def test_second_concurrent_execute_is_immediately_busy_without_queue() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_hook() -> None:
        entered.set()
        assert release.wait(timeout=5)

    app = create_app(
        MockRuntimeSettings(token=TOKEN),
        execution_hook=blocking_hook,
    )
    first_result: list[object] = []

    def execute_first() -> None:
        with TestClient(app) as client:
            first_result.append(
                client.post(
                    "/internal/v1/execute",
                    json=_valid_request(),
                    headers=AUTH_HEADERS,
                )
            )

    thread = threading.Thread(target=execute_first)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with TestClient(app) as client:
            busy = client.post(
                "/internal/v1/execute",
                json=_valid_request(
                    request_id="req_busy",
                    task_id="task_busy",
                    tool_run_id="trun_busy",
                ),
                headers=AUTH_HEADERS,
            )
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert busy.status_code == 503
    assert busy.json()["error"] == {
        "code": "RUNTIME_BUSY",
        "safe_message": "ZTA35G Runtime 正忙。",
        "retryable": True,
        "failed_step": None,
        "details": {},
    }
    assert len(first_result) == 1
    assert first_result[0].status_code == 200
    assert app.state.runtime_state.execution_count == 1


def test_unexpected_execute_error_is_safe_and_releases_lock_for_next_request() -> None:
    hook_calls = 0

    def fail_once() -> None:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            raise RuntimeError("secret traceback C:\\private\\model.py")

    app = create_app(
        MockRuntimeSettings(token=TOKEN),
        execution_hook=fail_once,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.post(
            "/internal/v1/execute",
            json=_valid_request(),
            headers=AUTH_HEADERS,
        )
        recovered = client.post(
            "/internal/v1/execute",
            json=_valid_request(
                request_id="req_recovered",
                task_id="task_recovered",
                tool_run_id="trun_recovered",
            ),
            headers=AUTH_HEADERS,
        )

    assert failed.status_code == 500
    assert failed.json() == {
        "runtime_contract_version": "1.0",
        "error": {
            "code": "INTERNAL_RUNTIME_ERROR",
            "safe_message": "Runtime 内部处理失败。",
            "retryable": False,
            "failed_step": None,
            "details": {},
        },
    }
    assert "secret" not in failed.text
    assert "private" not in failed.text
    assert "traceback" not in failed.text.lower()
    assert recovered.status_code == 200
    assert recovered.json()["tool_run_id"] == "trun_recovered"
    assert app.state.runtime_state.execution_lock.locked() is False
    assert app.state.runtime_state.execution_count == 2


def test_response_limit_uses_actual_compact_json_utf8_bytes(monkeypatch) -> None:
    baseline_app = create_app(MockRuntimeSettings(token=TOKEN))
    with TestClient(baseline_app) as client:
        baseline = client.post(
            "/internal/v1/execute",
            json=_valid_request(),
            headers=AUTH_HEADERS,
        )
    assert baseline.status_code == 200
    actual_json_bytes = len(baseline.content)
    python_repr_bytes = len(str(baseline.json()).encode("utf-8"))
    assert python_repr_bytes > actual_json_bytes

    monkeypatch.setattr(runtime_main, "MAX_RESPONSE_BYTES", actual_json_bytes)
    limited_app = create_app(MockRuntimeSettings(token=TOKEN))
    with TestClient(limited_app) as client:
        response = client.post(
            "/internal/v1/execute",
            json=_valid_request(),
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert len(response.content) == actual_json_bytes
    assert len(
        json.dumps(
            response.json(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) == actual_json_bytes
