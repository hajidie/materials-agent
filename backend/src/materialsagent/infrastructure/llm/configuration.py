from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import math
import re
import tomllib
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from materialsagent.infrastructure.config import AppSettings, ConfigurationError


LLM_CONFIG_FILE: Final = (
    Path(__file__).resolve().parents[4] / "config" / "llm.toml"
)
ROLE_NAMES: Final = (
    "chat_orchestration",
    "tool_input_extraction",
    "tool_result_explanation",
)
STRUCTURED_ROLES: Final = frozenset(
    {"chat_orchestration", "tool_input_extraction"}
)
PROVIDER_ENDPOINTS: Final = {
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
PROVIDER_PARAMETERS: Final = {
    "deepseek": frozenset({"temperature", "top_p", "max_tokens"}),
    "qwen": frozenset({"temperature", "top_p", "top_k", "max_tokens"}),
}
PROVIDER_REASONING_EFFORTS: Final = {
    "deepseek": frozenset({"low", "high", "max"}),
    "qwen": frozenset({"low", "medium", "high", "xhigh", "max"}),
}
PROVIDER_STRUCTURED_REASONING_MODES: Final = {
    "deepseek": frozenset({"disabled", "enabled"}),
    "qwen": frozenset({"disabled"}),
}
PROFILE_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REASONING_EFFORTS: Final = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)

ProviderName = Literal["deepseek", "qwen"]
ParameterName = Literal["temperature", "top_p", "top_k", "max_tokens"]
ReasoningMode = Literal["disabled", "enabled"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ModelCapabilities(_StrictModel):
    supported_parameters: Annotated[
        tuple[ParameterName, ...],
        Field(strict=False),
    ]
    reasoning_modes: Annotated[
        tuple[ReasoningMode, ...],
        Field(strict=False),
    ]
    reasoning_efforts: Annotated[
        tuple[ReasoningEffort, ...],
        Field(strict=False),
    ]
    supports_thinking_budget: bool
    structured_output_reasoning_modes: Annotated[
        tuple[ReasoningMode, ...],
        Field(strict=False),
    ]
    supports_sampling_with_reasoning: bool
    requires_streaming_for_reasoning: bool

    @field_validator(
        "supported_parameters",
        "reasoning_modes",
        "reasoning_efforts",
        "structured_output_reasoning_modes",
    )
    @classmethod
    def require_unique_nonempty_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Capability lists must be non-empty and unique.")
        return value

    @model_validator(mode="after")
    def require_structured_modes_to_be_supported(self) -> ModelCapabilities:
        if not set(self.structured_output_reasoning_modes).issubset(
            self.reasoning_modes
        ):
            raise ValueError(
                "Structured-output reasoning modes must be supported modes."
            )
        return self


class ModelDefinition(_StrictModel):
    provider: ProviderName
    name: str
    context_window_tokens: Annotated[
        int,
        Field(gt=0, le=4_000_000, strict=True),
    ]
    capabilities: ModelCapabilities

    @field_validator("name")
    @classmethod
    def require_controlled_model_name(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or len(value) > 128
            or not value.isprintable()
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Model name must be bounded controlled text.")
        return value

    @model_validator(mode="after")
    def enforce_provider_capability_ceiling(self) -> ModelDefinition:
        unsupported = set(self.capabilities.supported_parameters).difference(
            PROVIDER_PARAMETERS[self.provider]
        )
        if unsupported:
            raise ValueError(
                "Capability declaration exceeds the provider parameter boundary."
            )
        unsupported_efforts = set(
            self.capabilities.reasoning_efforts
        ).difference(PROVIDER_REASONING_EFFORTS[self.provider])
        if unsupported_efforts:
            raise ValueError(
                "Capability declaration exceeds the provider reasoning-effort "
                "boundary."
            )
        unsupported_structured_modes = set(
            self.capabilities.structured_output_reasoning_modes
        ).difference(PROVIDER_STRUCTURED_REASONING_MODES[self.provider])
        if unsupported_structured_modes:
            raise ValueError(
                "Capability declaration exceeds the provider structured-output "
                "reasoning boundary."
            )
        if self.provider == "deepseek":
            if self.capabilities.supports_thinking_budget:
                raise ValueError(
                    "DeepSeek cannot declare thinking-budget support."
                )
            if self.capabilities.supports_sampling_with_reasoning:
                raise ValueError(
                    "DeepSeek cannot declare sampling support in reasoning mode."
                )
        elif not self.capabilities.requires_streaming_for_reasoning:
            raise ValueError(
                "Qwen reasoning must declare its provider streaming requirement."
            )
        return self


class DefaultsDefinition(_StrictModel):
    timeout_seconds: Annotated[float, Field(gt=0, le=300, strict=False)]


class RoleDefinition(_StrictModel):
    model: str | None = None
    temperature: float | int | None = None
    top_p: float | int | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    reasoning_mode: ReasoningMode | None = None
    reasoning_effort: ReasoningEffort | None = None
    thinking_budget: int | None = None
    prompt_limit_tokens: Annotated[
        int,
        Field(gt=0, le=1_000_000, strict=True),
    ]
    history_token_budget: Annotated[
        int,
        Field(ge=0, le=1_000_000, strict=True),
    ]
    safety_margin_tokens: Annotated[
        int,
        Field(ge=0, le=131_072, strict=True),
    ]

    @field_validator("model")
    @classmethod
    def validate_profile_reference(cls, value: str | None) -> str | None:
        if value is not None and PROFILE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Role model reference is invalid.")
        return value

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, value: float | int | None) -> float | int | None:
        if value is not None and (
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            or not 0 <= value <= 2
        ):
            raise ValueError("temperature must be between 0 and 2.")
        return value

    @field_validator("top_p")
    @classmethod
    def validate_top_p(cls, value: float | int | None) -> float | int | None:
        if value is not None and (
            type(value) not in (int, float)
            or not math.isfinite(float(value))
            or not 0 < value <= 1
        ):
            raise ValueError("top_p must be greater than 0 and at most 1.")
        return value

    @field_validator("top_k", "max_tokens", "thinking_budget")
    @classmethod
    def validate_positive_integer(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or not 1 <= value <= 131072):
            raise ValueError("Token and sampling limits must be positive integers.")
        return value

    @model_validator(mode="after")
    def require_reasoning_for_reasoning_parameters(self) -> RoleDefinition:
        if self.reasoning_mode != "enabled" and (
            self.reasoning_effort is not None or self.thinking_budget is not None
        ):
            raise ValueError(
                "reasoning_effort and thinking_budget require enabled reasoning."
            )
        return self

    @model_validator(mode="after")
    def require_bounded_context_budget(self) -> RoleDefinition:
        if self.history_token_budget > self.prompt_limit_tokens:
            raise ValueError(
                "history_token_budget must not exceed prompt_limit_tokens."
            )
        return self


class RolesDefinition(_StrictModel):
    chat_orchestration: RoleDefinition
    tool_input_extraction: RoleDefinition
    tool_result_explanation: RoleDefinition


class LLMDocument(_StrictModel):
    schema_version: Literal[1]
    default_model: str
    models: dict[str, ModelDefinition]
    defaults: DefaultsDefinition
    roles: RolesDefinition

    @field_validator("default_model")
    @classmethod
    def validate_default_profile_name(cls, value: str) -> str:
        if PROFILE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Default model reference is invalid.")
        return value

    @field_validator("models")
    @classmethod
    def validate_model_profile_names(
        cls,
        value: dict[str, ModelDefinition],
    ) -> dict[str, ModelDefinition]:
        if not value or any(
            PROFILE_NAME_PATTERN.fullmatch(name) is None for name in value
        ):
            raise ValueError("Model profile names are invalid.")
        return value

    @model_validator(mode="after")
    def validate_model_references(self) -> LLMDocument:
        if self.default_model not in self.models:
            raise ValueError("default_model does not reference a configured model.")
        for role_name in ROLE_NAMES:
            role = getattr(self.roles, role_name)
            if role.model is not None and role.model not in self.models:
                raise ValueError(
                    f"roles.{role_name}.model does not reference a configured model."
                )
        return self


@dataclass(frozen=True, slots=True)
class ConfiguredRole:
    role: str
    provider: ProviderName
    model_name: str
    api_key: SecretStr
    endpoint: str
    timeout_seconds: float
    temperature: float | int | None
    top_p: float | int | None
    top_k: int | None
    max_tokens: int | None
    reasoning_mode: ReasoningMode | None
    reasoning_effort: ReasoningEffort | None
    thinking_budget: int | None
    response_format: Literal["json_object", "text"]
    streaming: bool
    context_window_tokens: int
    prompt_limit_tokens: int
    history_token_budget: int
    safety_margin_tokens: int

    @property
    def generation_parameters(self) -> Mapping[str, object]:
        values: dict[str, object] = {"schema_version": 1}
        for name in ("temperature", "top_p", "top_k", "max_tokens"):
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        if self.reasoning_mode is not None:
            values["reasoning_mode"] = self.reasoning_mode
        if self.reasoning_effort is not None:
            values["reasoning_effort"] = self.reasoning_effort
        if self.thinking_budget is not None:
            values["thinking_budget"] = self.thinking_budget
        values["response_format"] = self.response_format
        values["streaming"] = self.streaming
        return MappingProxyType(values)


@dataclass(frozen=True, slots=True)
class LLMConfiguration:
    roles: Mapping[str, ConfiguredRole]

    def for_role(self, role: str) -> ConfiguredRole:
        try:
            return self.roles[role]
        except KeyError:
            raise ConfigurationError(
                f"Unknown LLM role '{role}'."
            ) from None


def _validation_path(error: ValidationError) -> str:
    first = error.errors(include_url=False, include_context=False)[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return location or "document"


def _read_document(path: Path) -> LLMDocument:
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
        return LLMDocument.model_validate(payload)
    except FileNotFoundError:
        raise ConfigurationError("LLM configuration file is missing.") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigurationError("LLM configuration file is invalid.") from None
    except ValidationError as error:
        raise ConfigurationError(
            f"Invalid LLM configuration at '{_validation_path(error)}'."
        ) from None


def _secret_for_provider(
    settings: AppSettings,
    provider: ProviderName,
) -> SecretStr:
    environment_name, value = {
        "deepseek": ("DEEPSEEK_API_KEY", settings.deepseek_api_key),
        "qwen": ("DASHSCOPE_API_KEY", settings.dashscope_api_key),
    }[provider]
    if value is None or not value.get_secret_value():
        raise ConfigurationError(
            f"Missing provider credential '{environment_name}'."
        )
    return value


def _validate_role(
    role_name: str,
    role: RoleDefinition,
    model: ModelDefinition,
) -> None:
    capabilities = model.capabilities
    configured_parameters = {
        name
        for name in ("temperature", "top_p", "top_k", "max_tokens")
        if getattr(role, name) is not None
    }
    unsupported = configured_parameters.difference(
        capabilities.supported_parameters
    )
    if unsupported:
        field = sorted(unsupported)[0]
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field '{field}' is not supported "
            "by its model capability declaration."
        )
    if model.provider == "deepseek" and role.top_k is not None:
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field 'top_k' is not supported "
            "by provider 'deepseek'."
        )
    if role.reasoning_mode is not None and (
        role.reasoning_mode not in capabilities.reasoning_modes
    ):
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field 'reasoning_mode' is not "
            "supported by its model capability declaration."
        )
    if role.reasoning_effort is not None and (
        role.reasoning_effort not in capabilities.reasoning_efforts
    ):
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field 'reasoning_effort' is not "
            "supported by its model capability declaration."
        )
    if role.thinking_budget is not None and not capabilities.supports_thinking_budget:
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field 'thinking_budget' is not "
            "supported by its model capability declaration."
        )
    if role.reasoning_mode == "enabled" and (
        not capabilities.supports_sampling_with_reasoning
        and {"temperature", "top_p", "top_k"}.intersection(configured_parameters)
    ):
        field = sorted(
            {"temperature", "top_p", "top_k"}.intersection(configured_parameters)
        )[0]
        raise ConfigurationError(
            f"Invalid LLM role '{role_name}': field '{field}' is ineffective "
            "while reasoning is enabled."
        )
    if role_name in STRUCTURED_ROLES:
        if model.provider == "qwen" and role.reasoning_mode != "disabled":
            raise ConfigurationError(
                f"Invalid LLM role '{role_name}': field 'reasoning_mode' must "
                "be explicitly disabled for Qwen structured output."
            )
        if role.reasoning_mode is not None and (
            role.reasoning_mode
            not in capabilities.structured_output_reasoning_modes
        ):
            raise ConfigurationError(
                f"Invalid LLM role '{role_name}': field 'reasoning_mode' is not "
                "supported for structured output."
            )


def load_llm_configuration(
    settings: AppSettings,
    path: Path = LLM_CONFIG_FILE,
) -> LLMConfiguration:
    if settings.llm_adapter != "provider":
        raise ConfigurationError("Provider LLM configuration is not enabled.")
    document = _read_document(path)
    roles: dict[str, ConfiguredRole] = {}
    secrets: dict[ProviderName, SecretStr] = {}
    for role_name in ROLE_NAMES:
        role = getattr(document.roles, role_name)
        profile_name = role.model or document.default_model
        model = document.models[profile_name]
        _validate_role(role_name, role, model)
        if model.provider not in secrets:
            secrets[model.provider] = _secret_for_provider(
                settings,
                model.provider,
            )
        reasoning_enabled = role.reasoning_mode == "enabled"
        roles[role_name] = ConfiguredRole(
            role=role_name,
            provider=model.provider,
            model_name=model.name,
            api_key=secrets[model.provider],
            endpoint=PROVIDER_ENDPOINTS[model.provider],
            timeout_seconds=document.defaults.timeout_seconds,
            temperature=role.temperature,
            top_p=role.top_p,
            top_k=role.top_k,
            max_tokens=role.max_tokens,
            reasoning_mode=role.reasoning_mode,
            reasoning_effort=role.reasoning_effort,
            thinking_budget=role.thinking_budget,
            response_format=(
                "json_object" if role_name in STRUCTURED_ROLES else "text"
            ),
            streaming=(
                reasoning_enabled
                and model.capabilities.requires_streaming_for_reasoning
            ),
            context_window_tokens=model.context_window_tokens,
            prompt_limit_tokens=role.prompt_limit_tokens,
            history_token_budget=role.history_token_budget,
            safety_margin_tokens=role.safety_margin_tokens,
        )
    return LLMConfiguration(roles=MappingProxyType(roles))
