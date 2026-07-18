from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


class ConfigurationError(RuntimeError):
    """Safe application configuration error without input value disclosure."""


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: Literal["local", "test"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    local_actor_id: str | None = None

    postgres_host: str | None = None
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None


ENVIRONMENT_FIELDS = {
    "APP_ENV": "app_env",
    "LOG_LEVEL": "log_level",
    "LOCAL_ACTOR_ID": "local_actor_id",
    "POSTGRES_HOST": "postgres_host",
    "POSTGRES_PORT": "postgres_port",
    "POSTGRES_DB": "postgres_db",
    "POSTGRES_USER": "postgres_user",
    "POSTGRES_PASSWORD": "postgres_password",
}


def load_settings(environ: Mapping[str, str] | None = None) -> AppSettings:
    try:
        if environ is None:
            return AppSettings(_env_file=ROOT_ENV_FILE)

        values = {
            field_name: environ[environment_name]
            for environment_name, field_name in ENVIRONMENT_FIELDS.items()
            if environment_name in environ
        }
        return AppSettings(_env_file=None, **values)
    except ValidationError:
        raise ConfigurationError("Invalid application configuration.") from None
