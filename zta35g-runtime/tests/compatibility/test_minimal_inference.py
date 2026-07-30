import math

import numpy as np
import pytest


EXPECTED_PROCESS_PARAMETERS = {
    "solution_temperature": 1000,
    "solution_time": 3.0,
    "aging_temperature": 730,
    "aging_time": 3.0,
}
EXPECTED_RUNTIME_PARAMETERS = {
    "seed": 20260730,
    "num_samples": 1,
    "guide_scale": 2.0,
    "timesteps": 1000,
}
EXPECTED_REQUESTS = {
    "A": ("sem_image",),
    "B": ("mechanical_properties",),
    "C": ("sem_image", "mechanical_properties"),
    "D": ("sem_image", "mechanical_properties"),
}


class _FakeSampler:
    def __init__(self, summary=None, stop_error=None):
        self.start_calls = 0
        self.stop_calls = 0
        self.summary = summary or {"sample_count": 1}
        self.stop_error = stop_error

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        return dict(self.summary)


class _FakeEngine:
    def __init__(self, load_error=None):
        self.load_calls = 0
        self.close_calls = 0
        self.load_error = load_error

    def load(self):
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error

    def close(self):
        self.close_calls += 1


def _fake_cleanup(engine, calls):
    def cleanup():
        engine.close()
        calls["gc_collect"] += 1
        calls["cuda_empty_cache"] += 1

    return cleanup


def test_inference_budget_rejects_fifth_before_delegate(
    p1b2_gpu_offline_harness,
):
    budget = p1b2_gpu_offline_harness["InferenceBudget"](4)
    delegate_calls = 0

    def reserve_then_delegate():
        nonlocal delegate_calls
        budget.reserve_before_delegate()
        delegate_calls += 1

    for _ in range(4):
        reserve_then_delegate()

    with pytest.raises(
        RuntimeError,
        match="^P1B2_GPU_INFERENCE_BUDGET_EXCEEDED$",
    ):
        reserve_then_delegate()

    assert delegate_calls == 4
    assert budget.summary() == {
        "maximum": 4,
        "reserved": 4,
        "retries": 0,
        "fifth_attempted": True,
    }


def test_load_sampler_stops_once_when_engine_load_succeeds(
    p1b2_gpu_offline_harness,
):
    engine = _FakeEngine()
    sampler = _FakeSampler(summary={"host_sample_count": 3})
    cleanup_calls = {"gc_collect": 0, "cuda_empty_cache": 0}
    synchronize_calls = []

    resources = p1b2_gpu_offline_harness["load_engine_with_sampler"](
        engine=engine,
        sampler=sampler,
        synchronize=lambda: synchronize_calls.append(True),
        oom_detected=lambda: False,
        cleanup=_fake_cleanup(engine, cleanup_calls),
    )

    assert resources == {"host_sample_count": 3}
    assert sampler.start_calls == 1
    assert engine.load_calls == 1
    assert sampler.stop_calls == 1
    assert synchronize_calls == [True]
    assert engine.close_calls == 0
    assert cleanup_calls == {"gc_collect": 0, "cuda_empty_cache": 0}


def test_load_sampler_stops_once_and_cleans_up_on_ordinary_failure(
    p1b2_gpu_offline_harness,
):
    engine = _FakeEngine(load_error=RuntimeError("ordinary raw failure"))
    sampler = _FakeSampler()
    cleanup_calls = {"gc_collect": 0, "cuda_empty_cache": 0}

    with pytest.raises(
        RuntimeError,
        match="^P1B2_ENGINE_LOAD_FAILED$",
    ) as error:
        p1b2_gpu_offline_harness["load_engine_with_sampler"](
            engine=engine,
            sampler=sampler,
            synchronize=lambda: None,
            oom_detected=lambda: False,
            cleanup=_fake_cleanup(engine, cleanup_calls),
        )

    assert "ordinary raw failure" not in str(error.value)
    assert sampler.start_calls == 1
    assert engine.load_calls == 1
    assert sampler.stop_calls == 1
    assert engine.close_calls == 1
    assert cleanup_calls == {"gc_collect": 1, "cuda_empty_cache": 1}


def test_load_oom_maps_wrapped_failure_without_raw_exception(
    p1b2_gpu_offline_harness,
):
    engine = _FakeEngine(
        load_error=RuntimeError("wrapped engine load with private CUDA text")
    )
    sampler = _FakeSampler()
    cleanup_calls = {"gc_collect": 0, "cuda_empty_cache": 0}

    with pytest.raises(
        RuntimeError,
        match="^CUDA_OUT_OF_MEMORY$",
    ) as error:
        p1b2_gpu_offline_harness["load_engine_with_sampler"](
            engine=engine,
            sampler=sampler,
            synchronize=lambda: None,
            oom_detected=lambda: True,
            cleanup=_fake_cleanup(engine, cleanup_calls),
        )

    assert "private CUDA text" not in str(error.value)
    assert sampler.start_calls == 1
    assert engine.load_calls == 1
    assert sampler.stop_calls == 1
    assert engine.close_calls == 1
    assert cleanup_calls == {"gc_collect": 1, "cuda_empty_cache": 1}


def test_load_sampler_stops_once_when_controlled_base_exception_escapes(
    p1b2_gpu_offline_harness,
):
    controlled_failure = pytest.fail.Exception("CONTROLLED_TEST_FAILURE")
    engine = _FakeEngine(load_error=controlled_failure)
    sampler = _FakeSampler()

    with pytest.raises(pytest.fail.Exception, match="CONTROLLED_TEST_FAILURE"):
        p1b2_gpu_offline_harness["load_engine_with_sampler"](
            engine=engine,
            sampler=sampler,
            synchronize=lambda: None,
            oom_detected=lambda: False,
            cleanup=lambda: None,
        )

    assert sampler.start_calls == 1
    assert engine.load_calls == 1
    assert sampler.stop_calls == 1


def test_sampler_shutdown_rejects_lingering_workers(
    p1b2_gpu_offline_harness,
):
    class FakeAlive:
        def __init__(self):
            self.join_calls = 0

        def join(self, timeout):
            assert timeout == 5
            self.join_calls += 1

        def is_alive(self):
            return True

    alive_thread = FakeAlive()
    sampler = p1b2_gpu_offline_harness["ResourceSampler"]()
    sampler._started = True
    sampler._host_thread = alive_thread

    with pytest.raises(
        RuntimeError,
        match="^P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED$",
    ):
        sampler.stop()
    assert alive_thread.join_calls == 1

    with pytest.raises(
        RuntimeError,
        match="^P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED$",
    ):
        sampler.stop()
    assert alive_thread.join_calls == 1


def test_load_sampler_stop_reuses_first_summary_without_rejoining(
    p1b2_gpu_offline_harness,
):
    class FakeThread:
        def join(self, timeout):
            raise AssertionError("stopped sampler must not join again")

    sampler = p1b2_gpu_offline_harness["ResourceSampler"]()
    sampler._started = True
    sampler._stopped = True
    sampler._stop_summary = {"host_sample_count": 7}
    sampler._host_thread = FakeThread()

    first = sampler.stop()
    second = sampler.stop()

    assert first == {"host_sample_count": 7}
    assert second == first
    assert first is not second


def _assert_valid_image(stage):
    image = stage["raw_image"]
    statistics = stage["image_statistics"]

    assert isinstance(image, np.ndarray)
    assert image.dtype == np.dtype("<f4")
    assert image.shape == (512, 512)
    assert image.ndim == 2
    assert image.flags.c_contiguous
    assert not image.flags.f_contiguous
    assert np.isfinite(image).all()
    assert -1.0 <= statistics["minimum"] <= 1.0
    assert -1.0 <= statistics["maximum"] <= 1.0
    assert math.isfinite(statistics["mean"])
    assert statistics["standard_deviation"] > 0.0
    assert statistics["nan_count"] == 0
    assert statistics["inf_count"] == 0
    assert statistics["all_constant"] is False


def _assert_valid_performance(stage):
    performance = stage["performance"]

    assert math.isfinite(performance["yield_strength"])
    assert math.isfinite(performance["elongation"])
    assert performance["yield_strength"] > 0.0
    assert performance["elongation"] > 0.0
    assert performance["yield_strength_unit"] == "MPa"
    assert performance["elongation_unit"] == "%"
    assert performance["clipping_applied"] is False


def test_real_fixed_parameter_minimal_inference_under_p1b2_authorization(
    p1b2_gpu_inference_session,
):
    session = p1b2_gpu_inference_session

    assert session["process_parameters"] == EXPECTED_PROCESS_PARAMETERS
    assert session["runtime_parameters"] == EXPECTED_RUNTIME_PARAMETERS
    assert session["engine"] == {
        **session["engine"],
        "model_bundle_id": "zta35g-sem-original-bundle",
        "engine_device": "cuda",
        "ddpm_device": "cuda:0",
        "densenet_device": "cuda:0",
        "svr_device": "cpu",
        "engine_load_calls": 1,
        "bundle_load_calls": 1,
        "gpu_move_stages": 1,
    }
    assert session["engine"]["cpu_load_seconds"] > 0.0
    assert session["engine"]["gpu_move_seconds"] > 0.0

    for label, requested_outputs in EXPECTED_REQUESTS.items():
        stage = session["stages"][label]
        assert stage["requested_outputs"] == requested_outputs
        assert stage["status"] == "SUCCEEDED"
        assert stage["completed_outputs"] == requested_outputs
        assert stage["failed_outputs"] == ()
        assert stage["actual_runtime_parameters"] == EXPECTED_RUNTIME_PARAMETERS
        _assert_valid_image(stage)

    assert session["stages"]["A"]["result_data"] == {}
    assert session["stages"]["A"]["image_role"] == "generated_sem"
    assert session["stages"]["A"]["image_requested_output"] is True
    assert session["stages"]["A"]["call_deltas"] == {
        "sem_generation_calls": 1,
        "ddpm_sampling_calls": 1,
        "mechanical_prediction_calls": 0,
        "densenet_forward_calls": 0,
        "yield_predict_calls": 0,
        "elongation_predict_calls": 0,
    }

    assert session["stages"]["B"]["image_role"] == "intermediate_sem"
    assert session["stages"]["B"]["image_requested_output"] is False
    assert session["stages"]["B"]["call_deltas"] == {
        "sem_generation_calls": 1,
        "ddpm_sampling_calls": 1,
        "mechanical_prediction_calls": 1,
        "densenet_forward_calls": 1,
        "yield_predict_calls": 1,
        "elongation_predict_calls": 1,
    }
    _assert_valid_performance(session["stages"]["B"])

    for label in ("C", "D"):
        assert session["stages"][label]["image_role"] == "generated_sem"
        assert session["stages"][label]["image_requested_output"] is True
        assert session["stages"][label]["call_deltas"] == {
            "sem_generation_calls": 1,
            "ddpm_sampling_calls": 1,
            "mechanical_prediction_calls": 1,
            "densenet_forward_calls": 1,
            "yield_predict_calls": 1,
            "elongation_predict_calls": 1,
        }
        _assert_valid_performance(session["stages"][label])

    assert session["counts"] == {
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
    }
    assert session["inference_budget"] == {
        "maximum": 4,
        "reserved": 4,
        "retries": 0,
        "fifth_attempted": False,
    }

    consistency = session["consistency"]
    for comparison_name in ("a_vs_c_sem", "c_vs_d_sem"):
        comparison = consistency[comparison_name]
        assert isinstance(comparison["exact_equal"], bool)
        assert isinstance(comparison["allclose"], bool)
        assert comparison["max_abs_diff"] >= 0.0
        assert comparison["mean_abs_diff"] >= 0.0
    for comparison_name in ("b_vs_c_mechanical", "c_vs_d_mechanical"):
        comparison = consistency[comparison_name]
        assert comparison["yield_absolute_diff"] >= 0.0
        assert comparison["elongation_absolute_diff"] >= 0.0
    repeated = consistency["c_vs_d_mechanical"]
    assert isinstance(repeated["yield_exact_equal"], bool)
    assert isinstance(repeated["elongation_exact_equal"], bool)
