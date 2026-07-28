import pytest


MINIO_CONFIGURATION = {
    "MINIO_ENDPOINT": "http://storage.example:9000",
    "MINIO_ACCESS_KEY": "test-access-key",
    "MINIO_SECRET_KEY": "test-secret-key",
    "MINIO_BUCKET": "test-bucket",
    "MINIO_SECURE": "false",
}


def test_safe_defaults_create_m1_settings() -> None:
    from materialsagent.infrastructure.config import load_settings

    settings = load_settings({})

    assert settings.app_env == "local"
    assert settings.log_level == "INFO"


def test_invalid_configuration_uses_sanitized_error() -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings

    secret_value = "secret-configuration-value"

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(
            {
                "APP_ENV": secret_value,
                "LLM_API_KEY": "secret-provider-key",
            }
        )

    message = str(exc_info.value)
    assert message == "Invalid application configuration."
    assert secret_value not in message
    assert "secret-provider-key" not in message


@pytest.mark.parametrize(
    ("endpoint", "secure", "expected_endpoint", "expected_secure"),
    [
        ("http://storage.example", "false", "storage.example", False),
        ("http://storage.example:9000", "false", "storage.example:9000", False),
        ("https://storage.example", "true", "storage.example", True),
        ("http://[2001:db8::1]:9000", "false", "[2001:db8::1]:9000", False),
        ("https://[2001:db8::1]", "true", "[2001:db8::1]", True),
    ],
)
def test_complete_minio_configuration_is_safely_parsed(
    endpoint: str,
    secure: str,
    expected_endpoint: str,
    expected_secure: bool,
) -> None:
    from materialsagent.infrastructure.config import (
        load_settings,
        parse_minio_config,
    )

    settings = load_settings(
        {
            **MINIO_CONFIGURATION,
            "MINIO_ENDPOINT": endpoint,
            "MINIO_SECURE": secure,
        }
    )

    parsed = parse_minio_config(settings)

    assert parsed.endpoint == expected_endpoint
    assert parsed.secure is expected_secure
    assert parsed.access_key == "test-access-key"
    assert parsed.secret_key == "test-secret-key"
    assert parsed.bucket == "test-bucket"


@pytest.mark.parametrize(
    ("endpoint", "secure"),
    [
        ("http://storage.example:9000", "true"),
        ("https://storage.example:9000", "false"),
    ],
)
def test_minio_scheme_and_secure_flag_must_match(
    endpoint: str,
    secure: str,
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_minio_config,
    )

    settings = load_settings(
        {
            **MINIO_CONFIGURATION,
            "MINIO_ENDPOINT": endpoint,
            "MINIO_SECURE": secure,
        }
    )

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid object storage configuration\.$",
    ):
        parse_minio_config(settings)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://storage.example:9000",
        "http://user:password@storage.example:9000",
        "http://storage.example:9000/path",
        "http://storage.example:9000?query=value",
        "http://storage.example:9000#fragment",
    ],
)
def test_minio_endpoint_rejects_unsafe_url_parts(endpoint: str) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_minio_config,
    )

    settings = load_settings(
        {**MINIO_CONFIGURATION, "MINIO_ENDPOINT": endpoint}
    )

    with pytest.raises(ConfigurationError) as exc_info:
        parse_minio_config(settings)

    assert str(exc_info.value) == "Invalid object storage configuration."
    assert endpoint not in str(exc_info.value)


@pytest.mark.parametrize(
    ("endpoint", "secure"),
    [
        ("http://storage.example:", "false"),
        ("https://storage.example:", "true"),
        ("http://storage.example:0", "false"),
        ("http://storage.example:65536", "false"),
        ("http://storage.example:not-a-port", "false"),
    ],
)
def test_minio_endpoint_rejects_empty_or_invalid_explicit_port(
    endpoint: str,
    secure: str,
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_minio_config,
    )

    settings = load_settings(
        {
            **MINIO_CONFIGURATION,
            "MINIO_ENDPOINT": endpoint,
            "MINIO_SECURE": secure,
        }
    )

    with pytest.raises(ConfigurationError) as exc_info:
        parse_minio_config(settings)

    message = str(exc_info.value)
    assert message == "Invalid object storage configuration."
    assert endpoint not in message
    assert MINIO_CONFIGURATION["MINIO_ACCESS_KEY"] not in message
    assert MINIO_CONFIGURATION["MINIO_SECRET_KEY"] not in message
    assert MINIO_CONFIGURATION["MINIO_BUCKET"] not in message


@pytest.mark.parametrize(
    "missing_name",
    [
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_BUCKET",
        "MINIO_SECURE",
    ],
)
def test_missing_minio_configuration_is_safely_rejected(
    missing_name: str,
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_minio_config,
    )

    values = dict(MINIO_CONFIGURATION)
    del values[missing_name]
    settings = load_settings(values)

    with pytest.raises(ConfigurationError) as exc_info:
        parse_minio_config(settings)

    message = str(exc_info.value)
    assert message == "Invalid object storage configuration."
    for actual_value in MINIO_CONFIGURATION.values():
        assert actual_value not in message


def test_explicit_mapping_does_not_read_env_file(
    tmp_path,
    monkeypatch,
) -> None:
    from materialsagent.infrastructure import config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MINIO_ENDPOINT=https://must-not-be-read.example\n"
        "MINIO_ACCESS_KEY=must-not-be-read\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROOT_ENV_FILE", env_file)

    settings = config.load_settings({})

    assert settings.minio_endpoint is None
    assert settings.minio_access_key is None


def test_runtime_configuration_is_loopback_only_and_secret_safe() -> None:
    from materialsagent.infrastructure.config import (
        load_settings,
        parse_zta35g_runtime_config,
    )

    secret = "runtime-secret-for-test"
    settings = load_settings(
        {
            "ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100",
            "ZTA35G_RUNTIME_TOKEN": secret,
            "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "7.5",
            "M5_DEV_ROUTES_ENABLED": "true",
        }
    )
    config = parse_zta35g_runtime_config(settings)

    assert config is not None
    assert config.base_url == "http://127.0.0.1:8100"
    assert config.token.get_secret_value() == secret
    assert config.timeout_seconds == 7.5
    assert settings.m5_dev_routes_enabled is True
    assert secret not in repr(settings)
    assert secret not in repr(config)


@pytest.mark.parametrize(
    "values",
    [
        {"ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100"},
        {"ZTA35G_RUNTIME_TOKEN": "secret"},
        {
            "ZTA35G_RUNTIME_URL": "http://localhost:8100",
            "ZTA35G_RUNTIME_TOKEN": "secret",
        },
        {
            "ZTA35G_RUNTIME_URL": "http://127.0.0.1",
            "ZTA35G_RUNTIME_TOKEN": "secret",
        },
    ],
)
def test_runtime_configuration_rejects_partial_or_non_loopback_values(
    values: dict[str, str],
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_zta35g_runtime_config,
    )

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid Tool Runtime configuration\.$",
    ):
        parse_zta35g_runtime_config(load_settings(values))


def test_absent_runtime_configuration_keeps_adapter_disabled() -> None:
    from materialsagent.infrastructure.config import (
        load_settings,
        parse_zta35g_runtime_config,
    )

    settings = load_settings({})

    assert parse_zta35g_runtime_config(settings) is None
    assert settings.m5_dev_routes_enabled is False


def test_timeline_cursor_signing_key_is_optional_and_secret_safe() -> None:
    from materialsagent.infrastructure.config import load_settings

    absent = load_settings({})
    secret = "timeline-signing-key-with-at-least-32-bytes"
    configured = load_settings({"TIMELINE_CURSOR_SIGNING_KEY": secret})

    assert absent.timeline_cursor_signing_key is None
    assert configured.timeline_cursor_signing_key is not None
    assert (
        configured.timeline_cursor_signing_key.get_secret_value()
        == secret
    )
    assert secret not in repr(configured)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        " timeline-signing-key-with-at-least-32-bytes",
        "timeline-signing-key-with-at-least-32-bytes ",
        "timeline-signing-key-with-at-least-32-\nbytes",
        "timeline-signing-key-with-at-least-32-\x00bytes",
    ],
)
def test_invalid_timeline_cursor_signing_key_is_safely_rejected(
    value: str,
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
    )

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid application configuration\.$",
    ) as exc_info:
        load_settings({"TIMELINE_CURSOR_SIGNING_KEY": value})

    if value:
        assert value not in str(exc_info.value)


def test_timeline_cursor_signing_key_length_is_measured_in_utf8_bytes() -> None:
    from materialsagent.infrastructure.config import load_settings

    value = "密钥" * 6
    settings = load_settings({"TIMELINE_CURSOR_SIGNING_KEY": value})

    assert settings.timeline_cursor_signing_key is not None
    assert settings.timeline_cursor_signing_key.get_secret_value() == value


def test_llm_defaults_keep_mock_enabled_without_provider_secret() -> None:
    from materialsagent.infrastructure.config import (
        load_settings,
        parse_deepseek_config,
    )

    settings = load_settings({})

    assert settings.llm_adapter == "mock"
    assert settings.deepseek_api_key is None
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_timeout_seconds == 60.0
    assert parse_deepseek_config(settings) is None


def test_blank_deepseek_secret_is_normalized_to_none_in_mock_mode() -> None:
    from materialsagent.infrastructure.config import load_settings

    settings = load_settings(
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "   ",
        }
    )

    assert settings.deepseek_api_key is None


def test_complete_deepseek_configuration_is_exact_and_secret_safe() -> None:
    from pydantic import SecretStr

    from materialsagent.infrastructure.config import (
        load_settings,
        parse_deepseek_config,
    )

    secret = "test-only-deepseek-secret"
    settings = load_settings(
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": secret,
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "DEEPSEEK_TIMEOUT_SECONDS": "60",
        }
    )

    parsed = parse_deepseek_config(settings)

    assert parsed is not None
    assert parsed.model_name == "deepseek-v4-flash"
    assert parsed.base_url == "https://api.deepseek.com"
    assert parsed.timeout_seconds == 60.0
    assert isinstance(parsed.api_key, SecretStr)
    assert parsed.api_key.get_secret_value() == secret
    assert secret not in repr(settings)
    assert secret not in repr(parsed)


def test_deepseek_mode_without_nonblank_secret_fails_closed() -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
        parse_deepseek_config,
    )

    settings = load_settings({"LLM_ADAPTER": "deepseek"})

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid DeepSeek configuration\.$",
    ):
        parse_deepseek_config(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"LLM_ADAPTER": "unknown"},
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": " test-only-secret",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret ",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_MODEL": "deepseek-chat",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_BASE_URL": "http://api.deepseek.com",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_BASE_URL": "https://user:password@api.deepseek.com",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com?query=value",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com#fragment",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_TIMEOUT_SECONDS": "0",
        },
        {
            "LLM_ADAPTER": "deepseek",
            "DEEPSEEK_API_KEY": "test-only-secret",
            "DEEPSEEK_TIMEOUT_SECONDS": "-1",
        },
    ],
)
def test_invalid_deepseek_configuration_is_sanitized(
    values: dict[str, str],
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
    )

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid application configuration\.$",
    ) as exc_info:
        load_settings(values)

    message = str(exc_info.value)
    assert "test-only-secret" not in message
    assert all(actual not in message for actual in values.values())
