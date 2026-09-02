from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Annotated, Any, Final, Literal

from langchain_deepseek import ChatDeepSeek
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, ValidationError, model_validator

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput, ChatOrchestrationOutcome, ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError, ChatOrchestrationRequestMetadata,
    ChatOrchestrationTimeoutError, KnowledgeAnswer, ToolCandidateProposal,
    ToolCandidateSet,
)
from materialsagent.infrastructure.config import DeepSeekConfig
from materialsagent.infrastructure.llm.deepseek_common import (
    SAFE_MESSAGES, canonical_prompt_digest, classify_provider_exception,
    controlled_usage, deepseek_client_kwargs, success_provider_request_id,
)


PROMPT_TEMPLATE_ID: Final = "chat-orchestration"
PROMPT_TEMPLATE_VERSION: Final = "5"
GENERATION_PARAMETERS: Final = {
    "temperature": 0, "max_tokens": 1024, "thinking_mode": "disabled",
    "response_format": "json_object", "streaming": False,
}


class ProviderToolCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool_id: StrictStr
    candidate_input: dict[StrictStr, JsonValue]


class ProviderChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    route: Literal["KNOWLEDGE_ANSWER", "TOOL_CANDIDATES"]
    answer_text: StrictStr | None = None
    candidates: Annotated[list[ProviderToolCandidate], Field(min_length=1, max_length=5)] | None = None

    @model_validator(mode="before")
    @classmethod
    def require_route_specific_shape(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("Provider response must be an object.")
        expected = {
            "KNOWLEDGE_ANSWER": {"route", "answer_text"},
            "TOOL_CANDIDATES": {"route", "candidates"},
        }.get(value.get("route"))
        if expected is None or set(value) != expected:
            raise ValueError("Provider response shape does not match route.")
        return value

    @model_validator(mode="after")
    def require_route_values(self) -> ProviderChatResponse:
        if self.route == "KNOWLEDGE_ANSWER":
            if self.answer_text is None or not self.answer_text.strip() or self.candidates is not None:
                raise ValueError("Knowledge route is incomplete.")
        else:
            if self.answer_text is not None or self.candidates is None:
                raise ValueError("Tool candidates route is incomplete.")
            ids = [candidate.tool_id for candidate in self.candidates]
            if any(not item.strip() for item in ids) or len(set(ids)) != len(ids):
                raise ValueError("Tool candidates must use unique non-blank IDs.")
        encoded = json.dumps(
            self.model_dump(exclude_none=True), ensure_ascii=False, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("Provider response exceeds the safe size limit.")
        return self


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _catalog_payload(value: ChatOrchestrationInput) -> list[dict[str, object]]:
    return [
        {
            "tool_id": entry.tool_id, "version": entry.version,
            "schema_hash": entry.schema_hash, "display_name": entry.display_name,
            "description": entry.description,
            "candidate_input_schema": _plain_json(entry.candidate_input_schema),
            "supported_outputs": list(entry.supported_outputs),
        }
        for entry in value.routing_catalog.entries
    ]


def _render_messages(value: ChatOrchestrationInput) -> list[dict[str, str]]:
    catalog = json.dumps(
        _catalog_payload(value), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True,
    )
    system = (
        "You are a single-call materials research router. Use only the supplied "
        "routing catalog. Return exactly one JSON object and no other text. "
        "For a knowledge question return "
        '{"route":"KNOWLEDGE_ANSWER","answer_text":"..."}. '
        "For Tool intent return one to five unique candidates as "
        '{"route":"TOOL_CANDIDATES","candidates":[{"tool_id":"...",'
        '"candidate_input":{}}]}. Candidate input must exactly follow that Tool\'s '
        "candidate_input_schema. Preserve missing values as null and explicit "
        "ambiguity as a controlled candidates array inside candidate_input. "
        "Never return task status, execution permission, Tool version, or schema hash. "
        f"Routing catalog: {catalog}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": value.content_text}]


def _domain_result(value: ProviderChatResponse) -> KnowledgeAnswer | ToolCandidateSet:
    if value.route == "KNOWLEDGE_ANSWER":
        return KnowledgeAnswer(answer_text=value.answer_text or "")
    return ToolCandidateSet(tuple(
        ToolCandidateProposal(candidate.tool_id, candidate.candidate_input)
        for candidate in value.candidates or ()
    ))


class DeepSeekChatAdapter:
    provider = "deepseek"
    model_name = "deepseek-v4-flash"

    def __init__(self, config: DeepSeekConfig, *, structured_runnable: object | None = None,
                 model_factory: Callable[..., object] | None = None) -> None:
        if structured_runnable is None:
            factory = model_factory or ChatDeepSeek
            model = factory(**deepseek_client_kwargs(config, max_tokens=1024))
            structured_runnable = model.with_structured_output(
                ProviderChatResponse, method="json_mode", include_raw=True,
            )
        self._structured_runnable = structured_runnable

    def request_metadata(self, orchestration_input: ChatOrchestrationInput) -> ChatOrchestrationRequestMetadata:
        messages = _render_messages(orchestration_input)
        return ChatOrchestrationRequestMetadata(
            provider=self.provider, model_name=self.model_name,
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            prompt_digest=canonical_prompt_digest(
                template_id=PROMPT_TEMPLATE_ID,
                template_version=PROMPT_TEMPLATE_VERSION,
                messages=messages,
            ),
            generation_parameters=GENERATION_PARAMETERS,
        )

    def orchestrate(self, orchestration_input: ChatOrchestrationInput) -> ChatOrchestrationOutcome:
        messages = _render_messages(orchestration_input)
        try:
            envelope = self._structured_runnable.invoke(messages)
        except json.JSONDecodeError:
            self._raise_protocol("LLM_INVALID_JSON")
        except (OutputParserException, ValidationError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        except Exception as error:
            failure = classify_provider_exception(error)
            error_type = ChatOrchestrationTimeoutError if failure.error_code == "LLM_TIMEOUT" else ChatOrchestrationProviderError
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
        if envelope.get("parsing_error") is not None:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                self._raise_protocol("LLM_INVALID_JSON")
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        try:
            result = _domain_result(ProviderChatResponse.model_validate(envelope.get("parsed")))
        except (ValidationError, ValueError, TypeError):
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        return ChatOrchestrationOutcome(
            result=result, usage=controlled_usage(raw),
            provider_request_id=success_provider_request_id(raw),
        )

    @staticmethod
    def _raise_protocol(error_code: str) -> Any:
        raise ChatOrchestrationProtocolError(
            error_code=error_code, safe_error_message=SAFE_MESSAGES[error_code],
        )
