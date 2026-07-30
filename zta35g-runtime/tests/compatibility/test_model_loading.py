import ctypes
from copy import deepcopy
from collections import OrderedDict
import gc
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import weakref

import pytest


_GIB = 1024 ** 3
_CRITICAL_FREE_MEMORY_BYTES = 2 * _GIB
_EXPECTED_INPUT_FEATURES = 3076


class _FakeTensor:
    def __init__(self, shape, dtype="float32", value=0):
        self.shape = tuple(shape)
        self.dtype = dtype
        self.device = SimpleNamespace(type="cpu")
        self._value = value

    def item(self):
        return self._value


class _FakeBatchNorm:
    @staticmethod
    def _load_from_state_dict(
        _self,
        state_dict,
        prefix,
        _local_metadata,
        _strict,
        _missing_keys,
        _unexpected_keys,
        _error_messages,
    ):
        state_dict[prefix + "num_batches_tracked"] = _FakeTensor(
            (),
            dtype="int64",
            value=0,
        )


_FAKE_TORCH = SimpleNamespace(
    Tensor=_FakeTensor,
    int64="int64",
    Size=lambda values: tuple(values),
    nn=SimpleNamespace(
        modules=SimpleNamespace(
            batchnorm=SimpleNamespace(_BatchNorm=_FakeBatchNorm)
        )
    ),
)


class _FakeStrictBatchNormModel:
    def __init__(self, model_state, expected_counters):
        self._model_state = OrderedDict(model_state.items())
        self._expected_counters = set(expected_counters)

    def load_state_dict(self, state_dict, strict):
        assert strict is True
        internal_state = OrderedDict(state_dict.items())
        for counter_key in self._expected_counters:
            prefix = counter_key[: -len("num_batches_tracked")]
            _FakeBatchNorm._load_from_state_dict(
                None,
                internal_state,
                prefix,
                {},
                True,
                [],
                [],
                [],
            )
        self._model_state = internal_state
        return SimpleNamespace(
            missing_keys=(),
            unexpected_keys=(),
        )

    def state_dict(self):
        return self._model_state


class _MemoryStatus(ctypes.Structure):
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


def _physical_memory():
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
        ctypes.byref(status)
    ):
        raise RuntimeError("HOST_MEMORY_MEASUREMENT_FAILED")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


class _MemoryMonitor:
    def __init__(self):
        total, free = _physical_memory()
        self.total_bytes = total
        self.start_free_bytes = free
        self.min_free_bytes = free
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sample,
            name="p1b2-memory-monitor",
            daemon=True,
        )

    def _sample(self):
        while not self._stop.wait(0.05):
            _, free = _physical_memory()
            self.min_free_bytes = min(self.min_free_bytes, free)

    def start(self):
        self._thread.start()

    def observe(self):
        _, free = _physical_memory()
        self.min_free_bytes = min(self.min_free_bytes, free)
        if free < _CRITICAL_FREE_MEMORY_BYTES:
            pytest.fail(
                "HOST_MEMORY_CRITICAL_LOW: free_gib=%.2f"
                % (free / _GIB)
            )
        return free

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.observe()


def _sha256_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _state_source(checkpoint, mapping_type):
    if isinstance(checkpoint.get("ema"), mapping_type):
        return "ema"
    if isinstance(checkpoint.get("model"), mapping_type):
        return "model"
    return "checkpoint_mapping"


def _model_counts(model):
    parameters = tuple(model.parameters())
    buffers = tuple(model.buffers())
    return {
        "parameter_tensor_count": len(parameters),
        "parameter_element_count": sum(
            item.numel() for item in parameters
        ),
        "buffer_tensor_count": len(buffers),
        "buffer_element_count": sum(item.numel() for item in buffers),
    }


def _state_compatibility(model_state, loaded_state, torch):
    model_keys = set(model_state)
    loaded_keys = set(loaded_state)
    shared_keys = model_keys.intersection(loaded_keys)
    shape_mismatches = 0
    dtype_mismatches = 0
    for key in shared_keys:
        model_value = model_state[key]
        loaded_value = loaded_state[key]
        if not isinstance(loaded_value, torch.Tensor):
            shape_mismatches += 1
            continue
        if tuple(model_value.shape) != tuple(loaded_value.shape):
            shape_mismatches += 1
        if model_value.dtype != loaded_value.dtype:
            dtype_mismatches += 1
    return {
        "model_key_count": len(model_keys),
        "checkpoint_key_count": len(loaded_keys),
        "missing_count": len(model_keys - loaded_keys),
        "unexpected_count": len(loaded_keys - model_keys),
        "shape_mismatch_count": shape_mismatches,
        "dtype_mismatch_count": dtype_mismatches,
    }


def _type_name(value):
    value_type = type(value)
    return "%s.%s" % (value_type.__module__, value_type.__name__)


def _estimator_chain(model):
    chain = [model]
    current = model
    seen = {id(model)}
    while True:
        next_value = None
        steps = getattr(current, "steps", None)
        if isinstance(steps, (list, tuple)) and steps:
            next_value = steps[-1][1]
        elif getattr(current, "best_estimator_", None) is not None:
            next_value = current.best_estimator_
        elif getattr(current, "regressor_", None) is not None:
            next_value = current.regressor_
        if next_value is None or id(next_value) in seen:
            return chain
        chain.append(next_value)
        seen.add(id(next_value))
        current = next_value


def _controlled_estimator_children(value):
    children = []
    steps = getattr(value, "steps", None)
    if isinstance(steps, (list, tuple)):
        children.extend(step for _, step in steps)
    for attribute in (
        "regressor_",
        "transformer_",
        "best_estimator_",
    ):
        child = getattr(value, attribute, None)
        if child is not None:
            children.append(child)
    transformers = getattr(value, "transformers_", None)
    if isinstance(transformers, (list, tuple)):
        for item in transformers:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            child = item[1]
            if not isinstance(child, str):
                children.append(child)
    transformer_list = getattr(value, "transformer_list", None)
    if isinstance(transformer_list, (list, tuple)):
        children.extend(child for _, child in transformer_list)
    return tuple(children)


def _is_controlled_pipeline(value):
    value_type = type(value)
    return (
        value_type.__module__.startswith("sklearn.pipeline")
        and value_type.__name__ == "Pipeline"
    )


def _svr_summary(model):
    key_objects = [model]
    index = 0
    while index < len(key_objects):
        value = key_objects[index]
        index += 1
        for child in _controlled_estimator_children(value):
            if all(id(child) != id(item) for item in key_objects):
                key_objects.append(child)
    chain = _estimator_chain(model)
    pipelines = [
        value
        for value in key_objects
        if _is_controlled_pipeline(value)
    ]
    pipeline_steps = (
        [name for name, _ in pipelines[0].steps]
        if pipelines
        else []
    )
    all_pipeline_steps = [
        [name for name, _ in pipeline.steps]
        for pipeline in pipelines
    ]
    controlled_modules = all(
        type(value).__module__.split(".", 1)[0]
        in ("sklearn", "numpy", "joblib")
        for value in key_objects
    )
    feature_dimension = getattr(model, "n_features_in_", None)
    feature_source = "top_level"
    if feature_dimension is None:
        for value in key_objects[1:]:
            feature_dimension = getattr(value, "n_features_in_", None)
            if feature_dimension is not None:
                feature_source = "nested_estimator"
                break
    pca_components = None
    for value in key_objects:
        if type(value).__name__ == "PCA":
            pca_components = getattr(value, "n_components_", None)
            if pca_components is None:
                pca_components = getattr(value, "n_components", None)
            break
    terminal = chain[-1]
    shape_summary = {}
    for attribute in (
        "support_vectors_",
        "support_",
        "dual_coef_",
        "n_support_",
    ):
        value = getattr(terminal, attribute, None)
        shape = getattr(value, "shape", None)
        if shape is not None:
            shape_summary[attribute] = list(shape)
    return {
        "top_level_type": _type_name(model),
        "is_pipeline": _is_controlled_pipeline(model),
        "pipeline_steps": pipeline_steps,
        "all_pipeline_steps": all_pipeline_steps,
        "final_estimator_type": _type_name(terminal),
        "n_features_in": (
            int(feature_dimension)
            if feature_dimension is not None
            else None
        ),
        "n_features_source": feature_source,
        "pca_n_components": (
            int(pca_components)
            if pca_components is not None
            else None
        ),
        "safe_shape_summary": shape_summary,
        "controlled_modules": controlled_modules,
        "predictor_classes": tuple(
            {
                type(value)
                for value in key_objects
                if callable(getattr(type(value), "predict", None))
            }
        ),
        "transformer_classes": tuple(
            {
                type(value)
                for value in key_objects
                if callable(getattr(type(value), "transform", None))
            }
        ),
        "fitter_classes": tuple(
            {
                type(value)
                for value in key_objects
                if callable(getattr(type(value), "fit", None))
            }
        ),
        "scorer_classes": tuple(
            {
                type(value)
                for value in key_objects
                if callable(getattr(type(value), "score", None))
            }
        ),
    }


def _assert_expected_svr_identity(kind, summary):
    expectations = {
        "yield_svr": {
            "error": "YIELD_SVR_IDENTITY_MISMATCH",
            "top_level_type": (
                "sklearn.compose._target.TransformedTargetRegressor"
            ),
            "final_estimator_type": "sklearn.svm._classes.SVR",
            "pipeline_steps": ["prep", "svr"],
            "all_pipeline_steps": [
                ["prep", "svr"],
                ["scaler", "pca"],
            ],
            "n_features_in": 3076,
            "n_features_source": "top_level",
            "pca_n_components": 5,
            "support_vectors_shape": [23, 9],
        },
        "elongation_svr": {
            "error": "ELONGATION_SVR_IDENTITY_MISMATCH",
            "top_level_type": (
                "sklearn.compose._target.TransformedTargetRegressor"
            ),
            "final_estimator_type": "sklearn.svm._classes.SVR",
            "pipeline_steps": ["prep", "svr"],
            "all_pipeline_steps": [["prep", "svr"]],
            "n_features_in": 3076,
            "n_features_source": "top_level",
            "pca_n_components": None,
            "support_vectors_shape": [21, 8],
        },
    }
    expected = expectations.get(kind)
    if expected is None:
        raise AssertionError("SVR_IDENTITY_KIND_INVALID")
    shape_summary = summary.get("safe_shape_summary")
    actual = {
        "top_level_type": summary.get("top_level_type"),
        "final_estimator_type": summary.get(
            "final_estimator_type"
        ),
        "pipeline_steps": summary.get("pipeline_steps"),
        "all_pipeline_steps": summary.get("all_pipeline_steps"),
        "n_features_in": summary.get("n_features_in"),
        "n_features_source": summary.get("n_features_source"),
        "pca_n_components": summary.get("pca_n_components"),
        "support_vectors_shape": (
            shape_summary.get("support_vectors_")
            if isinstance(shape_summary, dict)
            else None
        ),
    }
    expected_values = {
        key: value for key, value in expected.items() if key != "error"
    }
    if (
        summary.get("controlled_modules") is not True
        or actual != expected_values
    ):
        raise AssertionError(expected["error"])
    return True


def _install_forward_canaries(
    monkeypatch,
    module_type,
    direct_forward_types,
    architecture,
    counters,
):
    original_builder = architecture.build_conditional_unet

    def blocked_module_call(_self, *_args, **_kwargs):
        counters["module_call_count"] += 1
        pytest.fail("MODEL_MODULE_CALL_CALLED")

    def blocked_direct_forward(_self, *_args, **_kwargs):
        counters["direct_forward_count"] += 1
        pytest.fail("MODEL_DIRECT_FORWARD_CALLED")

    def guarded_build_conditional_unet(*args, **kwargs):
        model = original_builder(*args, **kwargs)
        monkeypatch.setattr(
            type(model),
            "forward",
            blocked_direct_forward,
        )
        return model

    monkeypatch.setattr(module_type, "__call__", blocked_module_call)
    for direct_forward_type in direct_forward_types:
        monkeypatch.setattr(
            direct_forward_type,
            "forward",
            blocked_direct_forward,
        )
    monkeypatch.setattr(
        architecture,
        "build_conditional_unet",
        guarded_build_conditional_unet,
    )


def _is_cuda_target(torch, args, kwargs):
    candidates = []
    if args:
        candidates.append(args[0])
    if "device" in kwargs:
        candidates.append(kwargs["device"])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.lower().startswith(
            "cuda"
        ):
            return True
        if isinstance(candidate, torch.device):
            if candidate.type == "cuda":
                return True
    return False


def _validated_legacy_batchnorm_counter_set(
    model_state,
    checkpoint_state,
    expected_counters,
    torch,
):
    model_keys = set(model_state)
    checkpoint_keys = set(checkpoint_state)
    missing = model_keys - checkpoint_keys
    compatibility = _state_compatibility(
        model_state,
        checkpoint_state,
        torch,
    )
    expected_counters = set(expected_counters)
    missing_only_counters = all(
        key.endswith(".num_batches_tracked") for key in missing
    )
    missing_equals_expected = missing == expected_counters
    summary = {
        **compatibility,
        "batch_norm_count": len(expected_counters),
        "missing_only_num_batches_tracked": (
            missing_only_counters
        ),
        "missing_set_equals_batch_norm_counter_set": (
            missing_equals_expected
        ),
        "expected_counters": expected_counters,
    }
    if not (
        missing_equals_expected
        and missing_only_counters
        and compatibility["unexpected_count"] == 0
        and compatibility["shape_mismatch_count"] == 0
        and compatibility["dtype_mismatch_count"] == 0
    ):
        raise AssertionError(
            "DENSENET_LEGACY_BATCHNORM_SET_MISMATCH"
        )
    return summary


def _batchnorm_counter_names(model, torch):
    return {
        "%s.num_batches_tracked" % name
        for name, module in model.named_modules()
        if isinstance(module, torch.nn.modules.batchnorm._BatchNorm)
        and module.track_running_stats
    }


def _strict_load_legacy_batchnorm_counters(
    model,
    checkpoint_state,
    expected_counters,
    torch,
):
    state_copy = OrderedDict(checkpoint_state.items())
    caller_counter_count_before = sum(
        key.endswith(".num_batches_tracked") for key in state_copy
    )
    internal_added_counters = set()
    batch_norm_type = torch.nn.modules.batchnorm._BatchNorm
    original_load = batch_norm_type._load_from_state_dict

    def observing_load(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_messages,
    ):
        counter_key = prefix + "num_batches_tracked"
        existed_before = counter_key in state_dict
        result = original_load(
            self,
            state_dict,
            prefix,
            local_metadata,
            strict,
            missing_keys,
            unexpected_keys,
            error_messages,
        )
        if not existed_before and counter_key in state_dict:
            internal_added_counters.add(counter_key)
        return result

    batch_norm_type._load_from_state_dict = observing_load
    try:
        incompatible = model.load_state_dict(
            state_copy,
            strict=True,
        )
    finally:
        batch_norm_type._load_from_state_dict = original_load

    model_state = model.state_dict()
    caller_counter_count_after = sum(
        key.endswith(".num_batches_tracked") for key in state_copy
    )
    counters_are_cpu_int64_scalar_zero = all(
        model_state[key].device.type == "cpu"
        and model_state[key].dtype == torch.int64
        and model_state[key].shape == torch.Size([])
        and model_state[key].item() == 0
        for key in expected_counters
    )
    checkpoint_tensor_shapes_loaded = all(
        isinstance(value, torch.Tensor)
        and tuple(model_state[key].shape) == tuple(value.shape)
        and model_state[key].device.type == "cpu"
        for key, value in checkpoint_state.items()
    )
    summary = {
        "strict_missing_count": len(incompatible.missing_keys),
        "strict_unexpected_count": len(
            incompatible.unexpected_keys
        ),
        "internal_added_counter_count": len(
            internal_added_counters
        ),
        "internal_added_set_equals_expected": (
            internal_added_counters == expected_counters
        ),
        "counters_cpu_int64_scalar_zero": (
            counters_are_cpu_int64_scalar_zero
        ),
        "checkpoint_tensor_shapes_loaded": (
            checkpoint_tensor_shapes_loaded
        ),
        "caller_state_counter_count_before": (
            caller_counter_count_before
        ),
        "caller_state_counter_count_after": (
            caller_counter_count_after
        ),
    }
    if not (
        summary["strict_missing_count"] == 0
        and summary["strict_unexpected_count"] == 0
        and summary["internal_added_set_equals_expected"]
        and summary["counters_cpu_int64_scalar_zero"]
        and summary["checkpoint_tensor_shapes_loaded"]
        and summary["caller_state_counter_count_before"] == 0
        and summary["caller_state_counter_count_after"] == 0
    ):
        raise AssertionError("DENSENET_STRICT_LOAD_POSTCHECK_FAILED")
    return summary


def _tiny_legacy_batchnorm_case():
    expected_counters = {
        "first_bn.num_batches_tracked",
        "second_bn.num_batches_tracked",
    }
    model_state = OrderedDict()
    for prefix in ("first_bn", "second_bn"):
        for field_name in (
            "weight",
            "bias",
            "running_mean",
            "running_var",
        ):
            model_state["%s.%s" % (prefix, field_name)] = (
                _FakeTensor((2,))
            )
        model_state[
            "%s.num_batches_tracked" % prefix
        ] = _FakeTensor((), dtype="int64", value=0)
    checkpoint_state = OrderedDict(
        (key, value)
        for key, value in model_state.items()
        if key not in expected_counters
    )
    model = _FakeStrictBatchNormModel(
        model_state,
        expected_counters,
    )
    return model, model_state, checkpoint_state, expected_counters


def test_exact_legacy_batchnorm_counter_set_enters_strict_load():
    (
        model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()

    summary = _validated_legacy_batchnorm_counter_set(
        model_state,
        checkpoint_state,
        expected_counters,
        _FAKE_TORCH,
    )
    strict_summary = _strict_load_legacy_batchnorm_counters(
        model,
        checkpoint_state,
        expected_counters,
        _FAKE_TORCH,
    )

    assert summary["missing_count"] == 2
    assert summary["unexpected_count"] == 0
    assert summary["shape_mismatch_count"] == 0
    assert summary["batch_norm_count"] == 2
    assert summary["missing_only_num_batches_tracked"] is True
    assert (
        summary["missing_set_equals_batch_norm_counter_set"]
        is True
    )
    assert strict_summary == {
        "strict_missing_count": 0,
        "strict_unexpected_count": 0,
        "internal_added_counter_count": 2,
        "internal_added_set_equals_expected": True,
        "counters_cpu_int64_scalar_zero": True,
        "checkpoint_tensor_shapes_loaded": True,
        "caller_state_counter_count_before": 0,
        "caller_state_counter_count_after": 0,
    }


def test_legacy_batchnorm_gate_rejects_one_present_expected_counter():
    (
        _model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()
    present_counter = next(iter(expected_counters))
    checkpoint_state[present_counter] = model_state[present_counter]

    with pytest.raises(
        AssertionError,
        match="DENSENET_LEGACY_BATCHNORM_SET_MISMATCH",
    ):
        _validated_legacy_batchnorm_counter_set(
            model_state,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_legacy_batchnorm_gate_rejects_extra_fake_counter():
    (
        _model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()
    checkpoint_state["fake.num_batches_tracked"] = _FakeTensor(
        (),
        dtype="int64",
    )

    with pytest.raises(
        AssertionError,
        match="DENSENET_LEGACY_BATCHNORM_SET_MISMATCH",
    ):
        _validated_legacy_batchnorm_counter_set(
            model_state,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_legacy_batchnorm_gate_rejects_missing_running_mean():
    (
        _model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()
    checkpoint_state.pop("first_bn.running_mean")

    with pytest.raises(
        AssertionError,
        match="DENSENET_LEGACY_BATCHNORM_SET_MISMATCH",
    ):
        _validated_legacy_batchnorm_counter_set(
            model_state,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_legacy_batchnorm_gate_rejects_unexpected_key():
    (
        _model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()
    checkpoint_state["unexpected.weight"] = _FakeTensor((1,))

    with pytest.raises(
        AssertionError,
        match="DENSENET_LEGACY_BATCHNORM_SET_MISMATCH",
    ):
        _validated_legacy_batchnorm_counter_set(
            model_state,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_legacy_batchnorm_gate_rejects_shape_mismatch():
    (
        _model,
        model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()
    checkpoint_state["first_bn.weight"] = _FakeTensor((3,))

    with pytest.raises(
        AssertionError,
        match="DENSENET_LEGACY_BATCHNORM_SET_MISMATCH",
    ):
        _validated_legacy_batchnorm_counter_set(
            model_state,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_legacy_batchnorm_strict_load_error_propagates(monkeypatch):
    (
        model,
        _model_state,
        checkpoint_state,
        expected_counters,
    ) = _tiny_legacy_batchnorm_case()

    def strict_failure(*_args, **_kwargs):
        raise RuntimeError("controlled strict load failure")

    monkeypatch.setattr(model, "load_state_dict", strict_failure)

    with pytest.raises(
        RuntimeError,
        match="controlled strict load failure",
    ):
        _strict_load_legacy_batchnorm_counters(
            model,
            checkpoint_state,
            expected_counters,
            _FAKE_TORCH,
        )


def test_svr_summary_reports_nested_pipeline_without_execution():
    Pipeline = type(
        "Pipeline",
        (),
        {
            "__module__": "sklearn.pipeline",
            "fit": lambda self, value: value,
            "score": lambda self, value: value,
            "transform": lambda self, value: value,
        },
    )
    PCA = type(
        "PCA",
        (),
        {
            "__module__": "sklearn.decomposition._pca",
            "transform": lambda self, value: value,
        },
    )
    StandardScaler = type(
        "StandardScaler",
        (),
        {
            "__module__": "sklearn.preprocessing._data",
            "transform": lambda self, value: value,
        },
    )
    ColumnTransformer = type(
        "ColumnTransformer",
        (),
        {
            "__module__": "sklearn.compose._column_transformer",
            "transform": lambda self, value: value,
        },
    )
    TransformedTargetRegressor = type(
        "TransformedTargetRegressor",
        (),
        {
            "__module__": "sklearn.compose._target",
            "fit": lambda self, value: value,
            "predict": lambda self, value: value,
            "score": lambda self, value: value,
        },
    )
    SVR = type(
        "SVR",
        (),
        {
            "__module__": "sklearn.svm._classes",
            "fit": lambda self, value: value,
            "predict": lambda self, value: value,
            "score": lambda self, value: value,
        },
    )

    pca = PCA()
    pca.n_components_ = 9
    pca.n_features_in_ = _EXPECTED_INPUT_FEATURES - 4
    image_pipeline = Pipeline()
    image_pipeline.steps = (
        ("scaler", StandardScaler()),
        ("pca", pca),
    )
    prep = ColumnTransformer()
    prep.transformers_ = (
        ("proc_scale", StandardScaler(), (0, 1, 2, 3)),
        (
            "img_pca",
            image_pipeline,
            tuple(range(4, _EXPECTED_INPUT_FEATURES)),
        ),
    )
    prep.n_features_in_ = _EXPECTED_INPUT_FEATURES
    pipeline = Pipeline()
    pipeline.steps = (("prep", prep), ("svr", SVR()))
    pipeline.n_features_in_ = _EXPECTED_INPUT_FEATURES
    model = TransformedTargetRegressor()
    model.regressor_ = pipeline
    model.n_features_in_ = _EXPECTED_INPUT_FEATURES

    summary = _svr_summary(model)

    assert summary["pipeline_steps"] == ["prep", "svr"]
    assert summary["all_pipeline_steps"] == [
        ["prep", "svr"],
        ["scaler", "pca"],
    ]
    assert summary["pca_n_components"] == 9
    assert summary["n_features_in"] == _EXPECTED_INPUT_FEATURES
    assert summary["n_features_source"] == "top_level"
    assert summary["final_estimator_type"] == (
        "sklearn.svm._classes.SVR"
    )
    assert Pipeline in summary["transformer_classes"]
    assert PCA in summary["transformer_classes"]
    assert Pipeline in summary["fitter_classes"]
    assert SVR in summary["fitter_classes"]
    assert Pipeline in summary["scorer_classes"]
    assert SVR in summary["scorer_classes"]


def _literal_svr_identity_summary(kind):
    common = {
        "top_level_type": (
            "sklearn.compose._target.TransformedTargetRegressor"
        ),
        "final_estimator_type": "sklearn.svm._classes.SVR",
        "pipeline_steps": ["prep", "svr"],
        "n_features_in": 3076,
        "n_features_source": "top_level",
        "controlled_modules": True,
    }
    if kind == "yield_svr":
        return {
            **common,
            "all_pipeline_steps": [
                ["prep", "svr"],
                ["scaler", "pca"],
            ],
            "pca_n_components": 5,
            "safe_shape_summary": {
                "support_vectors_": [23, 9],
            },
        }
    return {
        **common,
        "all_pipeline_steps": [["prep", "svr"]],
        "pca_n_components": None,
        "safe_shape_summary": {
            "support_vectors_": [21, 8],
        },
    }


def test_svr_identity_accepts_both_exact_fixed_models():
    assert _assert_expected_svr_identity(
        "yield_svr",
        _literal_svr_identity_summary("yield_svr"),
    )
    assert _assert_expected_svr_identity(
        "elongation_svr",
        _literal_svr_identity_summary("elongation_svr"),
    )


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    (
        (
            "top_level_type",
            "sklearn.pipeline.Pipeline",
        ),
        (
            "final_estimator_type",
            "sklearn.linear_model._base.LinearRegression",
        ),
        (
            "pipeline_steps",
            ["prep", "wrong"],
        ),
        (
            "all_pipeline_steps",
            [["prep", "svr"]],
        ),
        (
            "pca_n_components",
            4,
        ),
    ),
)
def test_yield_svr_identity_rejects_wrong_structure(
    field_name,
    wrong_value,
):
    summary = _literal_svr_identity_summary("yield_svr")
    summary[field_name] = wrong_value

    with pytest.raises(
        AssertionError,
        match="YIELD_SVR_IDENTITY_MISMATCH",
    ):
        _assert_expected_svr_identity("yield_svr", summary)


def test_yield_svr_identity_rejects_wrong_support_vector_shape():
    summary = deepcopy(_literal_svr_identity_summary("yield_svr"))
    summary["safe_shape_summary"]["support_vectors_"] = [23, 8]

    with pytest.raises(
        AssertionError,
        match="YIELD_SVR_IDENTITY_MISMATCH",
    ):
        _assert_expected_svr_identity("yield_svr", summary)


def test_svr_identity_rejects_wrong_type_even_with_3076_features():
    summary = _literal_svr_identity_summary("elongation_svr")
    summary["top_level_type"] = "sklearn.pipeline.Pipeline"
    assert summary["n_features_in"] == 3076

    with pytest.raises(
        AssertionError,
        match="ELONGATION_SVR_IDENTITY_MISMATCH",
    ):
        _assert_expected_svr_identity("elongation_svr", summary)


def test_forward_canary_blocks_module_call_and_direct_forward(
    monkeypatch,
):
    counters = {
        "module_call_count": 0,
        "direct_forward_count": 0,
    }

    class FakeModule:
        def __call__(self, *args, **kwargs):
            return self.forward(*args, **kwargs)

    class FakeDenseNet(FakeModule):
        def forward(self, value):
            return value

    class FakeSequential(FakeModule):
        def forward(self, value):
            return value

    def build_fake_conditional_unet():
        class FakeConditionalUNet(FakeModule):
            def forward(self, value):
                return value

        return FakeConditionalUNet()

    architecture = SimpleNamespace(
        build_conditional_unet=build_fake_conditional_unet,
    )
    _install_forward_canaries(
        monkeypatch=monkeypatch,
        module_type=FakeModule,
        direct_forward_types=(FakeDenseNet, FakeSequential),
        architecture=architecture,
        counters=counters,
    )

    with pytest.raises(
        pytest.fail.Exception,
        match="MODEL_MODULE_CALL_CALLED",
    ):
        FakeDenseNet()("blocked")
    with pytest.raises(
        pytest.fail.Exception,
        match="MODEL_DIRECT_FORWARD_CALLED",
    ):
        FakeDenseNet().forward("blocked")
    with pytest.raises(
        pytest.fail.Exception,
        match="MODEL_DIRECT_FORWARD_CALLED",
    ):
        FakeSequential().forward("blocked")
    dynamic_model = architecture.build_conditional_unet()
    with pytest.raises(
        pytest.fail.Exception,
        match="MODEL_DIRECT_FORWARD_CALLED",
    ):
        dynamic_model.forward("blocked")

    assert counters == {
        "module_call_count": 1,
        "direct_forward_count": 3,
    }


def test_authorization_gate_requires_every_exact_common_fact(
    authorization_gate,
):
    authorized = {
        "ZTA35G_REAL_MODEL_ACCEPTANCE": (
            "P1B2_PROJECT_OWNER_AUTHORIZED"
        ),
        "CONDA_DEFAULT_ENV": "materialsagent-zta35g",
        "ZTA35G_MODEL_ROOT": "X:\\reviewed-model-root",
    }

    assert authorization_gate(
        authorized,
        python_version=(3, 8),
        require_gpu=False,
    ) == ()
    for field_name in tuple(authorized):
        missing = dict(authorized)
        missing.pop(field_name)
        assert field_name in authorization_gate(
            missing,
            python_version=(3, 8),
            require_gpu=False,
        )
        wrong = dict(authorized)
        wrong[field_name] = (
            "   " if field_name == "ZTA35G_MODEL_ROOT" else "wrong"
        )
        assert field_name in authorization_gate(
            wrong,
            python_version=(3, 8),
            require_gpu=False,
        )
    assert authorization_gate(
        authorized,
        python_version=(3, 8),
        require_gpu=True,
    ) == ("ZTA35G_GPU_ACCEPTANCE",)
    authorized["ZTA35G_GPU_ACCEPTANCE"] = "P1B2_GPU_AUTHORIZED"
    assert authorization_gate(
        authorized,
        python_version=(3, 8),
        require_gpu=True,
    ) == ()


@pytest.mark.parametrize("python_version", [(3, 11), (3, 9)])
def test_authorization_gate_rejects_non_python_38_interpreters(
    authorization_gate,
    python_version,
):
    authorized = {
        "ZTA35G_REAL_MODEL_ACCEPTANCE": (
            "P1B2_PROJECT_OWNER_AUTHORIZED"
        ),
        "CONDA_DEFAULT_ENV": "materialsagent-zta35g",
        "ZTA35G_MODEL_ROOT": "X:\\reviewed-model-root",
    }

    assert authorization_gate(
        authorized,
        python_version=python_version,
        require_gpu=False,
    ) == ("PYTHON_VERSION",)


def test_gpu_authorization_fails_when_cuda_is_unavailable_before_test_body(
    monkeypatch,
    request,
):
    monkeypatch.setenv(
        "ZTA35G_REAL_MODEL_ACCEPTANCE",
        "P1B2_PROJECT_OWNER_AUTHORIZED",
    )
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "materialsagent-zta35g")
    monkeypatch.setenv("ZTA35G_MODEL_ROOT", "X:\\reviewed-model-root")
    monkeypatch.setenv(
        "ZTA35G_GPU_ACCEPTANCE",
        "P1B2_GPU_AUTHORIZED",
    )
    monkeypatch.setattr(sys, "version_info", (3, 8))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
        ),
    )

    with pytest.raises(pytest.fail.Exception, match="CUDA"):
        request.getfixturevalue("authorized_gpu_model_root")


def test_real_bundle_loads_on_cpu_under_p1b2_authorization(
    authorized_model_root,
    monkeypatch,
):
    import joblib
    from collections.abc import Mapping
    import torch
    import torchvision.models as models

    from materialsagent_zta35g_runtime import (
        inference,
        model_architecture,
    )
    from materialsagent_zta35g_runtime.model_bundle import (
        BUNDLE_FILE_SPECS,
        ModelBundleLoader,
        clean_ddpm_state_keys,
        convert_legacy_densenet_keys,
        select_ddpm_state,
    )

    assert not __import__("os").environ.get("ZTA35G_GPU_ACCEPTANCE")
    paths = {
        spec.kind: authorized_model_root.joinpath(
            *spec.relative_path.split("/")
        )
        for spec in BUNDLE_FILE_SPECS
    }
    identity = {}
    for spec in BUNDLE_FILE_SPECS:
        path = paths[spec.kind]
        assert path.is_file()
        assert not path.is_symlink()
        assert path.stat().st_size == spec.size_bytes
        assert _sha256_file(path) == spec.sha256
        identity[spec.kind] = {
            "relative_path": "SEM/ZTA35G_lab/" + spec.relative_path,
            "size_bytes": spec.size_bytes,
            "sha256": spec.sha256,
        }

    counters = {
        "module_call_count": 0,
        "direct_forward_count": 0,
        "svr_predict_calls": 0,
        "transform_calls": 0,
        "fit_calls": 0,
        "score_calls": 0,
        "ddpm_sample_calls": 0,
        "module_cuda_calls": 0,
        "tensor_cuda_calls": 0,
        "module_to_cuda_calls": 0,
        "tensor_to_cuda_calls": 0,
    }
    deserialize_counts = {
        spec.kind: 0 for spec in BUNDLE_FILE_SPECS
    }
    approved_paths = {
        str(path.resolve()).casefold(): kind
        for kind, path in paths.items()
    }

    original_module_call = torch.nn.Module.__call__
    original_module_to = torch.nn.Module.to
    original_tensor_to = torch.Tensor.to
    original_torch_load = torch.load
    original_joblib_load = joblib.load

    def blocked_module_cuda(_self, *_args, **_kwargs):
        counters["module_cuda_calls"] += 1
        pytest.fail("MODULE_CUDA_CALLED")

    def blocked_tensor_cuda(_self, *_args, **_kwargs):
        counters["tensor_cuda_calls"] += 1
        pytest.fail("TENSOR_CUDA_CALLED")

    def guarded_module_to(self, *args, **kwargs):
        if _is_cuda_target(torch, args, kwargs):
            counters["module_to_cuda_calls"] += 1
            pytest.fail("MODULE_TO_CUDA_CALLED")
        return original_module_to(self, *args, **kwargs)

    def guarded_tensor_to(self, *args, **kwargs):
        if _is_cuda_target(torch, args, kwargs):
            counters["tensor_to_cuda_calls"] += 1
            pytest.fail("TENSOR_TO_CUDA_CALLED")
        return original_tensor_to(self, *args, **kwargs)

    def counted_torch_load(path, *args, **kwargs):
        kind = approved_paths.get(str(Path(path).resolve()).casefold())
        if kind not in ("ddpm", "densenet"):
            pytest.fail("UNAPPROVED_TORCH_LOAD_TARGET")
        deserialize_counts[kind] += 1
        return original_torch_load(path, *args, **kwargs)

    def counted_joblib_load(path, *args, **kwargs):
        kind = approved_paths.get(str(Path(path).resolve()).casefold())
        if kind not in ("yield_svr", "elongation_svr"):
            pytest.fail("UNAPPROVED_JOBLIB_LOAD_TARGET")
        deserialize_counts[kind] += 1
        return original_joblib_load(path, *args, **kwargs)

    def blocked_sampling(*_args, **_kwargs):
        counters["ddpm_sample_calls"] += 1
        pytest.fail("DDPM_SAMPLING_CALLED")

    def blocked_component_predict(*_args, **_kwargs):
        counters["svr_predict_calls"] += 1
        pytest.fail("MECHANICAL_PREDICTION_CALLED")

    _install_forward_canaries(
        monkeypatch=monkeypatch,
        module_type=torch.nn.Module,
        direct_forward_types=(
            models.DenseNet,
            torch.nn.Sequential,
        ),
        architecture=model_architecture,
        counters=counters,
    )
    monkeypatch.setattr(torch.nn.Module, "cuda", blocked_module_cuda)
    monkeypatch.setattr(torch.Tensor, "cuda", blocked_tensor_cuda)
    monkeypatch.setattr(torch.nn.Module, "to", guarded_module_to)
    monkeypatch.setattr(torch.Tensor, "to", guarded_tensor_to)
    monkeypatch.setattr(torch, "load", counted_torch_load)
    monkeypatch.setattr(joblib, "load", counted_joblib_load)
    monkeypatch.setattr(
        inference.TorchZTA35GComponents,
        "generate_sem",
        blocked_sampling,
    )
    monkeypatch.setattr(
        inference.TorchZTA35GComponents,
        "predict_mechanical",
        blocked_component_predict,
    )

    monitor = _MemoryMonitor()
    monitor.start()
    phase_memory = {
        "start_free_bytes": monitor.start_free_bytes,
    }
    try:
        ddpm_started = time.perf_counter()
        checkpoint = torch.load(
            str(paths["ddpm"]),
            map_location="cpu",
        )
        phase_memory["ddpm_checkpoint_free_bytes"] = monitor.observe()
        assert isinstance(checkpoint, Mapping)
        ddpm_state_source = _state_source(checkpoint, Mapping)
        state = clean_ddpm_state_keys(select_ddpm_state(checkpoint))
        ddpm_tensor_count = sum(
            isinstance(value, torch.Tensor) for value in state.values()
        )
        ddpm_non_tensor_count = len(state) - ddpm_tensor_count
        model = model_architecture.build_conditional_unet()
        model_state = model.state_dict()
        ddpm_compatibility = _state_compatibility(
            model_state,
            state,
            torch,
        )
        assert ddpm_compatibility["missing_count"] == 0
        assert ddpm_compatibility["unexpected_count"] == 0
        assert ddpm_compatibility["shape_mismatch_count"] == 0
        incompatible = model.load_state_dict(state, strict=False)
        assert len(incompatible.missing_keys) == 0
        assert len(incompatible.unexpected_keys) == 0
        assert all(
            value.device.type == "cpu"
            for value in tuple(model.parameters())
            + tuple(model.buffers())
        )
        ddpm_summary = {
            "checkpoint_classification": "mapping",
            "has_ema": isinstance(checkpoint.get("ema"), Mapping),
            "has_model": isinstance(checkpoint.get("model"), Mapping),
            "has_state_dict": isinstance(
                checkpoint.get("state_dict"),
                Mapping,
            ),
            "selected_state_source": ddpm_state_source,
            "state_entry_count": len(state),
            "tensor_entry_count": ddpm_tensor_count,
            "non_tensor_entry_count": ddpm_non_tensor_count,
            **ddpm_compatibility,
            **_model_counts(model),
            "load_seconds": round(
                time.perf_counter() - ddpm_started,
                3,
            ),
            "load_result": "compatible",
        }
        ddpm_ref = weakref.ref(model)
        phase_memory["ddpm_loaded_free_bytes"] = monitor.observe()
        del incompatible
        del model_state
        del state
        del checkpoint
        del model
        gc.collect()
        assert ddpm_ref() is None
        phase_memory["ddpm_released_free_bytes"] = monitor.observe()

        densenet_started = time.perf_counter()
        densenet_state = torch.load(
            str(paths["densenet"]),
            map_location="cpu",
        )
        assert isinstance(densenet_state, Mapping)
        converted_state = convert_legacy_densenet_keys(
            densenet_state
        )
        densenet = models.densenet121(weights=None)
        densenet_model_state = densenet.state_dict()
        expected_counters = _batchnorm_counter_names(
            densenet,
            torch,
        )
        densenet_compatibility = (
            _validated_legacy_batchnorm_counter_set(
                densenet_model_state,
                converted_state,
                expected_counters,
                torch,
            )
        )
        densenet_compatibility.pop("expected_counters")
        assert densenet_compatibility["missing_count"] == 121
        assert densenet_compatibility["batch_norm_count"] == 121
        assert (
            densenet_compatibility[
                "missing_only_num_batches_tracked"
            ]
            is True
        )
        assert (
            densenet_compatibility[
                "missing_set_equals_batch_norm_counter_set"
            ]
            is True
        )
        strict_summary = _strict_load_legacy_batchnorm_counters(
            densenet,
            converted_state,
            expected_counters,
            torch,
        )
        assert densenet_compatibility["unexpected_count"] == 0
        assert densenet_compatibility["shape_mismatch_count"] == 0
        assert all(
            value.device.type == "cpu"
            for value in tuple(densenet.parameters())
            + tuple(densenet.buffers())
        )
        densenet_summary = {
            "checkpoint_classification": "mapping",
            "selected_state_source": "checkpoint_mapping",
            "state_entry_count": len(converted_state),
            **densenet_compatibility,
            **strict_summary,
            **_model_counts(densenet),
            "strict_load": True,
            "load_seconds": round(
                time.perf_counter() - densenet_started,
                3,
            ),
            "load_result": "compatible",
        }
        densenet_ref = weakref.ref(densenet)
        phase_memory["densenet_loaded_free_bytes"] = monitor.observe()
        del expected_counters
        del densenet_model_state
        del strict_summary
        del converted_state
        del densenet_state
        del densenet
        gc.collect()
        assert densenet_ref() is None
        phase_memory[
            "densenet_released_free_bytes"
        ] = monitor.observe()

        predictor_classes = set()
        transformer_classes = set()
        fitter_classes = set()
        scorer_classes = set()
        svr_summaries = {}
        for kind in ("yield_svr", "elongation_svr"):
            svr_started = time.perf_counter()
            svr_model = joblib.load(str(paths[kind]))
            safe_summary = _svr_summary(svr_model)
            safe_summary["exact_identity"] = (
                _assert_expected_svr_identity(kind, safe_summary)
            )
            predictor_classes.update(
                safe_summary.pop("predictor_classes")
            )
            transformer_classes.update(
                safe_summary.pop("transformer_classes")
            )
            fitter_classes.update(
                safe_summary.pop("fitter_classes")
            )
            scorer_classes.update(
                safe_summary.pop("scorer_classes")
            )
            safe_summary["load_seconds"] = round(
                time.perf_counter() - svr_started,
                3,
            )
            safe_summary["load_result"] = "compatible"
            svr_summaries[kind] = safe_summary
            svr_ref = weakref.ref(svr_model)
            del svr_model
            gc.collect()
            assert svr_ref() is None
            phase_memory["%s_released_free_bytes" % kind] = (
                monitor.observe()
            )

        def blocked_svr_predict(_self, *_args, **_kwargs):
            counters["svr_predict_calls"] += 1
            pytest.fail("SVR_PREDICT_CALLED")

        def blocked_transform(_self, *_args, **_kwargs):
            counters["transform_calls"] += 1
            pytest.fail("TRANSFORM_CALLED")

        def blocked_fit(_self, *_args, **_kwargs):
            counters["fit_calls"] += 1
            pytest.fail("FIT_CALLED")

        def blocked_score(_self, *_args, **_kwargs):
            counters["score_calls"] += 1
            pytest.fail("SCORE_CALLED")

        for predictor_class in predictor_classes:
            monkeypatch.setattr(
                predictor_class,
                "predict",
                blocked_svr_predict,
            )
        for transformer_class in transformer_classes:
            monkeypatch.setattr(
                transformer_class,
                "transform",
                blocked_transform,
            )
        for fitter_class in fitter_classes:
            monkeypatch.setattr(
                fitter_class,
                "fit",
                blocked_fit,
            )
        for scorer_class in scorer_classes:
            monkeypatch.setattr(
                scorer_class,
                "score",
                blocked_score,
            )

        bundle_started = time.perf_counter()
        loader = ModelBundleLoader(authorized_model_root)
        assert not hasattr(loader, "_bundle")
        bundle = loader.load()
        assert bundle.model_bundle_id == "zta35g-sem-original-bundle"
        assert all(
            summary == {
                "missing_key_count": 0,
                "unexpected_key_count": 0,
            }
            for summary in bundle.load_summaries.values()
        )
        assert all(
            value is not None
            for value in (
                bundle.ddpm,
                bundle.densenet,
                bundle.yield_model,
                bundle.elongation_model,
            )
        )
        assert all(
            value.device.type == "cpu"
            for model in (bundle.ddpm, bundle.densenet)
            for value in tuple(model.parameters())
            + tuple(model.buffers())
        )
        assert deserialize_counts == {
            "ddpm": 2,
            "densenet": 2,
            "yield_svr": 2,
            "elongation_svr": 2,
        }
        phase_memory["bundle_loaded_free_bytes"] = monitor.observe()
        bundle_summary = {
            "bundle_id": bundle.model_bundle_id,
            "device_kind": "cpu",
            "loaded": not bundle._closed,
            "load_seconds": round(
                time.perf_counter() - bundle_started,
                3,
            ),
            "loader_state_model": "stateless_factory",
            "second_full_load_executed": False,
            "bundle_closed_before_close": bundle._closed,
            "bundle_closed_after_close": None,
            "owner_references_cleared": False,
        }
        assert bundle_summary["bundle_closed_before_close"] is False
        object_references = (
            weakref.ref(bundle.ddpm),
            weakref.ref(bundle.densenet),
            weakref.ref(bundle.yield_model),
            weakref.ref(bundle.elongation_model),
        )
        bundle_reference = weakref.ref(bundle)
        bundle.close()
        assert bundle._closed is True
        bundle_summary["bundle_closed_after_close"] = bundle._closed
        del bundle
        del loader
        gc.collect()
        references_cleared = (
            bundle_reference() is None
            and all(reference() is None for reference in object_references)
        )
        assert references_cleared is True
        bundle_summary["owner_references_cleared"] = references_cleared
        phase_memory["bundle_released_free_bytes"] = monitor.observe()
    finally:
        monitor.stop()

    assert original_module_call is not torch.nn.Module.__call__
    assert all(value == 0 for value in counters.values())
    summary = {
        "identity": identity,
        "ddpm": ddpm_summary,
        "densenet": densenet_summary,
        "svr": svr_summaries,
        "bundle": bundle_summary,
        "memory": {
            **phase_memory,
            "total_bytes": monitor.total_bytes,
            "minimum_free_bytes": monitor.min_free_bytes,
            "peak_used_bytes": (
                monitor.total_bytes - monitor.min_free_bytes
            ),
        },
        "deserialize_counts": deserialize_counts,
        "canary_counts": counters,
        "formal_input_feature_count": _EXPECTED_INPUT_FEATURES,
        "gpu_authorization_set": False,
    }
    print(
        "P1B2_MODEL_LOADING_SUMMARY="
        + json.dumps(summary, sort_keys=True)
    )
