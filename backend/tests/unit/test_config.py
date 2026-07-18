import pytest


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
