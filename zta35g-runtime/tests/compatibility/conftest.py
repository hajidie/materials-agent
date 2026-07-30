import base64
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from threading import Event, Lock, Thread
from time import perf_counter
import uuid

import pytest


_REAL_MODEL_AUTHORIZATION = "P1B2_PROJECT_OWNER_AUTHORIZED"
_GPU_AUTHORIZATION = "P1B2_GPU_AUTHORIZED"
_MODEL_ENVIRONMENT = "materialsagent-zta35g"
_NVIDIA_SMI = Path(r"C:\Windows\System32\nvidia-smi.exe")
_HOST_PREFLIGHT_BYTES = 8 * 1024**3
_HOST_CRITICAL_BYTES = 2 * 1024**3
_GPU_PREFLIGHT_FREE_MIB = 5500
_GPU_CRITICAL_FREE_MIB = 400
_GPU_NAME = "NVIDIA GeForce RTX 3060 Laptop GPU"
_GPU_TOTAL_MIB = 6144
_GPU_COMPUTE_CAPABILITY = "8.6"
_GPU_INFERENCE_BUDGET = 4
_POLL_INTERVAL_SECONDS = 0.075
_PROCESS_PARAMETERS = {
    "solution_temperature": 1000,
    "solution_time": 3.0,
    "aging_temperature": 730,
    "aging_time": 3.0,
}
_RUNTIME_PARAMETERS = {
    "seed": 20260730,
    "num_samples": 1,
    "guide_scale": 2.0,
    "timesteps": 1000,
}
_STAGE_REQUESTS = (
    ("A", ("sem_image",)),
    ("B", ("mechanical_properties",)),
    ("C", ("sem_image", "mechanical_properties")),
    ("D", ("sem_image", "mechanical_properties")),
)
_COUNTED_STAGE_CALLS = (
    "sem_generation_calls",
    "ddpm_sampling_calls",
    "mechanical_prediction_calls",
    "densenet_forward_calls",
    "yield_predict_calls",
    "elongation_predict_calls",
)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def _authorization_failures(environment, python_version, require_gpu):
    failures = []
    if (
        environment.get("ZTA35G_REAL_MODEL_ACCEPTANCE")
        != _REAL_MODEL_AUTHORIZATION
    ):
        failures.append("ZTA35G_REAL_MODEL_ACCEPTANCE")
    if environment.get("CONDA_DEFAULT_ENV") != _MODEL_ENVIRONMENT:
        failures.append("CONDA_DEFAULT_ENV")
    if tuple(python_version[:2]) != (3, 8):
        failures.append("PYTHON_VERSION")
    model_root = environment.get("ZTA35G_MODEL_ROOT")
    if not isinstance(model_root, str) or not model_root.strip():
        failures.append("ZTA35G_MODEL_ROOT")
    if (
        require_gpu
        and environment.get("ZTA35G_GPU_ACCEPTANCE")
        != _GPU_AUTHORIZATION
    ):
        failures.append("ZTA35G_GPU_ACCEPTANCE")
    return tuple(failures)


def _authorized_model_root(require_gpu):
    failures = _authorization_failures(
        os.environ,
        sys.version_info[:2],
        require_gpu,
    )
    if failures:
        pytest.skip(
            "P1B2 real-model acceptance is not authorized; "
            "missing or invalid gates: %s." % ", ".join(failures)
        )
    if require_gpu:
        import torch

        if torch.cuda.is_available() is not True:
            pytest.fail(
                "P1B2 GPU acceptance was authorized but CUDA is unavailable."
            )
    return Path(os.environ["ZTA35G_MODEL_ROOT"])


@pytest.fixture
def authorization_gate():
    return _authorization_failures


@pytest.fixture
def authorized_model_root():
    return _authorized_model_root(require_gpu=False)


@pytest.fixture
def authorized_gpu_model_root():
    return _authorized_model_root(require_gpu=True)


def _host_memory():
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    kernel32 = ctypes.windll.kernel32
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise RuntimeError("HOST_MEMORY_QUERY_FAILED")
    return {
        "total_bytes": int(status.ullTotalPhys),
        "free_bytes": int(status.ullAvailPhys),
    }


def _subprocess_creation_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_nvidia_smi(arguments):
    if not _NVIDIA_SMI.is_file():
        raise RuntimeError("GPU_RESOURCE_PREFLIGHT_FAILED")
    completed = subprocess.run(
        [str(_NVIDIA_SMI)] + list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
        creationflags=_subprocess_creation_flags(),
    )
    if completed.returncode != 0:
        raise RuntimeError("GPU_RESOURCE_PREFLIGHT_FAILED")
    return tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    )


def _parse_int(value):
    return int(value.strip())


def _gpu_snapshot():
    lines = _run_nvidia_smi(
        (
            "--query-gpu=name,driver_version,memory.total,memory.used,"
            "memory.free,compute_cap",
            "--format=csv,noheader,nounits",
        )
    )
    if len(lines) != 1:
        raise RuntimeError("GPU_RESOURCE_PREFLIGHT_FAILED")
    fields = tuple(item.strip() for item in lines[0].split(","))
    if len(fields) != 6:
        raise RuntimeError("GPU_RESOURCE_PREFLIGHT_FAILED")
    return {
        "name": fields[0],
        "driver_version": fields[1],
        "total_mib": _parse_int(fields[2]),
        "used_mib": _parse_int(fields[3]),
        "free_mib": _parse_int(fields[4]),
        "compute_capability": fields[5],
    }


def _gpu_compute_process_count():
    lines = _run_nvidia_smi(
        (
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        )
    )
    return len(lines)


def _gpu_resource_preflight():
    snapshot = _gpu_snapshot()
    process_count = _gpu_compute_process_count()
    if (
        snapshot["name"] != _GPU_NAME
        or snapshot["compute_capability"] != _GPU_COMPUTE_CAPABILITY
        or snapshot["total_mib"] != _GPU_TOTAL_MIB
        or snapshot["free_mib"] < _GPU_PREFLIGHT_FREE_MIB
        or process_count != 0
    ):
        raise RuntimeError("GPU_RESOURCE_PREFLIGHT_FAILED")
    snapshot["compute_process_count"] = process_count
    return snapshot


class _ResourceSampler:
    def __init__(self):
        self._stop = Event()
        self._host_samples = []
        self._gpu_samples = []
        self._sample_lock = Lock()
        self._lifecycle_lock = Lock()
        self._host_thread = None
        self._gpu_thread = None
        self._gpu_process = None
        self._started = False
        self._stopped = False
        self._stop_summary = None
        self._stop_failure_code = None

    def start(self):
        with self._lifecycle_lock:
            if self._started or self._stopped:
                raise RuntimeError("P1B2_RESOURCE_SAMPLER_START_INVALID")
            self._started = True
        self._sample_host()
        initial_gpu = _gpu_snapshot()
        self._gpu_samples.append(
            {
                "used_mib": initial_gpu["used_mib"],
                "free_mib": initial_gpu["free_mib"],
                "utilization_percent": 0,
            }
        )
        self._host_thread = Thread(target=self._host_loop, daemon=True)
        self._host_thread.start()
        self._gpu_process = subprocess.Popen(
            [
                str(_NVIDIA_SMI),
                "--query-gpu=memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
                "--loop-ms=100",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=_subprocess_creation_flags(),
        )
        self._gpu_thread = Thread(target=self._gpu_loop, daemon=True)
        self._gpu_thread.start()

    def _sample_host(self):
        sample = _host_memory()
        with self._sample_lock:
            self._host_samples.append(sample["free_bytes"])

    def _host_loop(self):
        while not self._stop.wait(_POLL_INTERVAL_SECONDS):
            self._sample_host()

    def _gpu_loop(self):
        stream = self._gpu_process.stdout
        if stream is None:
            return
        for raw_line in stream:
            if self._stop.is_set():
                break
            fields = tuple(
                item.strip() for item in raw_line.strip().split(",")
            )
            if len(fields) != 3:
                continue
            try:
                sample = {
                    "used_mib": _parse_int(fields[0]),
                    "free_mib": _parse_int(fields[1]),
                    "utilization_percent": _parse_int(fields[2]),
                }
            except ValueError:
                continue
            with self._sample_lock:
                self._gpu_samples.append(sample)

    def _raise_stop_failure(self, code):
        with self._lifecycle_lock:
            self._stop_failure_code = code
        raise RuntimeError(code)

    def stop(self):
        with self._lifecycle_lock:
            if not self._started:
                raise RuntimeError("P1B2_RESOURCE_SAMPLER_NOT_STARTED")
            if self._stopped:
                if self._stop_failure_code is not None:
                    raise RuntimeError(self._stop_failure_code)
                if self._stop_summary is None:
                    raise RuntimeError("P1B2_RESOURCE_SAMPLING_FAILED")
                return dict(self._stop_summary)
            self._stopped = True

        self._stop.set()
        shutdown_failed = False
        if self._gpu_process is not None:
            try:
                if self._gpu_process.poll() is None:
                    self._gpu_process.terminate()
                try:
                    self._gpu_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._gpu_process.kill()
                    self._gpu_process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                shutdown_failed = True
                try:
                    if self._gpu_process.poll() is None:
                        self._gpu_process.kill()
                        self._gpu_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    shutdown_failed = True
        try:
            if self._host_thread is not None:
                self._host_thread.join(timeout=5)
            if self._gpu_thread is not None:
                self._gpu_thread.join(timeout=5)
        except RuntimeError:
            shutdown_failed = True
        try:
            _assert_sampler_shutdown(
                self._gpu_process,
                (self._host_thread, self._gpu_thread),
            )
        except RuntimeError:
            shutdown_failed = True
        if shutdown_failed:
            self._raise_stop_failure(
                "P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED"
            )
        try:
            self._sample_host()
            final_gpu = _gpu_snapshot()
        except Exception:
            self._raise_stop_failure("P1B2_RESOURCE_SAMPLING_FAILED")
        with self._sample_lock:
            self._gpu_samples.append(
                {
                    "used_mib": final_gpu["used_mib"],
                    "free_mib": final_gpu["free_mib"],
                    "utilization_percent": 0,
                }
            )
            host_samples = tuple(self._host_samples)
            gpu_samples = tuple(self._gpu_samples)
        if not host_samples or not gpu_samples:
            self._raise_stop_failure("P1B2_RESOURCE_SAMPLING_FAILED")
        summary = {
            "host_start_free_bytes": host_samples[0],
            "host_minimum_free_bytes": min(host_samples),
            "host_end_free_bytes": host_samples[-1],
            "host_sample_count": len(host_samples),
            "nvidia_smi_peak_used_mib": max(
                item["used_mib"] for item in gpu_samples
            ),
            "nvidia_smi_minimum_free_mib": min(
                item["free_mib"] for item in gpu_samples
            ),
            "nvidia_smi_peak_utilization_percent": max(
                item["utilization_percent"] for item in gpu_samples
            ),
            "nvidia_smi_sample_count": len(gpu_samples),
        }
        with self._lifecycle_lock:
            self._stop_summary = dict(summary)
        return summary


def _assert_sampler_shutdown(process, threads):
    process_is_running = process is not None and process.poll() is None
    thread_is_running = any(
        thread is not None and thread.is_alive() for thread in threads
    )
    if process_is_running or thread_is_running:
        raise RuntimeError("P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED")


class _P1B2ControlledLoadFailure(RuntimeError):
    pass


def _load_engine_with_sampler(
    engine,
    sampler,
    synchronize,
    oom_detected,
    cleanup,
):
    failure_code = None
    resources = None
    sampler.start()
    try:
        try:
            engine.load()
            synchronize()
        except Exception:
            if oom_detected():
                failure_code = "CUDA_OUT_OF_MEMORY"
            else:
                failure_code = "P1B2_ENGINE_LOAD_FAILED"
    finally:
        try:
            resources = sampler.stop()
        except RuntimeError as error:
            if str(error) != "P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED":
                raise
            failure_code = "P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED"
    if failure_code is not None:
        cleanup()
        raise _P1B2ControlledLoadFailure(failure_code) from None
    return resources


class _InferenceBudget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.reserved = 0
        self.retries = 0
        self.fifth_attempted = False

    def reserve_before_delegate(self):
        if self.reserved >= self.maximum:
            self.fifth_attempted = True
            raise RuntimeError("P1B2_GPU_INFERENCE_BUDGET_EXCEEDED")
        self.reserved += 1

    def summary(self):
        return {
            "maximum": self.maximum,
            "reserved": self.reserved,
            "retries": self.retries,
            "fifth_attempted": self.fifth_attempted,
        }


@pytest.fixture
def p1b2_gpu_offline_harness():
    return {
        "InferenceBudget": _InferenceBudget,
        "ResourceSampler": _ResourceSampler,
        "load_engine_with_sampler": _load_engine_with_sampler,
        "assert_sampler_shutdown": _assert_sampler_shutdown,
    }


def _tensor_device_summary(module, torch):
    tensors = tuple(module.parameters()) + tuple(module.buffers())
    if not tensors:
        raise RuntimeError("P1B2_MODEL_DEVICE_VALIDATION_FAILED")
    devices = {str(value.device) for value in tensors}
    forbidden_dtypes = {torch.float16, torch.bfloat16}
    if devices != {"cuda:0"} or any(
        value.dtype in forbidden_dtypes for value in tensors
    ):
        raise RuntimeError("P1B2_MODEL_DEVICE_VALIDATION_FAILED")
    return "cuda:0"


def _image_statistics(image, np):
    return {
        "minimum": float(image.min()),
        "maximum": float(image.max()),
        "mean": float(image.mean()),
        "standard_deviation": float(image.std()),
        "nan_count": int(np.isnan(image).sum()),
        "inf_count": int(np.isinf(image).sum()),
        "all_constant": bool(np.all(image == image.flat[0])),
    }


def _image_comparison(left, right, np):
    absolute_difference = np.abs(
        left.astype(np.float64) - right.astype(np.float64)
    )
    return {
        "exact_equal": bool(np.array_equal(left, right)),
        "allclose": bool(np.allclose(left, right, rtol=1e-5, atol=1e-6)),
        "max_abs_diff": float(absolute_difference.max()),
        "mean_abs_diff": float(absolute_difference.mean()),
    }


def _mechanical_comparison(left, right):
    left_yield = left["performance"]["yield_strength"]
    right_yield = right["performance"]["yield_strength"]
    left_elongation = left["performance"]["elongation"]
    right_elongation = right["performance"]["elongation"]
    return {
        "yield_exact_equal": left_yield == right_yield,
        "yield_absolute_diff": abs(left_yield - right_yield),
        "elongation_exact_equal": left_elongation == right_elongation,
        "elongation_absolute_diff": abs(
            left_elongation - right_elongation
        ),
    }


def _timing_statistics(values):
    normalized = tuple(float(value) for value in values)
    if not normalized:
        raise RuntimeError("P1B2_TIMING_SAMPLES_MISSING")
    return {
        "sample_count": len(normalized),
        "minimum": min(normalized),
        "maximum": max(normalized),
        "mean": statistics.mean(normalized),
        "median_p50": statistics.median(normalized),
        "raw_timings": list(normalized),
        "p95": "insufficient_samples",
        "p99": "insufficient_samples",
    }


def _sha256_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_directory(repository_root, run_id):
    relative_directory = Path("tmp") / "p1b2-gpu-inference" / run_id
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "tmp/p1b2-gpu-inference"],
        cwd=str(repository_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=_subprocess_creation_flags(),
    )
    if ignored.returncode != 0:
        raise RuntimeError("P1B2_ARTIFACT_DIRECTORY_NOT_IGNORED")
    absolute_directory = repository_root / relative_directory
    absolute_directory.mkdir(parents=True, exist_ok=False)
    return relative_directory, absolute_directory


def _safe_stage_summary(stage):
    return {
        key: value
        for key, value in stage.items()
        if key not in ("raw_image", "result_object")
    }


def _write_json(path, payload):
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)


def _build_artifacts(
    repository_root,
    run_id,
    image,
    npy_payload,
    safe_summary,
    np,
):
    from PIL import Image

    relative_directory, absolute_directory = _artifact_directory(
        repository_root, run_id
    )
    npy_name = "sem-image-seed-20260730.npy"
    png_name = "sem-image-seed-20260730-preview.png"
    summary_name = "summary.json"
    manifest_name = "artifact-manifest.json"
    npy_path = absolute_directory / npy_name
    png_path = absolute_directory / png_name
    summary_path = absolute_directory / summary_name
    manifest_path = absolute_directory / manifest_name

    npy_path.write_bytes(npy_payload)
    preview = np.clip(
        np.rint((image + 1.0) * 127.5),
        0,
        255,
    ).astype(np.uint8)
    preview_image = Image.fromarray(preview, mode="L")
    preview_image.save(str(png_path), format="PNG")
    _write_json(summary_path, safe_summary)

    manifest_entries = []
    manifest_definitions = (
        (
            summary_name,
            "safe_gpu_inference_summary",
            False,
        ),
        (
            npy_name,
            "fixed_seed_c_mode_sem_review",
            False,
        ),
        (
            png_name,
            "review_only_sem_preview",
            True,
        ),
    )
    for file_name, purpose, review_only in manifest_definitions:
        path = absolute_directory / file_name
        manifest_entries.append(
            {
                "file_name": file_name,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "purpose": purpose,
                "review_only_preview": review_only,
            }
        )
    _write_json(manifest_path, {"artifacts": manifest_entries})

    file_metadata = {}
    for file_name in (
        summary_name,
        npy_name,
        png_name,
        manifest_name,
    ):
        path = absolute_directory / file_name
        file_metadata[file_name] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
    return {
        "absolute_directory": str(absolute_directory),
        "display_directory": relative_directory.as_posix(),
        "files": file_metadata,
        "preview": {
            "review_only_preview": True,
            "mode": preview_image.mode,
            "size": list(preview_image.size),
            "has_alpha": "A" in preview_image.getbands(),
        },
    }


def _payload_validation(c_stage, process_parameters, runtime_parameters):
    import numpy as np

    from materialsagent_zta35g_runtime.constants import MAX_RESPONSE_BYTES
    from materialsagent_zta35g_runtime.contracts import (
        ExecuteRequest,
        build_execute_response,
        serialize_response,
    )

    image = c_stage["raw_image"]
    serialization_started = perf_counter()
    buffer = BytesIO()
    np.save(buffer, image, allow_pickle=False)
    npy_payload = buffer.getvalue()
    serialization_seconds = perf_counter() - serialization_started

    encode_started = perf_counter()
    encoded = base64.b64encode(npy_payload)
    base64_encode_seconds = perf_counter() - encode_started
    decode_started = perf_counter()
    decoded = base64.b64decode(encoded, validate=True)
    base64_decode_seconds = perf_counter() - decode_started
    decoded_array = np.load(BytesIO(decoded), allow_pickle=False)

    runtime_image = c_stage["result_object"].images[0]
    runtime_payload = base64.b64decode(
        runtime_image["data_base64"].encode("ascii"),
        validate=True,
    )
    request = ExecuteRequest(
        request_id="p1b2-gpu-acceptance-c",
        task_id="p1b2-gpu-acceptance-task",
        tool_run_id="p1b2-gpu-acceptance-tool-run",
        process_parameters=dict(process_parameters),
        requested_outputs=("sem_image", "mechanical_properties"),
        runtime_parameters=dict(runtime_parameters),
    )
    response = build_execute_response(request, c_stage["result_object"])
    serialized_response = serialize_response(response)
    payload_sha256 = sha256(npy_payload).hexdigest()
    return (
        {
            "source_stage": "C",
            "npy_bytes": len(npy_payload),
            "base64_characters": len(encoded),
            "base64_decoded_bytes": len(decoded),
            "json_estimate_bytes": len(serialized_response),
            "json_margin_bytes": MAX_RESPONSE_BYTES
            - len(serialized_response),
            "serialization_seconds": serialization_seconds,
            "base64_encode_seconds": base64_encode_seconds,
            "base64_decode_seconds": base64_decode_seconds,
            "allow_pickle_false_roundtrip": (
                decoded_array.dtype == np.dtype("<f4")
                and decoded_array.shape == (512, 512)
            ),
            "array_exact_equal": bool(
                np.array_equal(decoded_array, image)
            ),
            "base64_bytes_exact_equal": decoded == npy_payload,
            "sha256": payload_sha256,
            "sha256_matches_runtime_payload": (
                payload_sha256 == runtime_image["sha256"]
                and runtime_payload == npy_payload
            ),
        },
        npy_payload,
    )


def _run_p1b2_gpu_inference_session(
    model_root,
    host_start,
    gpu_start,
):
    import gc
    import numpy as np
    import torch

    from materialsagent_zta35g_runtime import inference as inference_module
    from materialsagent_zta35g_runtime.inference import (
        TorchZTA35GComponents,
        ZTA35GInferenceEngine,
    )

    if torch.cuda.is_available() is not True:
        pytest.fail(
            "P1B2 GPU acceptance was authorized but CUDA is unavailable.",
            pytrace=False,
        )
    if torch.cuda.device_count() != 1 or torch.cuda.current_device() != 0:
        pytest.fail("GPU_RESOURCE_PREFLIGHT_FAILED", pytrace=False)

    repository_root = Path(__file__).resolve().parents[3]
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:12]
    )
    preflight = {
        "host_total_bytes": host_start["total_bytes"],
        "host_start_free_bytes": host_start["free_bytes"],
        "host_preload_free_bytes": None,
        "gpu_name": gpu_start["name"],
        "driver_version": gpu_start["driver_version"],
        "compute_capability": gpu_start["compute_capability"],
        "gpu_total_mib": gpu_start["total_mib"],
        "gpu_start_used_mib": gpu_start["used_mib"],
        "gpu_start_free_mib": gpu_start["free_mib"],
        "gpu_compute_process_count": gpu_start[
            "compute_process_count"
        ],
    }

    counts = {
        "engine_execute_calls": 0,
        "sem_generation_calls": 0,
        "ddpm_sampling_calls": 0,
        "mechanical_prediction_calls": 0,
        "densenet_forward_calls": 0,
        "yield_predict_calls": 0,
        "elongation_predict_calls": 0,
        "fit_calls": 0,
        "score_calls": 0,
        "backward_calls": 0,
        "optimizer_calls": 0,
    }
    budget = _InferenceBudget(_GPU_INFERENCE_BUDGET)
    active_stage = {"label": None}
    raw_images = {}
    sem_timings = {}
    mechanical_timings = {}
    oom_detected = {"value": False}
    engine_load_calls = {"value": 0}
    bundle_load_calls = {"value": 0}
    gpu_move_stages = {"value": 0}
    cpu_load_seconds = {"value": 0.0}

    components = TorchZTA35GComponents(model_root)
    engine = ZTA35GInferenceEngine(components)
    original_bundle_load = inference_module.ModelBundleLoader.load
    original_backward = torch.Tensor.backward
    original_optimizer_step = torch.optim.Optimizer.step
    original_module_to = torch.nn.Module.to
    original_tensor_to = torch.Tensor.to
    originals = {}
    stages = {}
    load_resources = None
    payload = None
    npy_payload = None
    engine_summary = None
    controlled_load_failure = None
    cleanup_state = {
        "attempted": False,
        "closed": False,
        "failure_code": None,
    }

    out_of_memory_type = getattr(torch.cuda, "OutOfMemoryError", None)
    if (
        not isinstance(out_of_memory_type, type)
        or not issubclass(out_of_memory_type, BaseException)
    ):
        pytest.fail("P1B2_CUDA_OOM_TYPE_UNAVAILABLE", pytrace=False)

    def cleanup_engine():
        if cleanup_state["attempted"]:
            return
        cleanup_state["attempted"] = True
        try:
            engine.close()
            cleanup_state["closed"] = True
        except Exception:
            cleanup_state["failure_code"] = "P1B2_ENGINE_RELEASE_FAILED"
        try:
            gc.collect()
        except Exception:
            cleanup_state["failure_code"] = "P1B2_ENGINE_RELEASE_FAILED"
        try:
            torch.cuda.empty_cache()
        except Exception:
            cleanup_state["failure_code"] = "P1B2_ENGINE_RELEASE_FAILED"

    def guarded_module_to(module, *args, **kwargs):
        try:
            return original_module_to(module, *args, **kwargs)
        except out_of_memory_type:
            oom_detected["value"] = True
            raise

    def guarded_tensor_to(tensor, *args, **kwargs):
        try:
            return original_tensor_to(tensor, *args, **kwargs)
        except out_of_memory_type:
            oom_detected["value"] = True
            raise

    def counted_backward(tensor, *args, **kwargs):
        counts["backward_calls"] += 1
        return original_backward(tensor, *args, **kwargs)

    def counted_optimizer_step(optimizer, *args, **kwargs):
        counts["optimizer_calls"] += 1
        return original_optimizer_step(optimizer, *args, **kwargs)

    def counted_bundle_load(loader):
        bundle_load_calls["value"] += 1
        started = perf_counter()
        try:
            return original_bundle_load(loader)
        finally:
            cpu_load_seconds["value"] += perf_counter() - started

    inference_module.ModelBundleLoader.load = counted_bundle_load
    torch.Tensor.backward = counted_backward
    torch.optim.Optimizer.step = counted_optimizer_step
    torch.nn.Module.to = guarded_module_to
    torch.Tensor.to = guarded_tensor_to

    try:
        preload_host = _host_memory()
        preflight["host_preload_free_bytes"] = preload_host["free_bytes"]
        if preload_host["free_bytes"] < _HOST_PREFLIGHT_BYTES:
            pytest.fail("HOST_MEMORY_PREFLIGHT_FAILED", pytrace=False)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        load_sampler = _ResourceSampler()
        load_started = perf_counter()
        print("P1B2_GPU_LOAD_START", flush=True)
        engine_load_calls["value"] += 1
        gpu_move_stages["value"] += 1
        load_resources = _load_engine_with_sampler(
            engine=engine,
            sampler=load_sampler,
            synchronize=torch.cuda.synchronize,
            oom_detected=lambda: oom_detected["value"],
            cleanup=cleanup_engine,
        )
        engine_load_seconds = perf_counter() - load_started
        load_resources.update(
            {
                "max_memory_allocated": int(
                    torch.cuda.max_memory_allocated()
                ),
                "max_memory_reserved": int(
                    torch.cuda.max_memory_reserved()
                ),
            }
        )
        if oom_detected["value"]:
            cleanup_engine()
            raise _P1B2ControlledLoadFailure("CUDA_OUT_OF_MEMORY")
        if engine.device_kind != "cuda":
            pytest.fail(
                "P1B2_MODEL_DEVICE_VALIDATION_FAILED", pytrace=False
            )

        bundle = components._bundle
        if bundle is None:
            pytest.fail("P1B2_MODEL_BUNDLE_MISSING", pytrace=False)
        ddpm_device = _tensor_device_summary(bundle.ddpm, torch)
        densenet_device = _tensor_device_summary(bundle.densenet, torch)
        engine_summary = {
            "model_bundle_id": engine.model_bundle_id,
            "engine_device": engine.device_kind,
            "ddpm_device": ddpm_device,
            "densenet_device": densenet_device,
            "svr_device": "cpu",
            "engine_load_calls": engine_load_calls["value"],
            "bundle_load_calls": bundle_load_calls["value"],
            "gpu_move_stages": gpu_move_stages["value"],
            "cpu_load_seconds": cpu_load_seconds["value"],
            "gpu_move_seconds": max(
                0.0, engine_load_seconds - cpu_load_seconds["value"]
            ),
            "engine_load_seconds": engine_load_seconds,
            "load_resources": load_resources,
        }
        if engine_summary["gpu_move_seconds"] <= 0.0:
            pytest.fail("P1B2_GPU_MOVE_TIMING_INVALID", pytrace=False)
        print(
            "P1B2_GPU_LOAD_COMPLETE cpu_seconds=%.6f gpu_seconds=%.6f"
            % (
                engine_summary["cpu_load_seconds"],
                engine_summary["gpu_move_seconds"],
            ),
            flush=True,
        )

        original_execute = engine.execute
        original_generate_sem = components.generate_sem
        original_predict_mechanical = components.predict_mechanical
        original_densenet_forward = bundle.densenet.forward
        original_yield_predict = bundle.yield_model.predict
        original_elongation_predict = bundle.elongation_model.predict
        original_yield_fit = bundle.yield_model.fit
        original_elongation_fit = bundle.elongation_model.fit
        original_yield_score = bundle.yield_model.score
        original_elongation_score = bundle.elongation_model.score
        originals.update(
            {
                "engine_execute": original_execute,
                "generate_sem": original_generate_sem,
                "predict_mechanical": original_predict_mechanical,
                "densenet_forward": original_densenet_forward,
                "yield_predict": original_yield_predict,
                "elongation_predict": original_elongation_predict,
                "yield_fit": original_yield_fit,
                "elongation_fit": original_elongation_fit,
                "yield_score": original_yield_score,
                "elongation_score": original_elongation_score,
            }
        )

        def counted_execute(*args, **kwargs):
            counts["engine_execute_calls"] += 1
            return original_execute(*args, **kwargs)

        def counted_generate_sem(*args, **kwargs):
            budget.reserve_before_delegate()
            counts["sem_generation_calls"] += 1
            counts["ddpm_sampling_calls"] += 1
            label = active_stage["label"]
            torch.cuda.synchronize()
            started = perf_counter()
            try:
                image = original_generate_sem(*args, **kwargs)
            except out_of_memory_type:
                oom_detected["value"] = True
                raise
            finally:
                torch.cuda.synchronize()
                sem_timings[label] = perf_counter() - started
            raw_images[label] = image.copy(order="C")
            return image

        def counted_predict_mechanical(*args, **kwargs):
            counts["mechanical_prediction_calls"] += 1
            label = active_stage["label"]
            torch.cuda.synchronize()
            started = perf_counter()
            try:
                return original_predict_mechanical(*args, **kwargs)
            except out_of_memory_type:
                oom_detected["value"] = True
                raise
            finally:
                torch.cuda.synchronize()
                mechanical_timings[label] = perf_counter() - started

        def counted_densenet_forward(*args, **kwargs):
            counts["densenet_forward_calls"] += 1
            return original_densenet_forward(*args, **kwargs)

        def counted_yield_predict(*args, **kwargs):
            counts["yield_predict_calls"] += 1
            return original_yield_predict(*args, **kwargs)

        def counted_elongation_predict(*args, **kwargs):
            counts["elongation_predict_calls"] += 1
            return original_elongation_predict(*args, **kwargs)

        def counted_yield_fit(*args, **kwargs):
            counts["fit_calls"] += 1
            return original_yield_fit(*args, **kwargs)

        def counted_elongation_fit(*args, **kwargs):
            counts["fit_calls"] += 1
            return original_elongation_fit(*args, **kwargs)

        def counted_yield_score(*args, **kwargs):
            counts["score_calls"] += 1
            return original_yield_score(*args, **kwargs)

        def counted_elongation_score(*args, **kwargs):
            counts["score_calls"] += 1
            return original_elongation_score(*args, **kwargs)

        engine.execute = counted_execute
        components.generate_sem = counted_generate_sem
        components.predict_mechanical = counted_predict_mechanical
        bundle.densenet.forward = counted_densenet_forward
        bundle.yield_model.predict = counted_yield_predict
        bundle.elongation_model.predict = counted_elongation_predict
        bundle.yield_model.fit = counted_yield_fit
        bundle.elongation_model.fit = counted_elongation_fit
        bundle.yield_model.score = counted_yield_score
        bundle.elongation_model.score = counted_elongation_score

        for label, requested_outputs in _STAGE_REQUESTS:
            active_stage["label"] = label
            print("P1B2_GPU_STAGE_START=%s" % label, flush=True)
            before_counts = {
                name: counts[name] for name in _COUNTED_STAGE_CALLS
            }
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            allocated_before = int(torch.cuda.memory_allocated())
            reserved_before = int(torch.cuda.memory_reserved())
            sampler = _ResourceSampler()
            sampler.start()
            stage_started = perf_counter()
            try:
                result = engine.execute(
                    dict(_PROCESS_PARAMETERS),
                    requested_outputs,
                    dict(_RUNTIME_PARAMETERS),
                )
                torch.cuda.synchronize()
            finally:
                complete_seconds = perf_counter() - stage_started
                sampled_resources = sampler.stop()
            if oom_detected["value"]:
                pytest.fail("CUDA_OUT_OF_MEMORY", pytrace=False)
            if result.status != "SUCCEEDED":
                pytest.fail(
                    "P1B2_STAGE_%s_FAILED" % label, pytrace=False
                )
            if label not in raw_images:
                pytest.fail(
                    "P1B2_STAGE_%s_IMAGE_MISSING" % label,
                    pytrace=False,
                )
            image = raw_images[label]
            image_statistics = _image_statistics(image, np)
            if (
                image.dtype != np.dtype("<f4")
                or image.shape != (512, 512)
                or image.ndim != 2
                or not image.flags.c_contiguous
                or image.flags.f_contiguous
                or image_statistics["nan_count"] != 0
                or image_statistics["inf_count"] != 0
                or image_statistics["minimum"] < -1.0
                or image_statistics["maximum"] > 1.0
                or image_statistics["standard_deviation"] <= 0.0
                or image_statistics["all_constant"]
            ):
                pytest.fail(
                    "P1B2_STAGE_%s_INVALID_IMAGE" % label,
                    pytrace=False,
                )
            if len(result.images) != 1:
                pytest.fail(
                    "P1B2_STAGE_%s_IMAGE_COLLECTION_INVALID" % label,
                    pytrace=False,
                )
            performance = None
            if "mechanical_properties" in requested_outputs:
                performance = {
                    "yield_strength": float(
                        result.data["yield_strength"]["value"]
                    ),
                    "yield_strength_unit": result.data[
                        "yield_strength"
                    ]["unit"],
                    "elongation": float(
                        result.data["elongation"]["value"]
                    ),
                    "elongation_unit": result.data["elongation"]["unit"],
                    "clipping_applied": False,
                }
                if (
                    not math.isfinite(performance["yield_strength"])
                    or not math.isfinite(performance["elongation"])
                ):
                    pytest.fail(
                        "P1B2_STAGE_%s_NONFINITE_MECHANICAL" % label,
                        pytrace=False,
                    )
                if (
                    performance["yield_strength"] <= 0.0
                    or performance["elongation"] <= 0.0
                ):
                    pytest.fail(
                        "MECHANICAL_OUTPUT_NONPOSITIVE", pytrace=False
                    )
            cuda_free_after, cuda_total_after = torch.cuda.mem_get_info()
            sampled_resources.update(
                {
                    "memory_allocated_before": allocated_before,
                    "memory_reserved_before": reserved_before,
                    "max_memory_allocated": int(
                        torch.cuda.max_memory_allocated()
                    ),
                    "max_memory_reserved": int(
                        torch.cuda.max_memory_reserved()
                    ),
                    "memory_allocated_after": int(
                        torch.cuda.memory_allocated()
                    ),
                    "memory_reserved_after": int(
                        torch.cuda.memory_reserved()
                    ),
                    "torch_cuda_free_after": int(cuda_free_after),
                    "torch_cuda_total": int(cuda_total_after),
                }
            )
            if (
                sampled_resources["host_minimum_free_bytes"]
                < _HOST_CRITICAL_BYTES
            ):
                pytest.fail("HOST_MEMORY_CRITICAL_LOW", pytrace=False)
            if (
                sampled_resources["nvidia_smi_minimum_free_mib"]
                < _GPU_CRITICAL_FREE_MIB
                or cuda_free_after < _GPU_CRITICAL_FREE_MIB * 1024**2
            ):
                pytest.fail(
                    "GPU_MEMORY_SAFETY_MARGIN_FAILED", pytrace=False
                )
            after_counts = {
                name: counts[name] for name in _COUNTED_STAGE_CALLS
            }
            call_deltas = {
                name: after_counts[name] - before_counts[name]
                for name in _COUNTED_STAGE_CALLS
            }
            stages[label] = {
                "requested_outputs": requested_outputs,
                "status": result.status,
                "completed_outputs": tuple(result.completed_outputs),
                "failed_outputs": tuple(result.failed_outputs),
                "actual_runtime_parameters": dict(_RUNTIME_PARAMETERS),
                "image_role": result.images[0]["image_role"],
                "image_requested_output": result.images[0][
                    "requested_output"
                ],
                "image_statistics": image_statistics,
                "raw_image": image,
                "result_data": dict(result.data),
                "performance": performance,
                "complete_inference_seconds": complete_seconds,
                "sem_generation_seconds": sem_timings[label],
                "mechanical_prediction_seconds": mechanical_timings.get(
                    label
                ),
                "resources": sampled_resources,
                "call_deltas": call_deltas,
                "result_object": result,
            }
            print(
                "P1B2_GPU_STAGE_COMPLETE=%s seconds=%.6f"
                % (label, complete_seconds),
                flush=True,
            )

        if counts != {
            "engine_execute_calls": 4,
            "sem_generation_calls": 4,
            "ddpm_sampling_calls": 4,
            "mechanical_prediction_calls": 3,
            "densenet_forward_calls": 3,
            "yield_predict_calls": 3,
            "elongation_predict_calls": 3,
            "fit_calls": 0,
            "score_calls": 0,
            "backward_calls": 0,
            "optimizer_calls": 0,
        }:
            pytest.fail("P1B2_CALL_COUNTS_INVALID", pytrace=False)
        if budget.summary() != {
            "maximum": 4,
            "reserved": 4,
            "retries": 0,
            "fifth_attempted": False,
        }:
            pytest.fail(
                "P1B2_GPU_INFERENCE_BUDGET_INVALID", pytrace=False
            )

        consistency = {
            "a_vs_c_sem": _image_comparison(
                stages["A"]["raw_image"],
                stages["C"]["raw_image"],
                np,
            ),
            "b_vs_c_mechanical": _mechanical_comparison(
                stages["B"], stages["C"]
            ),
            "c_vs_d_sem": _image_comparison(
                stages["C"]["raw_image"],
                stages["D"]["raw_image"],
                np,
            ),
            "c_vs_d_mechanical": _mechanical_comparison(
                stages["C"], stages["D"]
            ),
        }
        timing_statistics = {
            "sem_generation": _timing_statistics(
                stages[label]["sem_generation_seconds"]
                for label in ("A", "B", "C", "D")
            ),
            "mechanical_prediction": _timing_statistics(
                stages[label]["mechanical_prediction_seconds"]
                for label in ("B", "C", "D")
            ),
            "warm_complete_inference": _timing_statistics(
                stages[label]["complete_inference_seconds"]
                for label in ("B", "C", "D")
            ),
        }
        payload, npy_payload = _payload_validation(
            stages["C"],
            _PROCESS_PARAMETERS,
            _RUNTIME_PARAMETERS,
        )
        if (
            not payload["allow_pickle_false_roundtrip"]
            or not payload["array_exact_equal"]
            or not payload["base64_bytes_exact_equal"]
            or not payload["sha256_matches_runtime_payload"]
            or payload["json_margin_bytes"] <= 0
        ):
            pytest.fail("P1B2_PAYLOAD_VALIDATION_FAILED", pytrace=False)
    except _P1B2ControlledLoadFailure as error:
        controlled_load_failure = str(error)
    finally:
        inference_module.ModelBundleLoader.load = original_bundle_load
        torch.Tensor.backward = original_backward
        torch.optim.Optimizer.step = original_optimizer_step
        torch.nn.Module.to = original_module_to
        torch.Tensor.to = original_tensor_to
        if originals:
            engine.execute = originals["engine_execute"]
            components.generate_sem = originals["generate_sem"]
            components.predict_mechanical = originals[
                "predict_mechanical"
            ]
            if components._bundle is not None:
                components._bundle.densenet.forward = originals[
                    "densenet_forward"
                ]
                components._bundle.yield_model.predict = originals[
                    "yield_predict"
                ]
                components._bundle.elongation_model.predict = originals[
                    "elongation_predict"
                ]
                components._bundle.yield_model.fit = originals["yield_fit"]
                components._bundle.elongation_model.fit = originals[
                    "elongation_fit"
                ]
                components._bundle.yield_model.score = originals[
                    "yield_score"
                ]
                components._bundle.elongation_model.score = originals[
                    "elongation_score"
                ]
        cleanup_engine()

    if controlled_load_failure is not None:
        pytest.fail(controlled_load_failure, pytrace=False)
    if (
        not cleanup_state["closed"]
        or cleanup_state["failure_code"] is not None
        or engine.is_loaded()
    ):
        pytest.fail("P1B2_ENGINE_RELEASE_FAILED", pytrace=False)
    final_host = _host_memory()
    final_gpu = _gpu_snapshot()
    final_release = {
        "host_free_bytes": final_host["free_bytes"],
        "gpu_used_mib": final_gpu["used_mib"],
        "gpu_free_mib": final_gpu["free_mib"],
        "gpu_compute_process_count_inside_test_process": (
            _gpu_compute_process_count()
        ),
    }
    safe_summary = {
        "run_id": run_id,
        "status": "SUCCEEDED",
        "dual_authorization_verified": True,
        "process_parameters": dict(_PROCESS_PARAMETERS),
        "runtime_parameters": dict(_RUNTIME_PARAMETERS),
        "preflight": preflight,
        "engine": engine_summary,
        "inference_budget": budget.summary(),
        "counts": counts,
        "stages": {
            label: _safe_stage_summary(stages[label])
            for label in ("A", "B", "C", "D")
        },
        "consistency": consistency,
        "timing_statistics": timing_statistics,
        "payload": payload,
        "final_release": final_release,
        "production_code_modified": False,
        "retry_executed": False,
    }
    artifacts = _build_artifacts(
        repository_root,
        run_id,
        stages["C"]["raw_image"],
        npy_payload,
        safe_summary,
        np,
    )
    emitted_summary = dict(safe_summary)
    emitted_summary["artifacts"] = {
        "display_directory": artifacts["display_directory"],
        "files": artifacts["files"],
        "preview": artifacts["preview"],
    }
    print(
        "P1B2_GPU_INFERENCE_SUMMARY="
        + json.dumps(
            emitted_summary,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return {
        "authorization": {
            "real_model": _REAL_MODEL_AUTHORIZATION,
            "gpu": _GPU_AUTHORIZATION,
            "environment": _MODEL_ENVIRONMENT,
        },
        "process_parameters": dict(_PROCESS_PARAMETERS),
        "runtime_parameters": dict(_RUNTIME_PARAMETERS),
        "preflight": preflight,
        "engine": engine_summary,
        "inference_budget": budget.summary(),
        "counts": counts,
        "stages": stages,
        "consistency": consistency,
        "timing_statistics": timing_statistics,
        "payload": payload,
        "final_release": final_release,
        "artifacts": artifacts,
    }


@pytest.fixture(scope="session")
def p1b2_gpu_inference_session():
    failures = _authorization_failures(
        os.environ,
        sys.version_info[:2],
        require_gpu=True,
    )
    if failures:
        pytest.skip(
            "P1B2 real-model GPU acceptance is not authorized; "
            "missing or invalid gates: %s." % ", ".join(failures)
        )
    host_start = _host_memory()
    if host_start["free_bytes"] < _HOST_PREFLIGHT_BYTES:
        pytest.fail("HOST_MEMORY_PREFLIGHT_FAILED", pytrace=False)
    try:
        gpu_start = _gpu_resource_preflight()
    except RuntimeError:
        pytest.fail("GPU_RESOURCE_PREFLIGHT_FAILED", pytrace=False)
    model_root = Path(os.environ["ZTA35G_MODEL_ROOT"])
    return _run_p1b2_gpu_inference_session(
        model_root,
        host_start,
        gpu_start,
    )
