from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping, Optional

from .constants import DEFAULT_PORT, HOST


class ConfigurationError(RuntimeError):
    """Safe startup configuration failure."""


@dataclass(frozen=True)
class RuntimeSettings:
    token: str = field(repr=False)
    model_root: Path
    port: int = DEFAULT_PORT
    host: str = field(default=HOST, init=False)

    def __post_init__(self):
        if (
            not isinstance(self.token, str)
            or not self.token
            or not self.token.strip()
            or self.token != self.token.strip()
        ):
            raise ConfigurationError("Runtime token is invalid.")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ConfigurationError("Runtime port is invalid.")
        root = Path(self.model_root)
        if not root.is_dir():
            raise ConfigurationError("Runtime model root is invalid.")
        object.__setattr__(self, "model_root", root.resolve())


def load_settings(
    environ=None,  # type: Optional[Mapping[str, str]]
):
    # type: (...) -> RuntimeSettings
    source = os.environ if environ is None else environ
    token = source.get("ZTA35G_RUNTIME_TOKEN")
    port_text = source.get("ZTA35G_RUNTIME_PORT", str(DEFAULT_PORT))
    model_root_text = source.get("ZTA35G_MODEL_ROOT")
    if token is None:
        raise ConfigurationError("Runtime token is required.")
    if model_root_text is None or not model_root_text:
        raise ConfigurationError("Runtime model root is required.")
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        raise ConfigurationError("Runtime port is invalid.") from None
    try:
        return RuntimeSettings(
            token=token,
            port=port,
            model_root=Path(model_root_text),
        )
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("Runtime configuration is invalid.") from None
