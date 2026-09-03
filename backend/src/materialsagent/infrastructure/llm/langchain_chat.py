from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Annotated, Any, Literal

from langchain_core.exceptions import OutputParserException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    ValidationError,
    model_validator,
)

from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput,
    ChatOrchestrationOutcome,
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationRequestMetadata,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    ToolCandidateProposal,
    ToolCandidateSet,
)
from materialsagent.infrastructure.llm.common import (
    PromptRenderCache,
    SAFE_MESSAGES,
    canonical_prompt_digest,
    classify_provider_exception,
    controlled_usage,
    success_provider_request_id,
)
from materialsagent.infrastructure.llm.configuration import ConfiguredRole
from materialsagent.infrastructure.llm.factory import create_chat_model
from materialsagent.infrastructure.llm.prompts import (
    CHAT_ORCHESTRATION_PROMPT_ID,
    CHAT_ORCHESTRATION_PROMPT_VERSION,
    render_chat_orchestration_prompt,
)


class ProviderToolCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool_id: StrictStr
    candidate_input: dict[StrictStr, JsonValue]


class ProviderChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    route: Literal["KNOWLEDGE_ANSWER", "TOOL_CANDIDATES"]
    answer_text: StrictStr | None = None
    candidates: Annotated[
        list[ProviderToolCandidate],
        Field(min_length=1, max_length=5),
    ] | None = None

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
            if (
                self.answer_text is None
                or not self.answer_text.strip()
                or self.candidates is not None
            ):
                raise ValueError("Knowledge route is incomplete.")
        else:
            if self.answer_text is not None or self.candidates is None:
                raise ValueError("Tool candidates route is incomplete.")
            identifiers = [candidate.tool_id for candidate in self.candidates]
            if any(not item.strip() for item in identifiers) or len(
                set(identifiers)
            ) != len(identifiers):
                raise ValueError("Tool candidates must use unique non-blank IDs.")
        encoded = json.dumps(
            self.model_dump(exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("Provider response exceeds the safe size limit.")
        return self


def _domain_result(
    value: ProviderChatResponse,
) -> KnowledgeAnswer | ToolCandidateSet:
    if value.route == "KNOWLEDGE_ANSWER":
        return KnowledgeAnswer(answer_text=value.answer_text or "")
    return ToolCandidateSet(
        tuple(
            ToolCandidateProposal(candidate.tool_id, candidate.candidate_input)
            for candidate in value.candidates or ()
        )
    )


class LangChainChatOrchestrationAdapter:
    prompt_template_id = CHAT_ORCHESTRATION_PROMPT_ID
    prompt_template_version = CHAT_ORCHESTRATION_PROMPT_VERSION

    def __init__(
        self,
        config: ConfiguredRole,
        *,
        chat_model: object | None = None,
        structured_runnable: object | None = None,
    ) -> None:
        if config.role != "chat_orchestration":
            raise ValueError("Chat adapter requires chat_orchestration config.")
        self._config = config
        self._prompt_cache = PromptRenderCache()
        self.provider = config.provider
        self.model_name = config.model_name
        if structured_runnable is None:
            model = chat_model or create_chat_model(config)
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
        messages = render_chat_orchestration_prompt(orchestration_input)
        self._prompt_cache.store(orchestration_input, messages)
        return ChatOrchestrationRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id=self.prompt_template_id,
            prompt_template_version=self.prompt_template_version,
            prompt_digest=canonical_prompt_digest(
                template_id=self.prompt_template_id,
                template_version=self.prompt_template_version,
                messages=messages,
            ),
            generation_parameters=self._config.generation_parameters,
        )

    def orchestrate(
        self,
        orchestration_input: ChatOrchestrationInput,
    ) -> ChatOrchestrationOutcome:
        messages = self._prompt_cache.take(orchestration_input)
        if messages is None:
            messages = render_chat_orchestration_prompt(orchestration_input)
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
        if envelope.get("parsing_error") is not None:
            try:
                json.loads(content)
            except json.JSONDecodeError:
                self._raise_protocol("LLM_INVALID_JSON")
            self._raise_protocol("LLM_SCHEMA_MISMATCH")
        try:
            parsed = ProviderChatResponse.model_validate(envelope.get("parsed"))
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
