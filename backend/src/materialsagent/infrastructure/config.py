from collections.abc import Mapping
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigurationError(RuntimeError):
    """Safe application configuration error without input value disclosure."""


class AppSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: Literal["local", "test"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def load_settings(environ: Mapping[str, str] | None = None) -> AppSettings:
    source = os.environ if environ is None else environ
    values: dict[str, str] = {}

    if "APP_ENV" in source:
        values["app_env"] = source["APP_ENV"]
    if "LOG_LEVEL" in source:
        values["log_level"] = source["LOG_LEVEL"]

    try:
        return AppSettings.model_validate(values)
    except ValidationError:
        raise ConfigurationError("Invalid application configuration.") from None
