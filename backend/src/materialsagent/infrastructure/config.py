from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import unicodedata
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


class ConfigurationError(RuntimeError):
    """Safe application configuration error without input value disclosure."""


@dataclass(frozen=True, slots=True)
class MinioConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    secure: bool


@dataclass(frozen=True, slots=True)
class ZTA35GRuntimeConfig:
    base_url: str
    token: SecretStr
    timeout_seconds: float


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    local_actor_id: str | None = None

    postgres_host: str | None = None
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None

    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str | None = None
    minio_secure: bool | None = None

    llm_adapter: Literal["mock", "provider"] = "mock"
    deepseek_api_key: SecretStr | None = None
    dashscope_api_key: SecretStr | None = None

    zta35g_runtime_url: str | None = None
    zta35g_runtime_token: SecretStr | None = None
    zta35g_runtime_timeout_seconds: float = Field(default=1200.0, gt=0, le=86400)
    agent_max_action_steps: int = Field(default=12, ge=1, le=100)
    agent_max_tool_executions: int = Field(default=4, ge=1, le=32)
    agent_max_active_seconds: float = Field(default=3600, gt=0, le=86400)
    agent_max_llm_tokens: int = Field(default=32000, ge=1, le=2000000)
    agent_standard_timeout_seconds: float = Field(default=10, gt=0, le=3600)
    m5_dev_routes_enabled: bool = False
    enable_dev_fake_side_effect_tool: bool = False
    enable_materials_ml_resources: bool = False
    enable_materials_ml_resource_context: bool = False
    enable_dev_materials_ml_tools: bool = False
    materials_ml_mcp_url: str = "http://127.0.0.1:8200/mcp"
    materials_ml_binding_version: str = "1"
    materials_ml_mcp_token: SecretStr | None = None
    materials_ml_resource_token: SecretStr | None = None

    @field_validator(
        "deepseek_api_key",
        "dashscope_api_key",
        mode="before",
    )
    @classmethod
    def normalize_blank_provider_api_key(
        cls,
        value: object,
    ) -> object:
        if isinstance(value, str):
            if not value.strip():
                return None
            if value != value.strip() or any(
                character.isspace()
                or unicodedata.category(character).startswith("C")
                for character in value
            ):
                raise ValueError(
                    "Provider API keys must be controlled non-whitespace text."
                )
        return value

    @model_validator(mode="after")
    def reject_dev_fake_in_production(self) -> "AppSettings":
        if self.enable_materials_ml_resource_context and (self.app_env not in ("local", "test")
                or not self.enable_materials_ml_resources or not self.enable_dev_materials_ml_tools):
            raise ValueError("ML resource context requires local/test MCP and Resource capabilities.")
        if self.app_env == "production" and self.enable_dev_fake_side_effect_tool:
            raise ValueError("Development fake Side-effect Tool is forbidden in production.")
        if self.enable_materials_ml_resources:
            import re
            if (self.app_env == "production" or self.materials_ml_resource_token is None
                    or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.materials_ml_resource_token.get_secret_value()) is None):
                raise ValueError("Local ML resources require an explicit Resource credential.")
            if self.materials_ml_mcp_token and self.materials_ml_mcp_token.get_secret_value() == self.materials_ml_resource_token.get_secret_value():
                raise ValueError("Resource and MCP credentials must differ.")
        if self.enable_dev_materials_ml_tools:
            if self.app_env == "production":
                raise ValueError("ML Side-effect Tools are forbidden in production.")
            tokens = (self.materials_ml_mcp_token, self.materials_ml_resource_token)
            import re
            if (any(t is None or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", t.get_secret_value()) is None for t in tokens)
                    or tokens[0].get_secret_value() == tokens[1].get_secret_value()):
                raise ValueError("Distinct MCP and Resource credentials are required.")
            if not self.materials_ml_binding_version.strip():
                raise ValueError("ML binding version is required.")
        return self


ENVIRONMENT_FIELDS = {
    "APP_ENV": "app_env",
    "LOG_LEVEL": "log_level",
    "LOCAL_ACTOR_ID": "local_actor_id",
    "POSTGRES_HOST": "postgres_host",
    "POSTGRES_PORT": "postgres_port",
    "POSTGRES_DB": "postgres_db",
    "POSTGRES_USER": "postgres_user",
    "POSTGRES_PASSWORD": "postgres_password",
    "MINIO_ENDPOINT": "minio_endpoint",
    "MINIO_ACCESS_KEY": "minio_access_key",
    "MINIO_SECRET_KEY": "minio_secret_key",
    "MINIO_BUCKET": "minio_bucket",
    "MINIO_SECURE": "minio_secure",
    "LLM_ADAPTER": "llm_adapter",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "ZTA35G_RUNTIME_URL": "zta35g_runtime_url",
    "ZTA35G_RUNTIME_TOKEN": "zta35g_runtime_token",
    "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "zta35g_runtime_timeout_seconds",
    "AGENT_MAX_ACTION_STEPS": "agent_max_action_steps",
    "AGENT_MAX_TOOL_EXECUTIONS": "agent_max_tool_executions",
    "AGENT_MAX_ACTIVE_SECONDS": "agent_max_active_seconds",
    "AGENT_MAX_LLM_TOKENS": "agent_max_llm_tokens",
    "AGENT_STANDARD_TIMEOUT_SECONDS": "agent_standard_timeout_seconds",
    "M5_DEV_ROUTES_ENABLED": "m5_dev_routes_enabled",
    "ENABLE_DEV_FAKE_SIDE_EFFECT_TOOL": "enable_dev_fake_side_effect_tool",
    "ENABLE_MATERIALS_ML_RESOURCES": "enable_materials_ml_resources",
    "ENABLE_MATERIALS_ML_RESOURCE_CONTEXT": "enable_materials_ml_resource_context",
    "ENABLE_DEV_MATERIALS_ML_TOOLS": "enable_dev_materials_ml_tools",
    "MATERIALS_ML_MCP_URL": "materials_ml_mcp_url",
    "MATERIALS_ML_BINDING_VERSION": "materials_ml_binding_version",
    "MATERIALS_ML_MCP_TOKEN": "materials_ml_mcp_token",
    "MATERIALS_ML_RESOURCE_TOKEN": "materials_ml_resource_token",
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


def parse_minio_config(settings: AppSettings) -> MinioConfig:
    endpoint = settings.minio_endpoint
    access_key = settings.minio_access_key
    secret_key = settings.minio_secret_key
    bucket = settings.minio_bucket
    secure = settings.minio_secure

    if (
        endpoint is None
        or not endpoint.strip()
        or endpoint != endpoint.strip()
        or access_key is None
        or not access_key.strip()
        or secret_key is None
        or not secret_key
        or bucket is None
        or not bucket.strip()
        or secure is None
    ):
        raise ConfigurationError("Invalid object storage configuration.")

    try:
        parsed = urlparse(endpoint)
        parsed_port = parsed.port
    except ValueError:
        raise ConfigurationError(
            "Invalid object storage configuration."
        ) from None

    expected_secure = parsed.scheme == "https"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
        or secure is not expected_secure
    ):
        raise ConfigurationError("Invalid object storage configuration.")

    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ConfigurationError("Invalid object storage configuration.")

    return MinioConfig(
        endpoint=parsed.netloc,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        secure=secure,
    )


def parse_zta35g_runtime_config(
    settings: AppSettings,
) -> ZTA35GRuntimeConfig | None:
    base_url = settings.zta35g_runtime_url
    token = settings.zta35g_runtime_token
    if base_url is None and token is None:
        return None
    if base_url is None or token is None or not token.get_secret_value():
        raise ConfigurationError("Invalid Tool Runtime configuration.")
    try:
        parsed = urlparse(base_url)
        port = parsed.port
    except ValueError:
        raise ConfigurationError("Invalid Tool Runtime configuration.") from None
    if (
        base_url != base_url.strip()
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("Invalid Tool Runtime configuration.")
    return ZTA35GRuntimeConfig(
        base_url=base_url.rstrip("/"),
        token=token,
        timeout_seconds=settings.zta35g_runtime_timeout_seconds,
    )
