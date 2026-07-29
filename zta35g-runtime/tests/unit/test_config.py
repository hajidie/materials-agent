from pathlib import Path

import pytest

from materialsagent_zta35g_runtime.config import (
    ConfigurationError,
    RuntimeSettings,
    load_settings,
)


def test_settings_keep_host_fixed_and_hide_token(tmp_path):
    settings = RuntimeSettings(
        token="runtime-secret-token",
        port=8123,
        model_root=tmp_path,
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8123
    assert settings.model_root == tmp_path.resolve()
    assert "runtime-secret-token" not in repr(settings)


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"ZTA35G_RUNTIME_TOKEN": ""},
        {"ZTA35G_RUNTIME_TOKEN": "   "},
        {
            "ZTA35G_RUNTIME_TOKEN": "secret",
            "ZTA35G_RUNTIME_PORT": "0",
        },
        {
            "ZTA35G_RUNTIME_TOKEN": "secret",
            "ZTA35G_RUNTIME_PORT": "65536",
        },
        {
            "ZTA35G_RUNTIME_TOKEN": "secret",
            "ZTA35G_RUNTIME_PORT": "not-an-int",
        },
    ],
)
def test_invalid_settings_fail_without_leaking_token(tmp_path, environment):
    environment.setdefault("ZTA35G_MODEL_ROOT", str(tmp_path))

    with pytest.raises(ConfigurationError) as raised:
        load_settings(environment)

    assert "secret" not in str(raised.value)


def test_model_root_must_be_existing_directory(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(ConfigurationError) as raised:
        load_settings(
            {
                "ZTA35G_RUNTIME_TOKEN": "secret-value",
                "ZTA35G_RUNTIME_PORT": "8100",
                "ZTA35G_MODEL_ROOT": str(missing),
            }
        )

    assert str(missing) not in str(raised.value)
    assert "secret-value" not in str(raised.value)


def test_load_settings_preserves_exact_nonblank_token(tmp_path):
    settings = load_settings(
        {
            "ZTA35G_RUNTIME_TOKEN": "exact-token",
            "ZTA35G_RUNTIME_PORT": "8101",
            "ZTA35G_MODEL_ROOT": str(tmp_path),
        }
    )

    assert settings.token == "exact-token"
    assert settings.port == 8101
    assert settings.model_root == Path(tmp_path).resolve()
