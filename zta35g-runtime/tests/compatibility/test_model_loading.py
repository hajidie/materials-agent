import sys
from types import SimpleNamespace

import pytest


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
):
    from materialsagent_zta35g_runtime.model_bundle import (
        ModelBundleLoader,
    )

    bundle = ModelBundleLoader(authorized_model_root).load()
    try:
        assert bundle.model_bundle_id == "zta35g-sem-original-bundle"
        assert all(
            summary == {
                "missing_key_count": 0,
                "unexpected_key_count": 0,
            }
            for summary in bundle.load_summaries.values()
        )
    finally:
        bundle.close()
