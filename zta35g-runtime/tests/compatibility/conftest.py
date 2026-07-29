import os
from pathlib import Path
import sys

import pytest


_REAL_MODEL_AUTHORIZATION = "P1B2_PROJECT_OWNER_AUTHORIZED"
_GPU_AUTHORIZATION = "P1B2_GPU_AUTHORIZED"
_MODEL_ENVIRONMENT = "materialsagent-zta35g"


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
