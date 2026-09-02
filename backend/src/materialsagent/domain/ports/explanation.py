from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Protocol


PROVIDER_REQUEST_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z"
)


class ExplanationPortError(RuntimeError):
    """Controlled external Explanation adapter failure."""


class ExplanationTimeoutError(ExplanationPortError):
    pass


class ExplanationProviderUnavailableError(ExplanationPortError):
    pass


class ExplanationProtocolError(ExplanationPortError):
    pass


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ExplanationAssetReference:
    asset_id: str
    role: str
    asset_type: str

    def __post_init__(self) -> None:
        if not self.asset_id.strip():
            raise ValueError("asset_id must be non-blank.")
        if self.role not in {"requested_output", "intermediate", "supporting"}:
            raise ValueError("role is not allowed.")
        if self.asset_type != "sem_image":
            raise ValueError("asset_type is not allowed.")


@dataclass(frozen=True, slots=True)
class ExplanationInput:
    result_id: str
    status: str
    requested_outputs: tuple[str, ...]
    completed_outputs: tuple[str, ...]
    failed_outputs: tuple[str, ...]
    data: Mapping[str, object]
    artifacts: tuple[ExplanationAssetReference, ...]
    warnings: tuple[object, ...]
    error: Mapping[str, object] | None
    process_parameters: Mapping[str, object]
    tool_id: str
    tool_version: str
    schema_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "result_id",
            "tool_id",
            "tool_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank.")
        if re.fullmatch(r"[0-9a-f]{64}", self.schema_hash) is None:
            raise ValueError("schema_hash must be lowercase SHA-256 hex.")
        if self.status not in {
            "SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
            "FAILED",
        }:
            raise ValueError("status is not allowed.")
        object.__setattr__(self, "data", _freeze(self.data))
        object.__setattr__(self, "warnings", tuple(_freeze(self.warnings)))
        object.__setattr__(
            self,
            "error",
            None if self.error is None else _freeze(self.error),
        )
        object.__setattr__(
            self,
            "process_parameters",
            _freeze(self.process_parameters),
        )


@dataclass(frozen=True, slots=True)
class ExplanationOutcome:
    text: str | None
    usage: Mapping[str, int] | None
    provider_request_id: str | None
    error_code: str | None
    safe_error_message: str | None
    llm_error_code: str | None = None
    llm_safe_error_message: str | None = None

    def __post_init__(self) -> None:
        if (
            self.provider_request_id is not None
            and PROVIDER_REQUEST_ID_PATTERN.fullmatch(
                self.provider_request_id
            )
            is None
        ):
            raise ValueError(
                "provider_request_id must be a bounded controlled identifier."
            )
        succeeded = self.text is not None
        if succeeded:
            if (
                not self.text.strip()
                or len(self.text) > 4096
                or not self.text.isprintable()
                or self.error_code is not None
                or self.safe_error_message is not None
                or self.llm_error_code is not None
                or self.llm_safe_error_message is not None
            ):
                raise ValueError("Successful outcome requires bounded text only.")
        else:
            if (
                not isinstance(self.error_code, str)
                or not self.error_code.strip()
                or len(self.error_code) > 64
                or not self.error_code.isprintable()
                or not isinstance(self.safe_error_message, str)
                or not self.safe_error_message.strip()
                or len(self.safe_error_message) > 256
                or not self.safe_error_message.isprintable()
            ):
                raise ValueError(
                    "Failed outcome requires a controlled safe error."
                )
            llm_error_present = self.llm_error_code is not None
            llm_message_present = self.llm_safe_error_message is not None
            if llm_error_present != llm_message_present:
                raise ValueError(
                    "Detailed LLM failure fields must be supplied together."
                )
            if llm_error_present and (
                not isinstance(self.llm_error_code, str)
                or not self.llm_error_code.strip()
                or len(self.llm_error_code) > 64
                or not self.llm_error_code.isprintable()
                or not isinstance(self.llm_safe_error_message, str)
                or not self.llm_safe_error_message.strip()
                or len(self.llm_safe_error_message) > 256
                or not self.llm_safe_error_message.isprintable()
            ):
                raise ValueError(
                    "Detailed LLM failure requires a controlled safe error."
                )
        if self.usage is not None:
            if set(self.usage) != {"input_tokens", "output_tokens"} or any(
                type(value) is not int or value < 0
                for value in self.usage.values()
            ):
                raise ValueError("usage must use controlled token counters.")
            object.__setattr__(
                self,
                "usage",
                MappingProxyType(dict(self.usage)),
            )


@dataclass(frozen=True, slots=True)
class ExplanationRequestMetadata:
    provider: str
    model_name: str
    prompt_template_id: str
    prompt_template_version: str
    prompt_digest: str
    generation_parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "model_name",
            "prompt_template_id",
            "prompt_template_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank.")
        if re.fullmatch(r"[0-9a-f]{64}", self.prompt_digest) is None:
            raise ValueError("prompt_digest must be lowercase SHA-256 hex.")
        if not isinstance(self.generation_parameters, Mapping):
            raise ValueError("generation_parameters must be an object.")
        object.__setattr__(
            self,
            "generation_parameters",
            MappingProxyType(dict(self.generation_parameters)),
        )


class ExplanationPort(Protocol):
    provider: str
    model_name: str
    prompt_template_id: str
    prompt_template_version: str

    def request_metadata(
        self,
        value: ExplanationInput,
    ) -> ExplanationRequestMetadata: ...

    def explain(self, value: ExplanationInput) -> ExplanationOutcome: ...
