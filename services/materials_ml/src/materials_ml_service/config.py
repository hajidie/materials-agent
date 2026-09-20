from pathlib import Path
import re
import secrets

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ML_", extra="ignore")
    database_url: SecretStr
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: SecretStr
    minio_secret_key: SecretStr
    minio_bucket: str = "materials-ml"
    minio_secure: bool = False
    resource_token: SecretStr
    worker_token: SecretStr
    mcp_enabled: bool = False
    mcp_token: SecretStr | None = None
    namespace: str = "ml/v1"
    store_id: str = "ml-primary"

    @model_validator(mode="after")
    def validate_domains(self):
        tokens = [self.resource_token.get_secret_value(), self.worker_token.get_secret_value()]
        if self.mcp_enabled and self.mcp_token is None:
            raise ValueError("MCP credential required")
        if self.mcp_token is not None:
            tokens.append(self.mcp_token.get_secret_value())
        if (not all(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) for token in tokens)
                or any(secrets.compare_digest(a, b) for i, a in enumerate(tokens) for b in tokens[i+1:])):
            raise ValueError("Independent authentication domains require distinct strong tokens")
        if self.namespace != "ml/v1":
            raise ValueError("Unsupported storage namespace")
        url = make_url(self.database_url.get_secret_value())
        if (not re.fullmatch(r"materials_ml(?:_[0-9a-f]{16})?", url.database or "")
                or url.username != url.database or not url.password
                or self.minio_bucket != url.database.replace("_", "-")
                or self.minio_access_key.get_secret_value() != url.username):
            raise ValueError("ML requires its own corresponding database, role and bucket identities")
        return self


def load_settings():
    # Never loads the platform's root .env implicitly.
    import os
    return Settings(_env_file=Path(os.environ["ML_ENV_FILE"]) if os.environ.get("ML_ENV_FILE") else None)
