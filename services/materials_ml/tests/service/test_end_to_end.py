from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import numpy as np
import pandas as pd
import pytest

from materials_ml import load_package, predict, train_regression, read_csv, TrainingSpec
from materials_ml_service.domain import TrainingRun, ModelAsset
from materials_ml_service.worker import Worker
from materials_ml_service.windows import RunnerError

from test_windows import active_job_handles, assert_exited


def process_environment():
    return {k: v for k, v in os.environ.items() if k.upper() in
            {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH"}}


class Server:
    def __init__(self, settings):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.settings, self.process = settings, None
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        env = process_environment()
        for key, value in self.settings.model_dump().items():
            env["ML_" + key.upper()] = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        command = [sys.executable, "-I", "-m", "uvicorn", "materials_ml_service.api:create_app",
            "--factory", "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log", "--log-level", "critical"]
        if getattr(self, "program", None):
            command = [sys.executable, "-I", "-c", self.program, str(self.port)]
        self.process = subprocess.Popen(command,
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail("Service failed to start: " + self.process.stderr.read().decode(errors="replace"))
            try:
                if httpx.get(self.url + "/health/ready", timeout=1, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        pytest.fail("Service readiness timed out")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.kill(); self.process.wait(timeout=5)


@pytest.fixture
def server(service):
    server = Server(service.settings)
    try:
        server.start()
        yield server
    finally:
        server.stop()


def worker_process(server):
    env = process_environment()
    env.update(ML_SERVICE_URL=server.url, ML_WORKER_TOKEN=server.settings.worker_token.get_secret_value())
    return subprocess.Popen([sys.executable, "-I", "-m", "materials_ml_service.worker", "--once"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)


def client(server):
    return httpx.Client(base_url=server.url, headers={"Authorization": "Bearer " + server.settings.resource_token.get_secret_value()},
                         timeout=10, trust_env=False)


def submit(client, payload, algorithm="LR", suffix=""):
    response = client.post("/api/v1/scopes/independent/datasets", headers={"Idempotency-Key": "dataset" + suffix},
                            files={"file": ("data.csv", payload, "text/csv")})
    assert response.status_code == 200, response.text
    response = client.post("/api/v1/scopes/independent/training-runs", headers={"Idempotency-Key": "train" + suffix},
        json={"dataset_id": response.json()["id"], "features": ["x", "z"], "target": "strength_MPa", "algorithm": algorithm})
    assert response.status_code == 202, response.text
    return response.json()


def wait_status(client, identity, states, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/scopes/independent/training-runs/{identity}")
        assert result.status_code == 200, result.text
        if result.json()["status"] in states:
            return result.json()
        time.sleep(.05)
    pytest.fail("Training did not reach the expected state")


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_real_http_worker_model_download_and_prediction(server, csv_payload, tmp_path, algorithm):
    process = None
    with client(server) as http:
        run = submit(http, csv_payload, algorithm)
        assert run["status"] == "PENDING"
        try:
            process = worker_process(server)
            result = wait_status(http, run["id"], {"SUCCEEDED", "FAILED", "CANCELLED"})
            assert result["status"] == "SUCCEEDED", result
            assert process.wait(timeout=10) == 0, process.stderr.read().decode(errors="replace")
            model = http.get(f"/api/v1/scopes/independent/models/{result['model_id']}").json()
            assert model["units"] == {"x": None, "z": None, "strength_MPa": None}
            assert model["warnings"] == ["UNIT_UNKNOWN", "IID_NOT_VERIFIED"]
            location = tmp_path / "downloaded"
            location.mkdir()
            for member in model["members"]:
                response = http.get(f"/api/v1/scopes/independent/models/{result['model_id']}/files/{member}")
                assert response.status_code == 200
                (location / member).write_bytes(response.content)
            restored = load_package(location, trusted=True)
            table = read_csv(csv_payload)
            original = train_regression(table, TrainingSpec(("x", "z"), "strength_MPa", algorithm=algorithm))
            np.testing.assert_allclose(predict(restored, table[["z", "x"]]).values,
                                       predict(original, table[["x", "z"]]).values, rtol=1e-10, atol=1e-12)
        finally:
            if process and process.poll() is None:
                process.kill(); process.wait(timeout=5)


def long_data():
    rng = np.random.default_rng(42)
    x, z = rng.normal(size=(2, 10000))
    return pd.DataFrame({"x": x, "z": z, "strength_MPa": 3*x + z*z + rng.normal(size=len(x))}).to_csv(index=False).encode()


def test_real_running_cancel_stops_process_tree(server, service):
    process = None
    with client(server) as http:
        run = submit(http, long_data(), "RF")
        try:
            process = worker_process(server)
            wait_status(http, run["id"], {"RUNNING"})
            domain = service.get(TrainingRun, "independent", run["id"])
            handles = active_job_handles(service.job_name(domain))
            assert handles
            assert http.post(f"/api/v1/scopes/independent/training-runs/{run['id']}/cancel").status_code == 200
            result = wait_status(http, run["id"], {"CANCELLED"})
            assert result["model_id"] is None
            assert process.wait(timeout=10) == 0
            for handle in handles:
                assert_exited(handle)
        finally:
            if process and process.poll() is None:
                process.kill(); process.wait(timeout=5)


def test_worker_death_and_restart_recovers_without_retraining(server, service):
    process = restarted = None
    with client(server) as http:
        run = submit(http, long_data(), "RF")
        try:
            process = worker_process(server)
            wait_status(http, run["id"], {"RUNNING"})
            domain = service.get(TrainingRun, "independent", run["id"])
            handles = active_job_handles(service.job_name(domain))
            assert handles
            process.kill(); process.wait(timeout=5)
            for handle in handles:
                assert_exited(handle)
            restarted = worker_process(server)
            result = wait_status(http, run["id"], {"FAILED"})
            assert result["model_id"] is None and result["error_code"] == "WORKER_INTERRUPTED"
            assert restarted.wait(timeout=10) == 0
            assert service.get(TrainingRun, "independent", run["id"]).attempt == 1
        finally:
            for proc in (process, restarted):
                if proc and proc.poll() is None:
                    proc.kill(); proc.wait(timeout=5)


def test_job_setup_failure_never_sends_start(server, csv_payload):
    with client(server) as http:
        run = submit(http, csv_payload)
        def failed_runner(*args):
            raise RunnerError("JOB_SETUP_FAILED")
        worker = Worker(server.url, server.settings.worker_token.get_secret_value(), runner=failed_runner)
        calls, command = [], worker.command
        def tracked(run, action, *args, **kwargs):
            calls.append(action)
            return command(run, action, *args, **kwargs)
        worker.command = tracked
        try:
            worker.run(once=True)
        finally:
            worker.close()
        result = wait_status(http, run["id"], {"FAILED"})
        assert "start" not in calls and result["error_code"] == "JOB_SETUP_FAILED"


def test_service_restart_preserves_pending_and_runs_after_restart(server, csv_payload):
    with client(server) as http:
        run = submit(http, csv_payload)
        server.stop(); server.start()
        assert wait_status(http, run["id"], {"PENDING"})["model_id"] is None
        process = worker_process(server)
        try:
            result = wait_status(http, run["id"], {"SUCCEEDED", "FAILED"})
            assert result["status"] == "SUCCEEDED"
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)


@pytest.mark.parametrize("operation", ["claim", "start", "complete"])
def test_lost_http_receipt_reuses_claim_and_published_model(server, service, csv_payload, operation):
    with client(server) as http:
        run = submit(http, csv_payload)
        worker = Worker(server.url, server.settings.worker_token.get_secret_value())
        post, lost = worker.client.post, []
        def uncertain(url, **kwargs):
            response = post(url, **kwargs)
            if str(url).endswith("/" + operation) and response.is_success and not lost:
                lost.append(True)
                raise httpx.ReadError("Injected lost response", request=response.request)
            return response
        worker.client.post = uncertain
        try:
            worker.run(once=True)
        finally:
            worker.close()
        assert lost
        result = wait_status(http, run["id"], {"SUCCEEDED"})
        assert service.get(TrainingRun, "independent", run["id"]).attempt == 1
        assert [m.id for m in service.list(ModelAsset, "independent")] == [result["model_id"]]


def test_service_restart_during_running_keeps_original_claim(server, service):
    with client(server) as http:
        run = submit(http, long_data(), "RF")
        process = worker_process(server)
        try:
            wait_status(http, run["id"], {"RUNNING"})
            original = service.get(TrainingRun, "independent", run["id"])
            server.stop(); server.start()
            result = wait_status(http, run["id"], {"SUCCEEDED", "FAILED"}, timeout=90)
            assert result["status"] == "SUCCEEDED", result
            assert process.wait(timeout=10) == 0
            current = service.get(TrainingRun, "independent", run["id"])
            assert current.claim_id == original.claim_id and current.attempt == 1
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
