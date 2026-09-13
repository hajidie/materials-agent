"""Opt-in real GPU HTTP acceptance, independent of Backend/LLM/DB/storage.

EBSD_REAL_MODEL_TEST=1 enables the CNN check. EBSD_SEM_HTTP_TEST=1 also runs
two full SEM generations in the same server process (approximately 20 minutes).
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from types import ModuleType

import pytest

from materialsagent_zta35g_runtime.app import create_runtime_server, close_runtime_server
from materialsagent_zta35g_runtime.config import RuntimeSettings
from materialsagent_zta35g_runtime.ebsd import TOOL_ID, HEADER
from materialsagent_zta35g_runtime.inference import TorchZTA35GComponents, ZTA35GInferenceEngine


def digest(model):
    result = sha256()
    for name, value in model.state_dict().items():
        result.update(name.encode())
        result.update(value.detach().cpu().numpy().tobytes())
    return result.hexdigest()


def resource_snapshot(label):
    import torch
    usage = {"label": label, "cpu_seconds": round(time.process_time(), 3),
        "gpu_allocated_mib": round(torch.cuda.memory_allocated() / 2 ** 20, 2),
        "gpu_reserved_mib": round(torch.cuda.memory_reserved() / 2 ** 20, 2)}
    if os.name == "nt":
        command = "(Get-Process -Id %d).WorkingSet64" % os.getpid()
        usage["process_rss_mib"] = round(int(subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", command], timeout=10)) / 2 ** 20, 2)
    print("EBSD_HTTP_RESOURCES", json.dumps(usage), flush=True)


@pytest.mark.skipif(os.environ.get("EBSD_REAL_MODEL_TEST") != "1", reason="real GPU EBSD acceptance is opt-in")
def test_ebsd_real_gpu_independent_http_and_sem_isolation():
    import torch
    assert torch.cuda.is_available()
    root = Path(os.environ["EBSD_MODEL_ROOT"])
    sem_root = Path(__file__).resolve().parents[3] / "SEM" / "ZTA35G_lab"
    full_sem = os.environ.get("EBSD_SEM_HTTP_TEST") == "1"
    token = secrets.token_hex(24)
    flags = (torch.backends.cudnn.allow_tf32, torch.backends.cuda.matmul.allow_tf32)
    components = TorchZTA35GComponents(sem_root)
    engine = ZTA35GInferenceEngine(components)
    server = create_runtime_server(RuntimeSettings(token=token, model_root=sem_root, ebsd_model_root=root), engine, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    def request(method, path, body=None, headers=None, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=1200)
        try:
            connection.request(method, path, body=body,
                headers={**({"X-ZTA35G-Runtime-Token": token} if authorized else {}), **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    def predict(index=1, **overrides):
        payload = (root / "data" / "Examples_CNN" / ("cnn_%d.jpg" % index)).read_bytes()
        metadata = {"request_id": "req_ebsd_%d" % index, "task_id": "task_ebsd", "tool_run_id": "trun_ebsd_%d" % index,
            "asset_id": "asset_sample_%d" % index, "sha256": sha256(payload).hexdigest(), "seed": 20260730,
            "tool_id": TOOL_ID, "schema_version": "1.0"}
        metadata.update(overrides)
        return request("POST", "/internal/v1/ebsd/execute", payload,
            {"Content-Type": "application/octet-stream", HEADER: json.dumps(metadata)})
    try:
        assert server.ebsd_engine is not None and server.runtime_state.model_loaded
        models = (components._bundle.ddpm, components._bundle.densenet, server.ebsd_engine.model)
        before = [digest(model) for model in models]
        assert all(not model.training for model in models)
        assert request("GET", "/internal/v1/ebsd/health/ready", authorized=False)[0] == 401
        assert request("GET", "/internal/v1/ebsd/health/ready")[1]["device"] == "cuda"
        assert predict(sha256="0" * 64)[0] == 422
        assert predict(unexpected="field")[0] == 422
        assert request("POST", "/internal/v1/ebsd/execute", b"broken",
            {"Content-Type": "application/octet-stream", HEADER: "{}"})[0] == 422
        with server.runtime_state.execution_lock:
            assert predict()[1]["error"]["code"] == "RUNTIME_BUSY"
        resource_snapshot("both_models_loaded")
        assert request("POST", "/internal/v1/ebsd/execute", b"x",
            {"Content-Type": "application/octet-stream", "Content-Length": str(10 * 1024 * 1024 + 1), HEADER: "{}"})[0] == 413
        def injected_failure(_model, _input):
            raise RuntimeError("injected GPU inference failure")
        failure_hook = server.ebsd_engine.model.register_forward_pre_hook(injected_failure)
        try:
            status, error = predict()
            assert status == 500 and error["error"]["code"] == "INTERNAL_RUNTIME_ERROR"
            assert "injected" not in json.dumps(error)
        finally:
            failure_hook.remove()
        baseline = []
        for index in range(1, 6):
            started = time.perf_counter()
            status, result = predict(index)
            print("EBSD_HTTP_LATENCY_MS", index, round((time.perf_counter() - started) * 1000, 2), flush=True)
            assert status == 200 and result["status"] == "SUCCEEDED"
            assert result["tool_run_id"] == "trun_ebsd_%d" % index
            assert set(result) == {"request_id", "task_id", "tool_run_id", "tool_id", "schema_version", "asset_id", "sha256",
                "status", "requested_outputs", "completed_outputs", "failed_outputs", "data", "images", "warnings",
                "diagnostics", "error", "model_bundle_id", "actual_runtime_parameters"}
            assert set(result["data"]) == {"yield_strength"} and result["data"]["yield_strength"]["unit"] == "MPa"
            assert result["images"] == [] and result["failed_outputs"] == [] and result["error"] is None
            baseline.append(result["data"]["yield_strength"]["value"])
        resource_snapshot("after_ebsd_predictions")
        # Compare to the original CNN forward in this same environment and precision policy.
        original_module = ModuleType("original_ebsd_cnn")
        # Execute a read-only source snapshot; import loaders would write __pycache__
        # inside the user's external research directory.
        source = (root / "model" / "CNN_model.py").read_text(encoding="utf-8")
        exec(compile(source, "original_ebsd_cnn", "exec"), original_module.__dict__)
        original = original_module.CNN_model().cuda().float().eval()
        original.load_state_dict(server.ebsd_engine.model.state_dict(), strict=True)
        from materialsagent_zta35g_runtime.ebsd import decode_image
        with torch.no_grad():
            for index, value in enumerate(baseline, 1):
                payload = (root / "data" / "Examples_CNN" / ("cnn_%d.jpg" % index)).read_bytes()
                tensor = server.ebsd_engine.transform(decode_image(payload)).unsqueeze(0).cuda().float()
                assert abs(original(tensor)[0].item() - value) <= .001
        del original
        print("EBSD_HTTP_GPU_BASELINE", json.dumps(baseline), "tf32", flags, flush=True)
        if full_sem:
            sem_results = []
            for iteration in range(2):
                entered = threading.Event()
                steps = [0]
                def progress(_module, _input):
                    entered.set()
                    steps[0] += 1
                    if steps[0] % 400 == 0:
                        print("SEM_HTTP_PROGRESS", iteration + 1, steps[0] // 2, flush=True)
                hook = components._bundle.ddpm.register_forward_pre_hook(progress)
                body = {"runtime_contract_version": "1.0", "request_id": "req_sem_%d" % iteration,
                    "task_id": "task_sem", "tool_run_id": "trun_sem_%d" % iteration,
                    "tool_id": "zta35g_sem_virtual_lab", "tool_version": "0.1.0", "schema_version": "1.0",
                    "process_parameters": {"solution_temperature": 1000, "solution_time": 3.0, "aging_temperature": 730, "aging_time": 3.0},
                    "requested_outputs": ["sem_image", "mechanical_properties"],
                    "runtime_parameters": {"seed": 20260730, "num_samples": 1, "guide_scale": 2.0, "timesteps": 1000}}
                started = time.perf_counter()
                try:
                    with ThreadPoolExecutor(1) as executor:
                        future = executor.submit(request, "POST", "/internal/v1/execute", json.dumps(body), {"Content-Type": "application/json"})
                        assert entered.wait(30)
                        assert predict()[1]["error"]["code"] == "RUNTIME_BUSY"
                        status, result = future.result(timeout=1200)
                finally:
                    hook.remove()
                assert status == 200 and result["status"] == "SUCCEEDED"
                sem_results.append(result["data"])
                status, after = predict()
                assert status == 200 and abs(after["data"]["yield_strength"]["value"] - baseline[0]) <= .001
                print("SEM_EBSD_HTTP_CYCLE_OK", iteration + 1, round(time.perf_counter() - started, 2), flush=True)
                del result
            assert abs(sem_results[0]["yield_strength"]["value"] - sem_results[1]["yield_strength"]["value"]) < .01
        assert [digest(model) for model in models] == before
        assert all(not model.training for model in models)
        assert (torch.backends.cudnn.allow_tf32, torch.backends.cuda.matmul.allow_tf32) == flags
        print("EBSD_MODEL_ISOLATION_OK", "gpu_peak_mib", round(torch.cuda.max_memory_allocated() / 2 ** 20, 2), flush=True)
    finally:
        server.shutdown()
        close_runtime_server(server)
        thread.join(timeout=5)
