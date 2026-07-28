from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Annotated, Any, Final, Literal

from langchain_deepseek import ChatDeepSeek
from langchain_core.exceptions import OutputParserException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from materialsagent.domain.ports.chat_orchestration import (
    AmbiguousValue,
    ChatOrchestrationInput,
    ChatOrchestrationOutcome,
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationRequestMetadata,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    NeedsInputCandidate,
    ParameterCandidate,
    ToolCandidate,
    ZTA35GParameterCandidates,
)
from materialsagent.infrastructure.config import DeepSeekConfig
from materialsagent.infrastructure.llm.deepseek_common import (
    SAFE_MESSAGES,
    canonical_prompt_digest,
    classify_provider_exception,
    controlled_usage,
    deepseek_client_kwargs,
    success_provider_request_id,
)


PROMPT_TEMPLATE_ID: Final = "chat-orchestration"
PROMPT_TEMPLATE_VERSION: Final = "2"
GENERATION_PARAMETERS: Final = {
    "temperature": 0,
    "max_tokens": 1024,
    "thinking_mode": "disabled",
    "response_format": "json_object",
    "streaming": False,
}
PARAMETER_FIELDS: Final = (
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
)

ControlledScalar = StrictStr | StrictBool | StrictInt | StrictFloat | None


class ProviderAmbiguousValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidates: Annotated[list[ControlledScalar], Field(min_length=2)]


ProviderCandidateValue = ControlledScalar | ProviderAmbiguousValue


class ProviderParameterCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: ProviderCandidateValue
    unit: ProviderCandidateValue


class ProviderParameterCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    solution_temperature: ProviderParameterCandidate | None
    solution_time: ProviderParameterCandidate | None
    aging_temperature: ProviderParameterCandidate | None
    aging_time: ProviderParameterCandidate | None


class ProviderChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route: Literal["KNOWLEDGE_ANSWER", "TOOL_EXECUTION", "NEEDS_INPUT"]
    answer_text: StrictStr | None = None
    tool_id: Literal["zta35g_sem_virtual_lab"] | None = None
    material: ProviderCandidateValue = None
    candidate_parameters: ProviderParameterCandidates | None = None
    requested_outputs: list[
        Literal["sem_image", "mechanical_properties"]
    ] | None = None
    missing_fields: list[StrictStr] | None = None
    ambiguous_fields: list[StrictStr] | None = None
    follow_up_suggestion: StrictStr | None = None

    @model_validator(mode="before")
    @classmethod
    def require_route_specific_shape(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("Provider response must be an object.")
        route = value.get("route")
        common = {
            "route",
            "tool_id",
            "material",
            "candidate_parameters",
            "requested_outputs",
        }
        expected = {
            "KNOWLEDGE_ANSWER": {"route", "answer_text"},
            "TOOL_EXECUTION": common,
            "NEEDS_INPUT": common
            | {
                "missing_fields",
                "ambiguous_fields",
                "follow_up_suggestion",
            },
        }.get(route)
        if expected is None or set(value) != expected:
            raise ValueError("Provider response shape does not match route.")
        return value


def _render_messages(
    value: ChatOrchestrationInput,
) -> list[dict[str, str]]:
    system = (
        "Return exactly one JSON object and no other text. JSON is required. "
        "Do not use a Markdown code fence and do not explain the JSON. "
        "Use only one of these schema examples: "
        '{"route":"KNOWLEDGE_ANSWER","answer_text":"schema text"}; '
        '{"route":"TOOL_EXECUTION","tool_id":"zta35g_sem_virtual_lab",'
        '"material":"ZTA35G","candidate_parameters":{'
        '"solution_temperature":{"value":1000,"unit":"°C"},'
        '"solution_time":{"value":3,"unit":"h"},'
        '"aging_temperature":{"value":730,"unit":"°C"},'
        '"aging_time":{"value":3,"unit":"h"}},'
        '"requested_outputs":["sem_image"]}; '
        '{"route":"NEEDS_INPUT","tool_id":"zta35g_sem_virtual_lab",'
        '"material":"ZTA35G","candidate_parameters":{'
        '"solution_temperature":null,"solution_time":null,'
        '"aging_temperature":null,"aging_time":null},'
        '"missing_fields":["field_name"],"ambiguous_fields":[],'
        '"follow_up_suggestion":"ask for the missing field",'
        '"requested_outputs":["sem_image"]}. '
        "When a parameter is unknown, use NEEDS_INPUT; never guess it."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": value.content_text},
    ]


def _candidate(value: ProviderCandidateValue) -> object:
    if isinstance(value, ProviderAmbiguousValue):
        return AmbiguousValue(tuple(value.candidates))
    return value


def _parameters(
    value: ProviderParameterCandidates,
) -> ZTA35GParameterCandidates:
    decoded: dict[str, ParameterCandidate | None] = {}
    for field_name in PARAMETER_FIELDS:
        raw = getattr(value, field_name)
        decoded[field_name] = (
            None
            if raw is None
            else ParameterCandidate(
                value=_candidate(raw.value),
                unit=_candidate(raw.unit),
            )
        )
    return ZTA35GParameterCandidates(**decoded)


def _domain_result(
    value: ProviderChatResponse,
) -> KnowledgeAnswer | ToolCandidate | NeedsInputCandidate:
    if value.route == "KNOWLEDGE_ANSWER":
        if not isinstance(value.answer_text, str):
            raise ValueError("Provider route is missing required values.")
        return KnowledgeAnswer(answer_text=value.answer_text)
    if value.route not in {"TOOL_EXECUTION", "NEEDS_INPUT"}:
        raise ValueError("Provider route is not supported.")
    if (
        value.tool_id != "zta35g_sem_virtual_lab"
        or not isinstance(
            value.candidate_parameters,
            ProviderParameterCandidates,
        )
        or not isinstance(value.requested_outputs, list)
    ):
        raise ValueError("Provider route is missing required values.")
    common: dict[str, object] = {
        "tool_id": value.tool_id,
        "material": _candidate(value.material),
        "candidate_parameters": _parameters(value.candidate_parameters),
        "requested_outputs": tuple(value.requested_outputs),
    }
    if value.route == "TOOL_EXECUTION":
        return ToolCandidate(**common)  # type: ignore[arg-type]
    if (
        not isinstance(value.missing_fields, list)
        or not isinstance(value.ambiguous_fields, list)
        or not isinstance(value.follow_up_suggestion, str)
    ):
        raise ValueError("Provider route is missing required values.")
    return NeedsInputCandidate(
        **common,  # type: ignore[arg-type]
        missing_fields=tuple(value.missing_fields),
        ambiguous_fields=tuple(value.ambiguous_fields),
        follow_up_suggestion=value.follow_up_suggestion,
    )


class DeepSeekChatAdapter:
    provider = "deepseek"
    model_name = "deepseek-v4-flash"

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        structured_runnable: object | None = None,
        model_factory: Callable[..., object] | None = None,
    ) -> None:
        if structured_runnable is None:
            factory = model_factory or ChatDeepSeek
            model = factory(
                **deepseek_client_kwargs(config, max_tokens=1024)
            )
            structured_runnable = model.with_structured_output(  # type: ignore[attr-defined]
                ProviderChatResponse,
                method="json_mode",
                include_raw=True,
            )
        self._structured_runnable = structured_runnable

    def request_metadata(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationRequestMetadata:
        messages = _render_messages(orchestration_input)
        return ChatOrchestrationRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            prompt_digest=canonical_prompt_digest(
                template_id=PROMPT_TEMPLATE_ID,
                template_version=PROMPT_TEMPLATE_VERSION,
                messages=messages,
            ),
            generation_parameters=GENERATION_PARAMETERS,
        )

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationOutcome:
        messages = _render_messages(orchestration_input)
        try:
            envelope = self._structured_runnable.invoke(messages)  # type: ignore[attr-defined]
        except json.JSONDecodeError:
            self._raise_protocol("LLM_INVALID_JSON")
        except (OutputParserException, ValidationError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        except Exception as error:
            failure = classify_provider_exception(error)
            error_type = (
                ChatOrchestrationTimeoutError
                if failure.error_code == "LLM_TIMEOUT"
                else ChatOrchestrationProviderError
            )
            raise error_type(
                error_code=failure.error_code,
                safe_error_message=failure.safe_error_message,
                provider_request_id=failure.provider_request_id,
            ) from None

        if not isinstance(envelope, Mapping):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        raw = envelope.get("raw")
        content = getattr(raw, "content", None)
        if raw is None or not isinstance(content, str) or not content.strip():
            self._raise_protocol("LLM_EMPTY_RESPONSE")
        parsing_error = envelope.get("parsing_error")
        if parsing_error is not None:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                self._raise_protocol("LLM_INVALID_JSON")
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        try:
            parsed = ProviderChatResponse.model_validate(
                envelope.get("parsed")
            )
            result = _domain_result(parsed)
        except (ValidationError, ValueError, TypeError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        return ChatOrchestrationOutcome(
            result=result,
            usage=controlled_usage(raw),
            provider_request_id=success_provider_request_id(raw),
        )

    @staticmethod
    def _raise_protocol(error_code: str) -> Any:
        raise ChatOrchestrationProtocolError(
            error_code=error_code,
            safe_error_message=SAFE_MESSAGES[error_code],
        )
