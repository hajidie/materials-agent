from pathlib import Path


def test_real_payload_is_within_contract_under_p1b2_authorization(
    p1b2_gpu_inference_session,
):
    session = p1b2_gpu_inference_session
    payload = session["payload"]

    assert payload["source_stage"] == "C"
    assert payload["allow_pickle_false_roundtrip"] is True
    assert payload["array_exact_equal"] is True
    assert payload["base64_bytes_exact_equal"] is True
    assert payload["sha256_matches_runtime_payload"] is True
    assert payload["npy_bytes"] == payload["base64_decoded_bytes"]
    assert payload["base64_characters"] > payload["npy_bytes"]
    assert payload["json_estimate_bytes"] < 4 * 1024 * 1024
    assert payload["json_margin_bytes"] > 0
    assert payload["serialization_seconds"] > 0.0
    assert payload["base64_encode_seconds"] > 0.0
    assert payload["base64_decode_seconds"] > 0.0

    for label in ("A", "B", "C", "D"):
        resources = session["stages"][label]["resources"]
        assert resources["memory_allocated_before"] >= 0
        assert resources["memory_reserved_before"] >= 0
        assert resources["max_memory_allocated"] > 0
        assert resources["max_memory_reserved"] > 0
        assert resources["memory_allocated_after"] >= 0
        assert resources["memory_reserved_after"] >= 0
        assert resources["torch_cuda_free_after"] >= 400 * 1024 * 1024
        assert resources["nvidia_smi_peak_used_mib"] > 0
        assert resources["nvidia_smi_minimum_free_mib"] >= 400
        assert resources["host_minimum_free_bytes"] >= 2 * 1024**3
        assert session["stages"][label]["complete_inference_seconds"] > 0.0

    timing_statistics = session["timing_statistics"]
    assert timing_statistics["sem_generation"]["sample_count"] == 4
    assert timing_statistics["mechanical_prediction"]["sample_count"] == 3
    assert timing_statistics["warm_complete_inference"]["sample_count"] == 3
    for group in timing_statistics.values():
        assert group["minimum"] > 0.0
        assert group["maximum"] >= group["minimum"]
        assert group["mean"] > 0.0
        assert group["median_p50"] > 0.0
        assert len(group["raw_timings"]) == group["sample_count"]
        assert group["p95"] == "insufficient_samples"
        assert group["p99"] == "insufficient_samples"

    preflight = session["preflight"]
    assert preflight["host_total_bytes"] > 0
    assert preflight["host_start_free_bytes"] >= 8 * 1024**3
    assert preflight["host_preload_free_bytes"] >= 8 * 1024**3
    assert preflight["gpu_name"] == "NVIDIA GeForce RTX 3060 Laptop GPU"
    assert preflight["compute_capability"] == "8.6"
    assert preflight["gpu_total_mib"] == 6144
    assert preflight["gpu_start_free_mib"] >= 5500
    assert preflight["gpu_compute_process_count"] == 0

    artifacts = session["artifacts"]
    artifact_directory = Path(artifacts["absolute_directory"])
    assert artifacts["display_directory"].startswith(
        "tmp/p1b2-gpu-inference/"
    )
    assert artifact_directory.is_dir()
    assert set(artifacts["files"]) == {
        "summary.json",
        "sem-image-seed-20260730.npy",
        "sem-image-seed-20260730-preview.png",
        "artifact-manifest.json",
    }
    assert artifacts["preview"]["review_only_preview"] is True
    assert artifacts["preview"]["mode"] == "L"
    assert artifacts["preview"]["size"] == [512, 512]
    assert artifacts["preview"]["has_alpha"] is False
    for file_name, metadata in artifacts["files"].items():
        path = artifact_directory / file_name
        assert path.is_file()
        assert path.stat().st_size == metadata["bytes"]
        assert len(metadata["sha256"]) == 64

    assert session["authorization"] == {
        "real_model": "P1B2_PROJECT_OWNER_AUTHORIZED",
        "gpu": "P1B2_GPU_AUTHORIZED",
        "environment": "materialsagent-zta35g",
    }
