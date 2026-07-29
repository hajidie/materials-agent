import ast
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

import materialsagent_zta35g_runtime.inference as inference_module
from materialsagent_zta35g_runtime.inference import (
    EngineLoadError,
    InferenceNotLoadedError,
    InvalidModelOutputError,
    MechanicalPredictionError,
    SemGenerationError,
    TorchZTA35GComponents,
    ZTA35GInferenceEngine,
)


PROCESS = {
    "solution_temperature": 1000,
    "solution_time": 3.0,
    "aging_temperature": 730,
    "aging_time": 3.0,
}
RUNTIME = {
    "seed": 987654321,
    "num_samples": 1,
    "guide_scale": 2.0,
    "timesteps": 1000,
}


class _FakeComponents:
    model_bundle_id = "zta35g-sem-original-bundle"

    def __init__(self):
        self.load_count = 0
        self.generate_count = 0
        self.predict_count = 0
        self.close_count = 0
        self.generate_error = None
        self.predict_error = None
        self.image = np.linspace(
            -1.0, 1.0, 512 * 512, dtype="<f4"
        ).reshape(512, 512)
        self.performance = (650.5, 3.25)
        self.generated_arguments = []

    def load(self):
        self.load_count += 1

    def generate_sem(
        self,
        process_parameters,
        seed,
        num_samples,
        guide_scale,
        timesteps,
    ):
        self.generate_count += 1
        self.generated_arguments.append(
            (
                dict(process_parameters),
                seed,
                num_samples,
                guide_scale,
                timesteps,
            )
        )
        if self.generate_error is not None:
            raise self.generate_error
        return self.image

    def predict_mechanical(self, process_parameters, image):
        self.predict_count += 1
        assert image is self.image
        if self.predict_error is not None:
            raise self.predict_error
        return self.performance

    def close(self):
        self.close_count += 1


def _engine(components=None):
    resolved = components or _FakeComponents()
    return ZTA35GInferenceEngine(resolved), resolved


def _decode_image(image):
    npy_bytes = base64.b64decode(image["data_base64"], validate=True)
    array = np.load(BytesIO(npy_bytes), allow_pickle=False)
    return array, npy_bytes


def test_load_is_idempotent_and_close_is_idempotent():
    engine, components = _engine()

    engine.load()
    engine.load()
    assert engine.is_loaded() is True
    assert components.load_count == 1

    engine.close()
    engine.close()
    assert engine.is_loaded() is False
    assert components.close_count == 1


def test_execute_before_load_is_rejected_without_component_calls():
    engine, components = _engine()

    with pytest.raises(InferenceNotLoadedError):
        engine.execute(PROCESS, ("sem_image",), RUNTIME)

    assert components.generate_count == 0


@pytest.mark.parametrize(
    ("outputs", "role", "requested", "prediction_count", "data_fields"),
    [
        (("sem_image",), "generated_sem", True, 0, set()),
        (
            ("mechanical_properties",),
            "intermediate_sem",
            False,
            1,
            {"yield_strength", "elongation"},
        ),
        (
            ("sem_image", "mechanical_properties"),
            "generated_sem",
            True,
            1,
            {"yield_strength", "elongation"},
        ),
    ],
)
def test_three_output_modes_generate_once_and_reuse_same_image(
    outputs,
    role,
    requested,
    prediction_count,
    data_fields,
):
    engine, components = _engine()
    engine.load()

    result = engine.execute(PROCESS, outputs, RUNTIME)

    assert result.status == "SUCCEEDED"
    assert result.completed_outputs == outputs
    assert result.failed_outputs == ()
    assert components.generate_count == 1
    assert components.predict_count == prediction_count
    assert set(result.data) == data_fields
    assert result.data.get("yield_strength", {}).get("unit") in (None, "MPa")
    assert result.data.get("elongation", {}).get("unit") in (None, "%")
    assert len(result.images) == 1
    assert result.images[0]["image_role"] == role
    assert result.images[0]["requested_output"] is requested
    array, npy_bytes = _decode_image(result.images[0])
    assert array.shape == (512, 512)
    assert array.dtype.str == "<f4"
    assert len(npy_bytes) < 4 * 1024 * 1024
    assert result.model_bundle_id == "zta35g-sem-original-bundle"
    assert [item["step"] for item in result.diagnostics] == (
        ["sem_generation"]
        if prediction_count == 0
        else ["sem_generation", "mechanical_property_prediction"]
    )


def test_explicit_seed_and_fixed_runtime_parameters_are_forwarded():
    engine, components = _engine()
    engine.load()

    engine.execute(PROCESS, ("sem_image",), RUNTIME)

    assert components.generated_arguments == [
        (PROCESS, 987654321, 1, 2.0, 1000)
    ]


@pytest.mark.parametrize("model_bundle_id", ["", None, "other-bundle"])
def test_engine_does_not_replace_component_bundle_identity(model_bundle_id):
    engine, components = _engine()
    components.model_bundle_id = model_bundle_id

    assert engine.model_bundle_id == model_bundle_id


def test_performance_failure_is_partial_when_sem_was_requested():
    engine, components = _engine()
    components.predict_error = MechanicalPredictionError()
    engine.load()

    result = engine.execute(
        PROCESS,
        ("sem_image", "mechanical_properties"),
        RUNTIME,
    )

    assert result.status == "PARTIALLY_SUCCEEDED"
    assert result.completed_outputs == ("sem_image",)
    assert result.failed_outputs == ("mechanical_properties",)
    assert result.data == {}
    assert len(result.images) == 1
    assert result.images[0]["image_role"] == "generated_sem"
    assert result.error["code"] == "MECHANICAL_PROPERTY_PREDICTION_FAILED"
    assert len(result.warnings) == 1
    assert len(result.warnings) <= 20
    assert result.diagnostics[-1]["status"] == "FAILED"


def test_mechanical_only_failure_is_failed_but_keeps_intermediate_image():
    engine, components = _engine()
    components.predict_error = MechanicalPredictionError()
    engine.load()

    result = engine.execute(PROCESS, ("mechanical_properties",), RUNTIME)

    assert result.status == "FAILED"
    assert result.completed_outputs == ()
    assert result.failed_outputs == ("mechanical_properties",)
    assert result.data == {}
    assert len(result.images) == 1
    assert result.images[0]["image_role"] == "intermediate_sem"
    assert result.images[0]["requested_output"] is False


def test_sem_failure_stops_before_performance_prediction():
    engine, components = _engine()
    components.generate_error = SemGenerationError()
    engine.load()

    result = engine.execute(
        PROCESS,
        ("sem_image", "mechanical_properties"),
        RUNTIME,
    )

    assert result.status == "FAILED"
    assert result.completed_outputs == ()
    assert result.failed_outputs == ("sem_image", "mechanical_properties")
    assert result.images == ()
    assert components.predict_count == 0
    assert result.error["code"] == "SEM_GENERATION_FAILED"
    assert [item["step"] for item in result.diagnostics] == [
        "sem_generation"
    ]


def test_nonfinite_sampling_failure_is_not_repaired_or_sent_to_performance():
    engine, components = _engine()
    components.generate_error = InvalidModelOutputError()
    engine.load()

    result = engine.execute(
        PROCESS,
        ("sem_image", "mechanical_properties"),
        RUNTIME,
    )

    assert result.status == "FAILED"
    assert result.completed_outputs == ()
    assert result.failed_outputs == (
        "sem_image",
        "mechanical_properties",
    )
    assert result.error["code"] == "INVALID_MODEL_OUTPUT"
    assert result.images == ()
    assert result.data == {}
    assert components.predict_count == 0


def test_production_inference_source_never_silently_repairs_nonfinite_values():
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "materialsagent_zta35g_runtime"
        / "inference.py"
    ).read_text(encoding="utf-8")

    assert "torch.nan_to_num" not in source
    assert "np.nan_to_num" not in source


def test_production_sampling_loop_checks_nonfinite_values_after_every_step():
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "materialsagent_zta35g_runtime"
        / "inference.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    components_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "TorchZTA35GComponents"
    )
    generate_sem = next(
        node
        for node in components_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_sem"
    )
    sampling_loops = [
        node
        for node in ast.walk(generate_sem)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Call)
        and isinstance(node.iter.func, ast.Name)
        and node.iter.func.id == "range"
    ]

    assert len(sampling_loops) == 1
    sampling_loop = sampling_loops[0]
    assert isinstance(sampling_loop.body[-2], ast.Assign)
    assert ast.unparse(sampling_loop.body[-2].targets[0]) == "values"
    guard = sampling_loop.body[-1]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == (
        "torch.isnan(values).any() or torch.isinf(values).any()"
    )
    assert guard.orelse == []
    assert len(guard.body) == 1
    assert isinstance(guard.body[0], ast.Raise)
    assert ast.unparse(guard.body[0].exc) == "InvalidModelOutputError()"


@pytest.mark.parametrize(
    "image",
    [
        np.zeros((511, 512), dtype="<f4"),
        np.zeros((512, 512), dtype="<f8"),
        np.full((512, 512), np.nan, dtype="<f4"),
        np.full((512, 512), np.inf, dtype="<f4"),
        np.full((512, 512), 1.1, dtype="<f4"),
        np.asfortranarray(np.zeros((512, 512), dtype="<f4")),
    ],
    ids=(
        "shape",
        "dtype",
        "nan",
        "inf",
        "range",
        "fortran",
    ),
)
def test_invalid_image_output_fails_safely_and_skips_performance(image):
    engine, components = _engine()
    components.image = image
    engine.load()

    result = engine.execute(
        PROCESS,
        ("sem_image", "mechanical_properties"),
        RUNTIME,
    )

    assert result.status == "FAILED"
    assert result.images == ()
    assert result.data == {}
    assert result.error["code"] == "INVALID_MODEL_OUTPUT"
    assert components.predict_count == 0


@pytest.mark.parametrize(
    "performance",
    [
        (True, 3.2),
        (650.0, float("nan")),
        (float("inf"), 3.2),
        ("650", 3.2),
    ],
)
def test_invalid_performance_output_uses_bounded_failure(performance):
    engine, components = _engine()
    components.performance = performance
    engine.load()

    result = engine.execute(
        PROCESS,
        ("sem_image", "mechanical_properties"),
        RUNTIME,
    )

    assert result.status == "PARTIALLY_SUCCEEDED"
    assert result.error["code"] == "MECHANICAL_PROPERTY_PREDICTION_FAILED"
    assert result.data == {}
    assert len(result.warnings) == 1


class _FakeSchedule:
    def to(self, _device):
        return self

    def __rsub__(self, _value):
        return self


class _FakeDeviceModel:
    def __init__(self, fail_on_to=False):
        self.fail_on_to = fail_on_to

    def to(self, _device):
        if self.fail_on_to:
            raise RuntimeError("device transfer failed")
        return self

    def eval(self):
        return self


class _FakeBundle:
    model_bundle_id = "zta35g-sem-original-bundle"

    def __init__(self, failure_stage):
        self.ddpm = _FakeDeviceModel(failure_stage == "ddpm_to")
        self.densenet = _FakeDeviceModel(
            failure_stage == "densenet_to"
        )
        self.yield_model = object()
        self.elongation_model = object()
        self.close_count = 0

    def close(self):
        self.close_count += 1


@pytest.mark.parametrize(
    ("failure_stage", "expected_bundle_close_count"),
    [
        ("bundle_deserialization", 0),
        ("ddpm_to", 1),
        ("densenet_to", 1),
        ("beta_initialization", 1),
    ],
)
def test_real_components_load_failure_clears_references_and_does_not_retry(
    monkeypatch,
    tmp_path,
    failure_stage,
    expected_bundle_close_count,
):
    bundle = _FakeBundle(failure_stage)
    load_count = []

    class _Loader:
        def __init__(self, _model_root):
            pass

        def load(self):
            load_count.append("load")
            if failure_stage == "bundle_deserialization":
                raise RuntimeError("deserialization failed")
            return bundle

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
    fake_torch.device = lambda kind: kind
    fake_torch.cumprod = lambda values, dim: values
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(inference_module, "ModelBundleLoader", _Loader)

    def _schedule(_torch, _timesteps):
        if failure_stage == "beta_initialization":
            raise RuntimeError("beta initialization failed")
        return _FakeSchedule()

    monkeypatch.setattr(
        inference_module,
        "cosine_beta_schedule",
        _schedule,
    )
    components = TorchZTA35GComponents(tmp_path)
    engine = ZTA35GInferenceEngine(components)

    with pytest.raises(EngineLoadError):
        engine.load()
    with pytest.raises(EngineLoadError):
        engine.load()
    with pytest.raises(InferenceNotLoadedError):
        engine.execute(PROCESS, ("sem_image",), RUNTIME)
    engine.close()

    assert load_count == ["load"]
    assert bundle.close_count == expected_bundle_close_count
    assert engine.is_loaded() is False
    assert components.device_kind == "unknown"
    assert components._bundle is None
    assert components._torch is None
    assert components._numpy is None
    assert components._device is None
    assert components._betas is None
    assert components._alphas is None
    assert components._alphas_cumprod is None


def test_importing_inference_in_clean_python_does_not_import_heavy_modules():
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
        "import json, sys\n"
        "import materialsagent_zta35g_runtime.inference\n"
        "forbidden = {'torch', 'torchvision', 'joblib', "
        "'sklearn', 'matplotlib'}\n"
        "print(json.dumps(sorted(forbidden.intersection(sys.modules))))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(completed.stdout) == []
    assert completed.stderr == ""
