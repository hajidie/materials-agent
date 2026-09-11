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


def test_runtime_timeout_default_is_independent_from_llm_toml() -> None:
    from materialsagent.infrastructure.config import load_settings

    settings = load_settings({})

    assert settings.zta35g_runtime_timeout_seconds == 10.0


def test_runtime_timeout_accepts_gate4_upper_boundary() -> None:
    from materialsagent.infrastructure.config import (
        load_settings,
        parse_zta35g_runtime_config,
    )

    settings = load_settings(
        {
            "ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100",
            "ZTA35G_RUNTIME_TOKEN": "runtime-test-secret",
            "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "900",
        }
    )

    parsed = parse_zta35g_runtime_config(settings)

    assert parsed is not None
    assert settings.zta35g_runtime_timeout_seconds == 900.0
    assert parsed.timeout_seconds == 900.0


@pytest.mark.parametrize(
    "value",
    ["901", "0", "-1", "not-a-number"],
    ids=("above-upper-bound", "zero", "negative", "non-numeric"),
)
def test_runtime_timeout_rejects_values_outside_gate4_boundary(
    value: str,
) -> None:
    from materialsagent.infrastructure.config import (
        ConfigurationError,
        load_settings,
    )

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid application configuration\.$",
    ):
        load_settings({"ZTA35G_RUNTIME_TIMEOUT_SECONDS": value})


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


def test_llm_defaults_keep_mock_enabled_without_provider_secrets() -> None:
    from materialsagent.infrastructure.config import load_settings

    settings = load_settings({})

    assert settings.llm_adapter == "mock"
    assert settings.deepseek_api_key is None
    assert settings.dashscope_api_key is None


def test_blank_provider_secrets_are_normalized_to_none_in_mock_mode() -> None:
    from materialsagent.infrastructure.config import load_settings

    settings = load_settings(
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "   ",
            "DASHSCOPE_API_KEY": "",
        }
    )

    assert settings.deepseek_api_key is None
    assert settings.dashscope_api_key is None


def test_committed_llm_toml_resolves_default_roles_and_keeps_secret_safe() -> None:
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.infrastructure.llm.configuration import (
        load_llm_configuration,
    )

    secret = "test-only-deepseek-secret"
    configured = load_llm_configuration(
        load_settings(
            {
                "LLM_ADAPTER": "provider",
                "DEEPSEEK_API_KEY": secret,
            }
        )
    )

    chat = configured.for_role("chat_orchestration")
    explanation = configured.for_role("tool_result_explanation")
    assert chat.provider == explanation.provider == "deepseek"
    assert chat.model_name == "deepseek-v4-flash"
    assert chat.max_tokens == 1024
    assert chat.context_window_tokens == 1_000_000
    assert chat.prompt_limit_tokens == 16_384
    assert chat.history_token_budget == 8_192
    assert chat.safety_margin_tokens == 1_024
    assert explanation.max_tokens == 768
    assert explanation.history_token_budget == 0
    assert chat.endpoint == "https://api.deepseek.com"
    assert chat.api_key.get_secret_value() == secret
    assert secret not in repr(configured)


def test_qwen_default_and_role_override_select_only_required_keys(tmp_path) -> None:
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    text = LLM_CONFIG_FILE.read_text(encoding="utf-8")
    qwen_only = tmp_path / "qwen.toml"
    qwen_only.write_text(
        text.replace(
            'default_model = "deepseek_default"',
            'default_model = "qwen_default"',
        ),
        encoding="utf-8",
    )
    configured = load_llm_configuration(
        load_settings(
            {
                "LLM_ADAPTER": "provider",
                "DASHSCOPE_API_KEY": "test-qwen-key",
            }
        ),
        qwen_only,
    )
    assert {
        role.provider for role in configured.roles.values()
    } == {"qwen"}
    assert {
        role.context_window_tokens for role in configured.roles.values()
    } == {1_000_000}

    mixed = tmp_path / "mixed.toml"
    mixed.write_text(
        text.replace(
            "[roles.tool_result_explanation]\n",
            '[roles.tool_result_explanation]\nmodel = "qwen_default"\n',
        ),
        encoding="utf-8",
    )
    mixed_configuration = load_llm_configuration(
        load_settings(
            {
                "LLM_ADAPTER": "provider",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "DASHSCOPE_API_KEY": "test-qwen-key",
            }
        ),
        mixed,
    )
    assert mixed_configuration.for_role("chat_orchestration").provider == "deepseek"
    assert mixed_configuration.for_role("tool_result_explanation").provider == "qwen"


def test_qwen_explanation_role_resolves_independent_reasoning_parameters(
    tmp_path,
) -> None:
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    candidate = tmp_path / "qwen-explanation.toml"
    source = LLM_CONFIG_FILE.read_text(encoding="utf-8")
    old_block = (
        "[roles.tool_result_explanation]\n"
        "temperature = 0.0\n"
        "max_tokens = 768\n"
        'reasoning_mode = "disabled"'
    )
    new_block = (
        "[roles.tool_result_explanation]\n"
        'model = "qwen_default"\n'
        "top_p = 0.9\n"
        "top_k = 20\n"
        "max_tokens = 768\n"
        'reasoning_mode = "enabled"\n'
        'reasoning_effort = "high"\n'
        "thinking_budget = 4096"
    )
    candidate.write_text(source.replace(old_block, new_block), encoding="utf-8")

    configured = load_llm_configuration(
        load_settings(
            {
                "LLM_ADAPTER": "provider",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "DASHSCOPE_API_KEY": "test-qwen-key",
            }
        ),
        candidate,
    )
    role = configured.for_role("tool_result_explanation")

    assert role.provider == "qwen"
    assert role.top_p == 0.9
    assert role.top_k == 20
    assert role.reasoning_effort == "high"
    assert role.thinking_budget == 4096
    assert role.streaming is True
    assert dict(role.generation_parameters) == {
        "schema_version": 1,
        "top_p": 0.9,
        "top_k": 20,
        "max_tokens": 768,
        "reasoning_mode": "enabled",
        "reasoning_effort": "high",
        "thinking_budget": 4096,
        "response_format": "text",
        "streaming": True,
    }


@pytest.mark.parametrize(
    ("replacement", "expected_field"),
    [
        (
            "temperature = 0.0\ntop_k = 10",
            "top_k",
        ),
        (
            'temperature = 0.0\nreasoning_mode = "enabled"',
            "temperature",
        ),
    ],
)
def test_deepseek_hard_conflicts_fail_at_startup(
    tmp_path,
    replacement: str,
    expected_field: str,
) -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    candidate = tmp_path / "invalid.toml"
    source = LLM_CONFIG_FILE.read_text(encoding="utf-8")
    if expected_field == "temperature":
        source = source.replace(
            'reasoning_mode = "disabled"',
            'reasoning_mode = "enabled"',
            1,
        )
    else:
        source = source.replace("temperature = 0.0", replacement, 1)
    candidate.write_text(
        source,
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as captured:
        load_llm_configuration(
            load_settings(
                {
                    "LLM_ADAPTER": "provider",
                    "DEEPSEEK_API_KEY": "secret-must-not-leak",
                }
            ),
            candidate,
        )
    assert "chat_orchestration" in str(captured.value)
    assert expected_field in str(captured.value)
    assert "secret-must-not-leak" not in str(captured.value)


def test_qwen_structured_reasoning_is_rejected(tmp_path) -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    candidate = tmp_path / "qwen-reasoning.toml"
    candidate.write_text(
        LLM_CONFIG_FILE.read_text(encoding="utf-8")
        .replace(
            'default_model = "deepseek_default"',
            'default_model = "qwen_default"',
        )
        .replace(
            'reasoning_mode = "disabled"',
            'reasoning_mode = "enabled"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="chat_orchestration.*reasoning_mode"):
        load_llm_configuration(
            load_settings(
                {
                    "LLM_ADAPTER": "provider",
                    "DASHSCOPE_API_KEY": "test-qwen-key",
                }
            ),
            candidate,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(
            'reasoning_efforts = ["low", "high", "max"]',
            'reasoning_efforts = ["low", "medium", "high", "max"]',
        ),
        lambda text: text.replace(
            'structured_output_reasoning_modes = ["disabled"]',
            'structured_output_reasoning_modes = ["disabled", "enabled"]',
        ),
        lambda text: text.replace(
            "requires_streaming_for_reasoning = true",
            "requires_streaming_for_reasoning = false",
        ),
    ],
    ids=(
        "deepseek-effort-ceiling",
        "qwen-structured-reasoning-ceiling",
        "qwen-streaming-requirement",
    ),
)
def test_capability_declarations_cannot_relax_provider_hard_boundaries(
    tmp_path,
    mutation,
) -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    candidate = tmp_path / "invalid-capability.toml"
    candidate.write_text(
        mutation(LLM_CONFIG_FILE.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Invalid LLM configuration"):
        load_llm_configuration(
            load_settings(
                {
                    "LLM_ADAPTER": "provider",
                    "DEEPSEEK_API_KEY": "test-deepseek-key",
                    "DASHSCOPE_API_KEY": "test-qwen-key",
                }
            ),
            candidate,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("schema_version = 1", "schema_version = 2"),
        lambda text: text.replace(
            'provider = "deepseek"',
            'provider = "unknown"',
            1,
        ),
        lambda text: text.replace(
            "[defaults]\n",
            "[defaults]\nunknown_field = true\n",
        ),
        lambda text: text.replace("temperature = 0.0", "temperature = 3.0", 1),
    ],
)
def test_llm_toml_unknown_fields_and_invalid_values_are_strictly_rejected(
    tmp_path,
    mutation,
) -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.infrastructure.llm.configuration import (
        LLM_CONFIG_FILE,
        load_llm_configuration,
    )

    candidate = tmp_path / "invalid.toml"
    candidate.write_text(
        mutation(LLM_CONFIG_FILE.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Invalid LLM configuration"):
        load_llm_configuration(
            load_settings(
                {
                    "LLM_ADAPTER": "provider",
                    "DEEPSEEK_API_KEY": "test-key",
                }
            ),
            candidate,
        )


def test_provider_mode_requires_only_the_selected_provider_secret() -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.infrastructure.llm.configuration import load_llm_configuration

    with pytest.raises(
        ConfigurationError,
        match="DEEPSEEK_API_KEY",
    ):
        load_llm_configuration(load_settings({"LLM_ADAPTER": "provider"}))


@pytest.mark.parametrize(
    "values",
    [
        {"LLM_ADAPTER": "unknown"},
        {
            "LLM_ADAPTER": "provider",
            "DEEPSEEK_API_KEY": " test-only-secret",
        },
        {
            "LLM_ADAPTER": "provider",
            "DASHSCOPE_API_KEY": "test-only-secret ",
        },
        {
            "LLM_ADAPTER": "provider",
            "DEEPSEEK_API_KEY": "test-only\nsecret",
        },
    ],
)
def test_invalid_provider_environment_configuration_is_sanitized(
    values: dict[str, str],
) -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid application configuration\.$",
    ) as captured:
        load_settings(values)
    assert "test-only-secret" not in str(captured.value)


def test_dev_fake_side_effect_tool_is_forbidden_in_production() -> None:
    from materialsagent.infrastructure.config import ConfigurationError, load_settings

    with pytest.raises(
        ConfigurationError,
        match=r"^Invalid application configuration\.$",
    ):
        load_settings(
            {
                "APP_ENV": "production",
                "ENABLE_DEV_FAKE_SIDE_EFFECT_TOOL": "true",
            }
        )


def test_injected_side_effect_registry_is_forbidden_in_production() -> None:
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.infrastructure.config import ConfigurationError, load_settings
    from materialsagent.main import create_app

    registry = build_tool_registry(enable_dev_fake_side_effect_tool=True)

    with pytest.raises(ConfigurationError, match="forbidden in the production Catalog"):
        create_app(
            settings=load_settings({"APP_ENV": "production"}),
            tool_registry=registry,
        )
