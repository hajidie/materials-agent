from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import logging
import math
import re
import unicodedata
from typing import Final

from materialsagent.application.conversation_context import ConversationContextBuilder
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import (
    Clock,
    IdFactory,
    _default_clock,
    _default_id_factory,
    _validated_utc_now,
)
from materialsagent.application.errors import (
    AgentInternalError,
    ApplicationConflictError,
    OrchestrationOutcomeError,
    ResourceNotFoundError,
    ToolExecutionNotAllowedError,
    ToolSchemaDriftError,
    from_persistence_error,
)
from materialsagent.application.tool_registry import (
    ToolAuthorizationDenialReason,
    ToolAuthorizationError,
    UnknownToolError,
)
from materialsagent.application.messages import (
    NEW_TASK,
    SUPPLEMENT_TASK,
    PreparedSubmission,
)
from materialsagent.domain.models.llm_call import (
    CHAT_ORCHESTRATION,
    FAILED as LLM_FAILED,
    PENDING as LLM_PENDING,
    RUNNING as LLM_RUNNING,
    SUCCEEDED as LLM_SUCCEEDED,
    TOOL_INPUT_EXTRACTION,
    LLMCall,
)
from materialsagent.domain.models.context_snapshot import build_context_snapshot
from materialsagent.domain.models.message import (
    ASSISTANT,
    LLM,
    Message,
)
from materialsagent.domain.models.task import (
    FAILED as TASK_FAILED,
    KNOWLEDGE_QA,
    NEEDS_INPUT as TASK_NEEDS_INPUT,
    PENDING as TASK_PENDING,
    READY as TASK_READY,
    RUNNING as TASK_RUNNING,
    SUCCEEDED as TASK_SUCCEEDED,
    TOOL_EXECUTION,
    Task,
)
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationProtocolError,
    ChatOrchestrationProviderError,
    ChatOrchestrationInput,
    ChatOrchestrationOutcome,
    ChatOrchestrationPort,
    ChatOrchestrationRequestMetadata,
    ChatOrchestrationTimeoutError,
    HistoryReference,
    KnowledgeAnswer,
    ToolCandidateProposal,
    ToolCandidateSet,
)
from materialsagent.domain.ports.conversation_context import (
    ContextBudget,
    ContextBuildResult,
    PromptContextWindow,
    TokenCounter,
)
from materialsagent.domain.ports.tool_registry import (
    InvalidNormalization,
    NeedsInputNormalization,
    ReadyNormalization,
    RoutingCatalogSnapshot,
    ToolAction,
    ToolNormalization,
    ToolRef,
)
from materialsagent.domain.ports.tool_input_extraction import (
    ToolInputExtractionInput,
    ToolInputExtractionOutcome,
    ToolInputExtractionPort,
    ToolInputExtractionProtocolError,
    ToolInputExtractionProviderError,
    ToolInputExtractionRequestMetadata,
    ToolInputExtractionTimeoutError,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)
FOLLOW_UP_TEXT: Final = "请补充缺失参数或明确存在歧义的参数。"
VALIDATION_ERROR_MESSAGE: Final = "输入参数未通过验证。"
TOOL_UNAVAILABLE_MESSAGE: Final = "当前阶段尚未开放材料工具执行。"
TIMEOUT_MESSAGE: Final = "聊天编排服务响应超时。"
ORCHESTRATION_FAILURE_MESSAGE: Final = "聊天编排服务暂不可用。"
PROTOCOL_FAILURE_MESSAGE: Final = "聊天编排服务返回了无效响应。"
AGENT_INTERNAL_ERROR_MESSAGE: Final = "智能体内部处理失败。"
CONTEXT_BUDGET_MESSAGE: Final = "当前消息超过模型上下文预算。"


logger = logging.getLogger("materialsagent.chat_orchestration")


@dataclass(frozen=True, slots=True)
class ChatOrchestrationProjection:
    conversation_id: str
    user_message: Message
    task: Task
    llm_call: LLMCall | None
    assistant_message: Message | None
    revision: TaskInputRevision | None


@dataclass(frozen=True, slots=True)
class _StartedChatCall:
    call: LLMCall
    invoke_adapter: bool


@dataclass(frozen=True, slots=True)
class _ResolvedToolRoute:
    proposal_set: ToolCandidateSet
    candidate_refs: tuple[ToolRef, ...]
    selected_ref: ToolRef | None
    selected_input: Mapping[str, object] | None
    normalization: ToolNormalization | None


@dataclass(frozen=True, slots=True)
class _BoundSupplementSnapshot:
    bound_tool_ref: ToolRef
    latest_revision: TaskInputRevision
    revision_history: tuple[TaskInputRevision, ...]
    prior_normalized_input: Mapping[str, object]


class ChatOrchestrationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        orchestration_port: ChatOrchestrationPort,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        tool_chain_enabled: bool = False,
        tool_registry: object | None = None,
        tool_input_extraction_port: ToolInputExtractionPort | None = None,
        context_builder: ConversationContextBuilder | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._orchestration_port = orchestration_port
        self._provider = self._required_adapter_text("provider")
        self._model_name = self._required_adapter_text("model_name")
        self._clock = clock or _default_clock
        self._id_factory = id_factory or _default_id_factory
        self._tool_chain_enabled = tool_chain_enabled
        if tool_registry is None:
            from materialsagent.application.tool_registry import ToolRegistry
            from materialsagent.application.zta35g_tool import (
                build_zta35g_tool_definition,
            )

            tool_registry = ToolRegistry((build_zta35g_tool_definition(),))
        self._tool_registry = tool_registry
        self._tool_input_extraction_port = tool_input_extraction_port
        self._context_builder = context_builder or ConversationContextBuilder(
            unit_of_work_factory,
            tool_registry,
        )
        if token_counter is None:
            from materialsagent.infrastructure.llm.token_counter import (
                Cl100kTokenCounter,
            )

            token_counter = Cl100kTokenCounter()
        self._token_counter = token_counter

    def configured_for_tool_chain(
        self,
        *,
        enabled: bool,
    ) -> ChatOrchestrationService:
        if self._tool_chain_enabled is enabled:
            return self
        return ChatOrchestrationService(
            self._unit_of_work_factory,
            self._orchestration_port,
            clock=self._clock,
            id_factory=self._id_factory,
            tool_chain_enabled=enabled,
            tool_registry=self._tool_registry,
            tool_input_extraction_port=self._tool_input_extraction_port,
            context_builder=self._context_builder,
            token_counter=self._token_counter,
        )

    def orchestrate_submission(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        resume_call: LLMCall | None = None,
    ) -> ChatOrchestrationProjection:
        if submission.submission_mode == SUPPLEMENT_TASK:
            return self._orchestrate_bound_supplement(
                actor_context,
                submission,
            )
        catalog = self._tool_registry.routing_snapshot()
        if not isinstance(catalog, RoutingCatalogSnapshot):
            raise ChatOrchestrationProtocolError(
                "Tool Registry returned an invalid routing snapshot."
            )
        def input_for(window: PromptContextWindow) -> ChatOrchestrationInput:
            return ChatOrchestrationInput(
                task_id=submission.task.task_id,
                conversation_id=submission.conversation_id,
                request_id=submission.user_message.request_id,
                content_text=submission.user_message.content_text,
                routing_catalog=catalog,
                context_window=window,
            )

        context_result = self._context_builder.build(
            purpose=CHAT_ORCHESTRATION,
            actor_id=actor_context.actor_id,
            conversation_id=submission.conversation_id,
            current_message=submission.user_message,
            task_id=None,
            agent_state={},
            budget=self._context_budget(
                self._orchestration_port,
                purpose=CHAT_ORCHESTRATION,
            ),
            prompt_token_counter=lambda window: self._chat_prompt_tokens(
                input_for(window)
            ),
        )
        orchestration_input = input_for(context_result.window)
        metadata = self._orchestration_port.request_metadata(
            orchestration_input
        )
        if (
            not isinstance(metadata, ChatOrchestrationRequestMetadata)
            or metadata.provider != self._provider
            or metadata.model_name != self._model_name
        ):
            raise ChatOrchestrationProtocolError(
                "Chat orchestration metadata violated the protocol."
            )
        context_snapshot = build_context_snapshot(
            context_result,
            purpose=CHAT_ORCHESTRATION,
            model_name=metadata.model_name,
            prompt_digest=metadata.prompt_digest,
        )
        call = resume_call or self._prepare_call(
            actor_context,
            submission,
            metadata,
            orchestration_input,
            context_snapshot=context_snapshot,
        )
        if resume_call is not None and not self._call_matches_request(
            resume_call,
            submission=submission,
            metadata=metadata,
            orchestration_input=orchestration_input,
            context_snapshot=context_snapshot,
        ):
            raise self._conflict(submission)
        started = self._start_call(actor_context, submission, call)
        if not started.invoke_adapter:
            return self.load_current_submission(actor_context, submission)
        call = started.call
        if context_result.budget_exceeded:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                llm_error_code="CONTEXT_BUDGET_EXCEEDED",
                llm_safe_error_message=CONTEXT_BUDGET_MESSAGE,
                task_error_code="CONTEXT_BUDGET_EXCEEDED",
                task_safe_error_message=CONTEXT_BUDGET_MESSAGE,
                provider_request_id=None,
            )
            raise self._outcome_error(
                submission,
                status_code=422,
                code="CONTEXT_BUDGET_EXCEEDED",
                message=CONTEXT_BUDGET_MESSAGE,
            )
        self._log_event("chat_orchestration_started", submission, call.llm_call_id)
        try:
            outcome = self._orchestration_port.orchestrate(
                orchestration_input
            )
            if not isinstance(outcome, ChatOrchestrationOutcome):
                raise ChatOrchestrationProtocolError(
                    "Chat orchestration outcome has an invalid runtime type."
                )
            result = outcome.result
            if (
                submission.submission_mode == SUPPLEMENT_TASK
                and isinstance(result, KnowledgeAnswer)
            ):
                raise ChatOrchestrationProtocolError(
                    "A task supplement must remain a tool candidate."
                )
            resolved_result: KnowledgeAnswer | _ResolvedToolRoute = (
                result
                if isinstance(result, KnowledgeAnswer)
                else self._resolve_tool_candidates(
                    result,
                    catalog,
                    context_result=context_result,
                    actor_context=actor_context,
                    current_text=submission.user_message.content_text,
                    conversation_id=submission.conversation_id,
                )
            )
        except ChatOrchestrationTimeoutError as error:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                llm_error_code=error.error_code,
                llm_safe_error_message=error.safe_error_message,
                task_error_code="UPSTREAM_TIMEOUT",
                task_safe_error_message=TIMEOUT_MESSAGE,
                provider_request_id=error.provider_request_id,
            )
            self._log_event(
                "chat_orchestration_failed",
                submission,
                call.llm_call_id,
                status=LLM_FAILED,
                error_code="UPSTREAM_TIMEOUT",
            )
            raise self._outcome_error(
                submission,
                status_code=504,
                code="UPSTREAM_TIMEOUT",
                message=TIMEOUT_MESSAGE,
            ) from None
        except ChatOrchestrationProviderError as error:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                llm_error_code=error.error_code,
                llm_safe_error_message=error.safe_error_message,
                task_error_code="CHAT_ORCHESTRATION_FAILED",
                task_safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
                provider_request_id=error.provider_request_id,
            )
            self._log_event(
                "chat_orchestration_failed",
                submission,
                call.llm_call_id,
                status=LLM_FAILED,
                error_code="CHAT_ORCHESTRATION_FAILED",
            )
            raise self._outcome_error(
                submission,
                status_code=503,
                code="CHAT_ORCHESTRATION_FAILED",
                message=ORCHESTRATION_FAILURE_MESSAGE,
            ) from None
        except ChatOrchestrationProtocolError as error:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                llm_error_code=error.error_code,
                llm_safe_error_message=error.safe_error_message,
                task_error_code="AGENT_INTERNAL_ERROR",
                task_safe_error_message=AGENT_INTERNAL_ERROR_MESSAGE,
                provider_request_id=error.provider_request_id,
            )
            self._log_event(
                "chat_orchestration_failed",
                submission,
                call.llm_call_id,
                status=LLM_FAILED,
                error_code="CHAT_ORCHESTRATION_FAILED",
            )
            raise self._outcome_error(
                submission,
                status_code=500,
                code="AGENT_INTERNAL_ERROR",
                message=AGENT_INTERNAL_ERROR_MESSAGE,
            ) from None
        except Exception:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                llm_error_code="CHAT_ORCHESTRATION_FAILED",
                llm_safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
                task_error_code="CHAT_ORCHESTRATION_FAILED",
                task_safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
                provider_request_id=None,
            )
            self._log_event(
                "chat_orchestration_failed",
                submission,
                call.llm_call_id,
                status=LLM_FAILED,
                error_code="CHAT_ORCHESTRATION_FAILED",
            )
            raise self._outcome_error(
                submission,
                status_code=503,
                code="CHAT_ORCHESTRATION_FAILED",
                message=ORCHESTRATION_FAILURE_MESSAGE,
            ) from None

        projection = self.finalize_result(
            actor_context,
            submission,
            llm_call_id=call.llm_call_id,
            result=resolved_result,
            usage=outcome.usage,
            provider_request_id=outcome.provider_request_id,
        )
        self._log_event(
            "chat_orchestration_completed",
            submission,
            call.llm_call_id,
            status=projection.llm_call.status,
            error_code=projection.task.error_code,
        )
        if projection.task.error_code == "VALIDATION_FAILED":
            details = (
                projection.revision.validation_errors
                if projection.revision is not None
                else []
            )
            raise self._outcome_error(
                submission,
                status_code=422,
                code="VALIDATION_FAILED",
                message=VALIDATION_ERROR_MESSAGE,
                details=details,
            )
        if projection.task.error_code == "TOOL_UNAVAILABLE":
            raise self._outcome_error(
                submission,
                status_code=503,
                code="TOOL_UNAVAILABLE",
                message=TOOL_UNAVAILABLE_MESSAGE,
            )
        return projection

    def resume_or_load_submission(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> ChatOrchestrationProjection:
        if submission.submission_mode == SUPPLEMENT_TASK:
            try:
                with self._unit_of_work_factory() as unit_of_work:
                    task = unit_of_work.tasks.get_owned_for_update(
                        submission.task.task_id,
                        actor_context.actor_id,
                    )
                    message = unit_of_work.messages.get(
                        submission.user_message.message_id
                    )
                    if task is None or message is None:
                        raise ResourceNotFoundError()
                    self._require_source_identity(
                        actor_context,
                        submission,
                        message,
                        task,
                    )
                    runs = unit_of_work.tool_runs.list_for_task(task.task_id)
                    calls = [
                        call
                        for call in unit_of_work.llm_calls.list_for_task(
                            task.task_id,
                            request_id=message.request_id,
                        )
                        if call.purpose == TOOL_INPUT_EXTRACTION
                    ]
            except PersistenceError as error:
                raise from_persistence_error(
                    error,
                    conversation_id=submission.conversation_id,
                    task_id=submission.task.task_id,
                ) from None
            if task.current_status in {
                TASK_READY,
                TASK_SUCCEEDED,
                TASK_FAILED,
            } or runs:
                return self.load_current_submission(actor_context, submission)
            active = [
                call
                for call in calls
                if call.status in {LLM_PENDING, LLM_RUNNING}
            ]
            if len(active) > 1:
                raise self._conflict(submission)
            if active:
                if active[0].status == LLM_RUNNING:
                    return self.load_current_submission(actor_context, submission)
                return self.orchestrate_submission(actor_context, submission)
            ordinary_terminal = [
                call
                for call in calls
                if not (
                    call.status == LLM_FAILED
                    and call.error_code == "PROCESS_INTERRUPTED_BEFORE_START"
                )
            ]
            if ordinary_terminal:
                return self.load_current_submission(actor_context, submission)
            if task.current_status == TASK_NEEDS_INPUT:
                return self.orchestrate_submission(actor_context, submission)
            return self.load_current_submission(actor_context, submission)
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
                message = unit_of_work.messages.get(submission.user_message.message_id)
                if task is None or message is None:
                    raise ResourceNotFoundError()
                self._require_source_identity(
                    actor_context,
                    submission,
                    message,
                    task,
                )
                runs = unit_of_work.tool_runs.list_for_task(task.task_id)
                calls = [
                    call
                    for call in unit_of_work.llm_calls.list_for_task(
                        task.task_id,
                        request_id=message.request_id,
                    )
                    if call.purpose == CHAT_ORCHESTRATION
                ]
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        if task.current_status in {
            TASK_NEEDS_INPUT,
            TASK_READY,
            TASK_SUCCEEDED,
            TASK_FAILED,
        } or runs:
            return self.load_current_submission(actor_context, submission)
        active = [call for call in calls if call.status in {LLM_PENDING, LLM_RUNNING}]
        if len(active) > 1:
            raise self._conflict(submission)
        if active:
            call = active[0]
            if call.status == LLM_RUNNING:
                return self.load_current_submission(actor_context, submission)
            return self.orchestrate_submission(
                actor_context,
                submission,
                resume_call=call,
            )
        ordinary_terminal = [
            call
            for call in calls
            if not (
                call.status == LLM_FAILED
                and call.error_code == "PROCESS_INTERRUPTED_BEFORE_START"
            )
        ]
        if ordinary_terminal:
            return self.load_current_submission(actor_context, submission)
        if task.current_status == TASK_PENDING:
            return self.orchestrate_submission(actor_context, submission)
        return self.load_current_submission(actor_context, submission)

    def _orchestrate_bound_supplement(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> ChatOrchestrationProjection:
        snapshot = self._load_bound_supplement_snapshot(
            actor_context,
            submission,
        )
        try:
            definition = self._tool_registry.resolve(
                snapshot.bound_tool_ref.tool_id
            )
        except UnknownToolError:
            raise ToolExecutionNotAllowedError(
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        try:
            authorization = self._tool_registry.authorize(
                registration=definition,
                action=ToolAction.SUPPLEMENT,
                bound_ref=snapshot.bound_tool_ref,
            )
        except ToolAuthorizationError as error:
            error_type = (
                ToolSchemaDriftError
                if error.reason is ToolAuthorizationDenialReason.SCHEMA_DRIFT
                else ToolExecutionNotAllowedError
            )
            raise error_type(
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        current_ref = authorization.authorized_ref
        extractor = self._tool_input_extraction_port
        if extractor is None:
            raise AgentInternalError(
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            )
        missing_fields = tuple(snapshot.latest_revision.missing_fields)
        ambiguous_fields = tuple(
            item["field"]
            for item in snapshot.latest_revision.ambiguous_fields
            if isinstance(item, Mapping)
            and type(item.get("field")) is str
        )

        def command_for(window: PromptContextWindow) -> ToolInputExtractionInput:
            return ToolInputExtractionInput(
                content_text=submission.user_message.content_text,
                tool_context_ref=current_ref,
                candidate_input_schema=definition.candidate_input_schema,
                missing_fields=missing_fields,
                ambiguous_fields=ambiguous_fields,
                context_window=window,
            )

        context_result = self._context_builder.build(
            purpose=TOOL_INPUT_EXTRACTION,
            actor_id=actor_context.actor_id,
            conversation_id=submission.conversation_id,
            current_message=submission.user_message,
            task_id=submission.task.task_id,
            agent_state={
                "tool_id": current_ref.tool_id,
                "prior_normalized_input": self._plain_json(
                    snapshot.prior_normalized_input
                ),
                "missing_fields": list(missing_fields),
                "ambiguous_fields": list(ambiguous_fields),
            },
            budget=self._context_budget(
                extractor,
                purpose=TOOL_INPUT_EXTRACTION,
            ),
            prompt_token_counter=lambda window: self._tool_prompt_tokens(
                extractor,
                command_for(window),
            ),
        )
        command = command_for(context_result.window)
        request_metadata = getattr(extractor, "request_metadata", None)
        metadata = request_metadata(command) if callable(request_metadata) else None
        if metadata is not None and (
            not hasattr(metadata, "prompt_digest")
            or metadata.provider != getattr(extractor, "provider", None)
            or metadata.model_name != getattr(extractor, "model_name", None)
        ):
            raise ToolInputExtractionProtocolError()
        context_snapshot = (
            None
            if metadata is None
            else build_context_snapshot(
                context_result,
                purpose=TOOL_INPUT_EXTRACTION,
                model_name=metadata.model_name,
                prompt_digest=metadata.prompt_digest,
            )
        )
        if context_result.budget_exceeded:
            if metadata is None or context_snapshot is None:
                raise AgentInternalError(
                    conversation_id=submission.conversation_id,
                    task_id=submission.task.task_id,
                )
            self._persist_bound_supplement_budget_failure(
                actor_context,
                submission,
                snapshot=snapshot,
                current_ref=current_ref,
                metadata=metadata,
                context_snapshot=context_snapshot,
            )
            raise self._outcome_error(
                submission,
                status_code=422,
                code="CONTEXT_BUDGET_EXCEEDED",
                message=CONTEXT_BUDGET_MESSAGE,
            )
        if metadata is None or context_snapshot is None:
            raise AgentInternalError(
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            )
        pending_call = self._prepare_bound_supplement_call(
            actor_context,
            submission,
            snapshot=snapshot,
            current_ref=current_ref,
            metadata=metadata,
            context_snapshot=context_snapshot,
        )
        started = self._start_bound_supplement_call(
            actor_context,
            submission,
            pending_call,
        )
        if not started.invoke_adapter:
            return self.load_current_submission(actor_context, submission)
        running_call = started.call
        assert running_call.started_at is not None
        outcome: ToolInputExtractionOutcome | None = None
        try:
            outcome = extractor.extract(command)
            if not isinstance(outcome, ToolInputExtractionOutcome):
                raise ToolInputExtractionProtocolError()
            outcome_metadata = outcome.request_metadata
            if (
                outcome_metadata.provider != getattr(extractor, "provider", None)
                or outcome_metadata.model_name != getattr(extractor, "model_name", None)
                or (metadata is not None and outcome_metadata != metadata)
            ):
                raise ToolInputExtractionProtocolError()
            metadata = outcome_metadata
            if context_snapshot is None:
                context_snapshot = build_context_snapshot(
                    context_result,
                    purpose=TOOL_INPUT_EXTRACTION,
                    model_name=metadata.model_name,
                    prompt_digest=metadata.prompt_digest,
                )
            normalization = self._controlled_normalization(
                definition,
                definition.normalize(
                    self._supplement_normalization_delta(
                        outcome.candidate_input_delta,
                        snapshot,
                    ),
                    prior_normalized_input=snapshot.prior_normalized_input,
                ),
            )
            if normalization is None:
                raise ToolInputExtractionProtocolError()
        except ToolInputExtractionTimeoutError as error:
            self._persist_bound_supplement_failure(
                actor_context,
                submission,
                snapshot=snapshot,
                current_ref=current_ref,
                running_call=running_call,
                outcome=outcome,
                metadata=metadata,
                context_snapshot=context_snapshot,
                llm_error_code=error.error_code,
                llm_safe_error_message=error.safe_error_message,
                task_error_code="UPSTREAM_TIMEOUT",
                task_safe_error_message=TIMEOUT_MESSAGE,
                provider_request_id=error.provider_request_id,
            )
            raise self._outcome_error(
                submission,
                status_code=504,
                code="UPSTREAM_TIMEOUT",
                message=TIMEOUT_MESSAGE,
            ) from None
        except ToolInputExtractionProviderError as error:
            self._persist_bound_supplement_failure(
                actor_context,
                submission,
                snapshot=snapshot,
                current_ref=current_ref,
                running_call=running_call,
                outcome=outcome,
                metadata=metadata,
                context_snapshot=context_snapshot,
                llm_error_code=error.error_code,
                llm_safe_error_message=error.safe_error_message,
                task_error_code="CHAT_ORCHESTRATION_FAILED",
                task_safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
                provider_request_id=error.provider_request_id,
            )
            raise self._outcome_error(
                submission,
                status_code=503,
                code="CHAT_ORCHESTRATION_FAILED",
                message=ORCHESTRATION_FAILURE_MESSAGE,
            ) from None
        except (
            ToolInputExtractionProtocolError,
            AttributeError,
            OverflowError,
            TypeError,
            ValueError,
            RecursionError,
        ) as error:
            llm_error_code = getattr(
                error,
                "error_code",
                "LLM_SCHEMA_MISMATCH",
            )
            llm_safe_error_message = getattr(
                error,
                "safe_error_message",
                PROTOCOL_FAILURE_MESSAGE,
            )
            self._persist_bound_supplement_failure(
                actor_context,
                submission,
                snapshot=snapshot,
                current_ref=current_ref,
                running_call=running_call,
                outcome=outcome,
                metadata=metadata,
                context_snapshot=context_snapshot,
                llm_error_code=llm_error_code,
                llm_safe_error_message=llm_safe_error_message,
                task_error_code="AGENT_INTERNAL_ERROR",
                task_safe_error_message=AGENT_INTERNAL_ERROR_MESSAGE,
                provider_request_id=getattr(
                    error,
                    "provider_request_id",
                    None,
                ),
            )
            raise AgentInternalError(
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        completed_at = _validated_utc_now(self._clock)
        projection = self._persist_bound_supplement(
            actor_context,
            submission,
            snapshot=snapshot,
            current_ref=current_ref,
            outcome=outcome,
            normalization=normalization,
            running_call=running_call,
            completed_at=completed_at,
            context_snapshot=context_snapshot,
        )
        if isinstance(normalization, InvalidNormalization):
            raise self._outcome_error(
                submission,
                status_code=422,
                code="VALIDATION_FAILED",
                message=VALIDATION_ERROR_MESSAGE,
                details=[
                    self._plain_json(error)
                    for error in normalization.validation_errors
                ],
            )
        return projection

    def _prepare_bound_supplement_call(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        snapshot: _BoundSupplementSnapshot,
        current_ref: ToolRef,
        metadata: ToolInputExtractionRequestMetadata,
        context_snapshot: Mapping[str, object],
    ) -> LLMCall:
        timestamp = _validated_utc_now(self._clock)
        call = LLMCall(
            llm_call_id=self._id_factory("llm"),
            task_id=submission.task.task_id,
            conversation_id=submission.conversation_id,
            request_id=submission.user_message.request_id,
            purpose=TOOL_INPUT_EXTRACTION,
            input_result_id=None,
            provider=metadata.provider,
            model_name=metadata.model_name,
            prompt_template_id=metadata.prompt_template_id,
            prompt_template_version=metadata.prompt_template_version,
            prompt_digest=metadata.prompt_digest,
            generation_parameters=metadata.generation_parameters,
            structured_output_summary=None,
            usage=None,
            provider_request_id=None,
            status=LLM_PENDING,
            created_at=timestamp,
            started_at=None,
            completed_at=None,
            duration_ms=None,
            error_code=None,
            safe_error_message=None,
            catalog_snapshot_refs=None,
            catalog_hash=None,
            tool_context_ref=current_ref,
            context_snapshot=context_snapshot,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                latest = max(
                    revisions,
                    key=lambda item: (item.revision, item.task_input_revision_id),
                )
                if (
                    task.bound_tool_ref != snapshot.bound_tool_ref
                    or latest.task_input_revision_id
                    != snapshot.latest_revision.task_input_revision_id
                ):
                    raise self._conflict(submission)
                existing_calls = [
                    existing
                    for existing in unit_of_work.llm_calls.list_for_task(
                        task.task_id,
                        request_id=submission.user_message.request_id,
                    )
                    if existing.purpose == TOOL_INPUT_EXTRACTION
                    and not (
                        existing.status == LLM_FAILED
                        and existing.error_code
                        == "PROCESS_INTERRUPTED_BEFORE_START"
                    )
                ]
                if len(existing_calls) > 1:
                    raise self._conflict(submission)
                if existing_calls:
                    existing = existing_calls[0]
                    if not self._supplement_call_matches_request(
                        existing,
                        submission=submission,
                        current_ref=current_ref,
                        metadata=metadata,
                        context_snapshot=context_snapshot,
                    ):
                        raise self._conflict(submission)
                    return existing
                unit_of_work.llm_calls.add(call)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_call_after_uncertain_commit(
                actor_context,
                submission,
                expected=call,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return call

    def _start_bound_supplement_call(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        call: LLMCall,
    ) -> _StartedChatCall:
        if call.status in {LLM_RUNNING, LLM_SUCCEEDED, LLM_FAILED}:
            return _StartedChatCall(call=call, invoke_adapter=False)
        running_call = replace(
            call,
            status=LLM_RUNNING,
            started_at=_validated_utc_now(self._clock),
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                persisted = unit_of_work.llm_calls.get(call.llm_call_id)
                if persisted != call:
                    if (
                        persisted is not None
                        and self._same_call_identity(persisted, call)
                        and persisted.status
                        in {LLM_RUNNING, LLM_SUCCEEDED, LLM_FAILED}
                    ):
                        return _StartedChatCall(
                            call=persisted,
                            invoke_adapter=False,
                        )
                    raise self._conflict(submission)
                if unit_of_work.llm_calls.update(
                    running_call,
                    expected_status=LLM_PENDING,
                ) is None:
                    raise self._conflict(submission)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_call_after_uncertain_commit(
                actor_context,
                submission,
                expected=running_call,
            )
            if recovered == running_call:
                return _StartedChatCall(call=recovered, invoke_adapter=True)
            if recovered is not None and recovered.status in {
                LLM_RUNNING,
                LLM_SUCCEEDED,
                LLM_FAILED,
            }:
                return _StartedChatCall(call=recovered, invoke_adapter=False)
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return _StartedChatCall(call=running_call, invoke_adapter=True)

    def _supplement_call_matches_request(
        self,
        call: LLMCall,
        *,
        submission: PreparedSubmission,
        current_ref: ToolRef,
        metadata: ToolInputExtractionRequestMetadata,
        context_snapshot: Mapping[str, object],
    ) -> bool:
        return (
            call.task_id == submission.task.task_id
            and call.conversation_id == submission.conversation_id
            and call.request_id == submission.user_message.request_id
            and call.purpose == TOOL_INPUT_EXTRACTION
            and call.provider == metadata.provider
            and call.model_name == metadata.model_name
            and call.prompt_template_id == metadata.prompt_template_id
            and call.prompt_template_version == metadata.prompt_template_version
            and dict(call.generation_parameters)
            == dict(metadata.generation_parameters)
            and dict(call.tool_context_ref or {})
            == {
                "tool_id": current_ref.tool_id,
                "version": current_ref.version,
                "schema_hash": current_ref.schema_hash,
            }
        )

    def _persist_bound_supplement_failure(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        snapshot: _BoundSupplementSnapshot,
        current_ref: ToolRef,
        running_call: LLMCall,
        outcome: ToolInputExtractionOutcome | None,
        metadata: ToolInputExtractionRequestMetadata | None,
        context_snapshot: Mapping[str, object] | None,
        llm_error_code: str,
        llm_safe_error_message: str,
        task_error_code: str,
        task_safe_error_message: str,
        provider_request_id: str | None,
    ) -> None:
        completed_at = _validated_utc_now(self._clock)
        assert running_call.started_at is not None
        call = replace(
            running_call,
            structured_output_summary=None,
            usage=None if outcome is None else outcome.usage,
            provider_request_id=provider_request_id,
            status=LLM_FAILED,
            completed_at=completed_at,
            duration_ms=_duration_ms(running_call.started_at, completed_at),
            error_code=llm_error_code,
            safe_error_message=llm_safe_error_message,
        )
        original_task: Task | None = None
        failed_task: Task | None = None
        original_record: object | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                latest = max(
                    revisions,
                    key=lambda item: (
                        item.revision,
                        item.task_input_revision_id,
                    ),
                )
                if (
                    task.bound_tool_ref != snapshot.bound_tool_ref
                    or latest.task_input_revision_id
                    != snapshot.latest_revision.task_input_revision_id
                    or latest.revision != snapshot.latest_revision.revision
                ):
                    raise self._conflict(submission)
                record = unit_of_work.idempotency_records.get_by_first_request_id(
                    submission.user_message.request_id
                )
                if (
                    record is None
                    or record.idempotency_record_id
                    != submission.idempotency_record_id
                    or record.task_input_revision_id is not None
                ):
                    raise self._conflict(submission)
                original_task = task
                original_record = record
                failed_task = replace(
                    task,
                    current_status=TASK_FAILED,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=task_error_code,
                    safe_error_message=task_safe_error_message,
                )
                if unit_of_work.llm_calls.update(
                    call,
                    expected_status=LLM_RUNNING,
                ) is None or unit_of_work.tasks.update(
                    failed_task,
                    expected_status=TASK_NEEDS_INPUT,
                ) is None:
                    raise self._conflict(submission)
                unit_of_work.commit()
        except PersistenceError as error:
            if (
                original_task is not None
                and failed_task is not None
                and original_record is not None
            ):
                recovered = self._recover_bound_supplement_commit(
                    actor_context,
                    submission,
                    original_task=original_task,
                    original_record=original_record,
                    expected_task=failed_task,
                    expected_call=call,
                    expected_revision=None,
                    expected_assistant=None,
                    expected_record=original_record,
                )
                if recovered is not None:
                    return
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None

    def _persist_bound_supplement_budget_failure(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        snapshot: _BoundSupplementSnapshot,
        current_ref: ToolRef,
        metadata: ToolInputExtractionRequestMetadata,
        context_snapshot: Mapping[str, object],
    ) -> None:
        timestamp = _validated_utc_now(self._clock)
        call = LLMCall(
            llm_call_id=self._id_factory("llm"),
            task_id=submission.task.task_id,
            conversation_id=submission.conversation_id,
            request_id=submission.user_message.request_id,
            purpose=TOOL_INPUT_EXTRACTION,
            input_result_id=None,
            provider=metadata.provider,
            model_name=metadata.model_name,
            prompt_template_id=metadata.prompt_template_id,
            prompt_template_version=metadata.prompt_template_version,
            prompt_digest=metadata.prompt_digest,
            generation_parameters=metadata.generation_parameters,
            structured_output_summary=None,
            usage=None,
            provider_request_id=None,
            status=LLM_FAILED,
            created_at=timestamp,
            started_at=timestamp,
            completed_at=timestamp,
            duration_ms=0,
            error_code="CONTEXT_BUDGET_EXCEEDED",
            safe_error_message=CONTEXT_BUDGET_MESSAGE,
            catalog_snapshot_refs=None,
            catalog_hash=None,
            tool_context_ref=current_ref,
            context_snapshot=context_snapshot,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                latest = max(
                    revisions,
                    key=lambda item: (item.revision, item.task_input_revision_id),
                )
                if (
                    task.bound_tool_ref != snapshot.bound_tool_ref
                    or latest.task_input_revision_id
                    != snapshot.latest_revision.task_input_revision_id
                ):
                    raise self._conflict(submission)
                unit_of_work.llm_calls.add(call)
                unit_of_work.commit()
        except PersistenceError as error:
            try:
                with self._unit_of_work_factory() as unit_of_work:
                    recovered = unit_of_work.llm_calls.get(call.llm_call_id)
            except PersistenceError:
                recovered = None
            if recovered == call:
                return
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None

    def _load_bound_supplement_snapshot(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> _BoundSupplementSnapshot:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                bound_ref = task.bound_tool_ref
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                if bound_ref is None or not revisions:
                    raise self._conflict(submission)
                latest = max(
                    revisions,
                    key=lambda item: (
                        item.revision,
                        item.task_input_revision_id,
                    ),
                )
                if (
                    latest.normalized_input is None
                    or latest.candidate_tool_refs
                    or not (latest.missing_fields or latest.ambiguous_fields)
                ):
                    raise self._conflict(submission)
                detached_history = tuple(
                    replace(
                        revision,
                        raw_input=self._plain_json(revision.raw_input),
                        normalized_input=(
                            None
                            if revision.normalized_input is None
                            else self._plain_json(revision.normalized_input)
                        ),
                        missing_fields=list(revision.missing_fields),
                        ambiguous_fields=[
                            self._plain_json(item)
                            for item in revision.ambiguous_fields
                        ],
                        validation_errors=list(revision.validation_errors),
                    )
                    for revision in sorted(
                        revisions,
                        key=lambda item: (
                            item.revision,
                            item.task_input_revision_id,
                        ),
                    )
                )
                detached_latest = next(
                    revision
                    for revision in detached_history
                    if revision.task_input_revision_id
                    == latest.task_input_revision_id
                )
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        assert detached_latest.normalized_input is not None
        return _BoundSupplementSnapshot(
            bound_tool_ref=bound_ref,
            latest_revision=detached_latest,
            revision_history=detached_history,
            prior_normalized_input=detached_latest.normalized_input,
        )

    @classmethod
    def _supplement_normalization_delta(
        cls,
        candidate_input_delta: Mapping[str, object],
        snapshot: _BoundSupplementSnapshot,
    ) -> dict[str, object]:
        delta = cls._plain_json(candidate_input_delta)
        assert isinstance(delta, dict)
        for ambiguity in snapshot.latest_revision.ambiguous_fields:
            if not isinstance(ambiguity, Mapping):
                continue
            field_name = ambiguity.get("field")
            candidates = ambiguity.get("candidates")
            if (
                type(field_name) is not str
                or field_name in delta
                or not isinstance(candidates, (list, tuple))
            ):
                continue
            controlled_candidates = cls._plain_json(candidates)
            exact_raw_value = next(
                (
                    revision.raw_input[field_name]
                    for revision in reversed(snapshot.revision_history)
                    if field_name in revision.raw_input
                    and cls._standard_json_equivalent(
                        cls._ambiguity_candidates(
                            revision.raw_input[field_name]
                        ),
                        controlled_candidates,
                    )
                ),
                None,
            )
            if exact_raw_value is None:
                delta[field_name] = {"candidates": controlled_candidates}
            else:
                delta[field_name] = cls._plain_json(exact_raw_value)
        return delta

    @classmethod
    def _standard_json_equivalent(
        cls,
        left: object,
        right: object,
    ) -> bool:
        if isinstance(left, Mapping) or isinstance(right, Mapping):
            if not isinstance(left, Mapping) or not isinstance(right, Mapping):
                return False
            if (
                any(type(key) is not str for key in left)
                or any(type(key) is not str for key in right)
                or set(left) != set(right)
            ):
                return False
            return all(
                cls._standard_json_equivalent(left[key], right[key])
                for key in left
            )
        if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
            if not isinstance(left, (list, tuple)) or not isinstance(
                right,
                (list, tuple),
            ):
                return False
            return len(left) == len(right) and all(
                cls._standard_json_equivalent(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        if type(left) is not type(right):
            return False
        if left is None or type(left) in (str, bool, int):
            return left == right
        if type(left) is float:
            assert type(right) is float
            return (
                math.isfinite(left)
                and math.isfinite(right)
                and left == right
            )
        return False

    def _persist_bound_supplement(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        snapshot: _BoundSupplementSnapshot,
        current_ref: ToolRef,
        outcome: ToolInputExtractionOutcome,
        normalization: ToolNormalization,
        running_call: LLMCall,
        completed_at: datetime,
        context_snapshot: Mapping[str, object] | None,
    ) -> ChatOrchestrationProjection:
        metadata = outcome.request_metadata
        assert running_call.started_at is not None
        call = replace(
            running_call,
            provider=metadata.provider,
            model_name=metadata.model_name,
            prompt_template_id=metadata.prompt_template_id,
            prompt_template_version=metadata.prompt_template_version,
            prompt_digest=metadata.prompt_digest,
            generation_parameters=metadata.generation_parameters,
            structured_output_summary={
                "candidate_input_delta": self._plain_json(
                    outcome.candidate_input_delta
                )
            },
            usage=outcome.usage,
            provider_request_id=outcome.provider_request_id,
            status=LLM_SUCCEEDED,
            completed_at=completed_at,
            duration_ms=_duration_ms(running_call.started_at, completed_at),
            error_code=None,
            safe_error_message=None,
            context_snapshot=context_snapshot,
        )
        normalized_input = self._plain_json(normalization.normalized_input)
        delta = self._plain_json(outcome.candidate_input_delta)
        assert isinstance(normalized_input, dict)
        assert isinstance(delta, dict)
        validation_errors: list[dict[str, object]] = []
        if isinstance(normalization, InvalidNormalization):
            status = TASK_NEEDS_INPUT
            missing_fields = list(snapshot.latest_revision.missing_fields)
            ambiguous_fields = [
                self._plain_json(item)
                for item in snapshot.latest_revision.ambiguous_fields
            ]
            validation_errors = [
                self._plain_json(error)
                for error in normalization.validation_errors
            ]
        elif isinstance(normalization, NeedsInputNormalization):
            status = TASK_NEEDS_INPUT
            missing_fields = list(normalization.missing_fields)
            prior_ambiguities = {
                item["field"]: list(item.get("candidates", []))
                for item in snapshot.latest_revision.ambiguous_fields
                if isinstance(item, Mapping)
                and type(item.get("field")) is str
                and isinstance(item.get("candidates"), (list, tuple))
            }
            ambiguous_fields = [
                {
                    "field": field_name,
                    "candidates": (
                        self._ambiguity_candidates(
                            outcome.candidate_input_delta.get(field_name)
                        )
                        or prior_ambiguities.get(field_name, [])
                    ),
                }
                for field_name in normalization.ambiguous_fields
            ]
        else:
            status = TASK_READY
            missing_fields = []
            ambiguous_fields = []
        assistant: Message | None = None
        if status == TASK_NEEDS_INPUT:
            assistant = Message(
                message_id=self._id_factory("msg"),
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
                actor_id=actor_context.actor_id,
                request_id=submission.user_message.request_id,
                role=ASSISTANT,
                generation_source=LLM,
                content_text=FOLLOW_UP_TEXT,
                structured_content=None,
                llm_call_id=call.llm_call_id,
                created_at=completed_at,
            )
        original_task: Task | None = None
        original_record: object | None = None
        finalized_task: Task | None = None
        revision: TaskInputRevision | None = None
        bound_record: object | None = None
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_NEEDS_INPUT,
                )
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                latest = max(
                    revisions,
                    key=lambda item: (
                        item.revision,
                        item.task_input_revision_id,
                    ),
                )
                if (
                    task.bound_tool_ref != snapshot.bound_tool_ref
                    or latest.task_input_revision_id
                    != snapshot.latest_revision.task_input_revision_id
                    or latest.revision != snapshot.latest_revision.revision
                ):
                    raise self._conflict(submission)
                original_task = task
                source_messages = [
                    message.message_id
                    for message in unit_of_work.messages.list_for_task(
                        task.task_id
                    )
                    if message.role == "USER"
                ]
                revision = TaskInputRevision(
                    task_input_revision_id=self._id_factory("revision"),
                    task_id=task.task_id,
                    request_id=submission.user_message.request_id,
                    source_llm_call_id=call.llm_call_id,
                    source_message_ids=source_messages,
                    revision=latest.revision + 1,
                    raw_input=delta,
                    normalized_input=normalized_input,
                    missing_fields=missing_fields,
                    ambiguous_fields=ambiguous_fields,
                    validation_errors=validation_errors,
                    created_at=completed_at,
                    candidate_tool_refs=(),
                )
                finalized_task = replace(
                    task,
                    current_status=status,
                    updated_at=completed_at,
                    completed_at=None,
                    error_code=None,
                    safe_error_message=None,
                )
                if unit_of_work.llm_calls.update(
                    call,
                    expected_status=LLM_RUNNING,
                ) is None:
                    raise self._conflict(submission)
                unit_of_work.task_input_revisions.add(revision)
                if assistant is not None:
                    unit_of_work.messages.add(assistant)
                record = unit_of_work.idempotency_records.get_by_first_request_id(
                    submission.user_message.request_id
                )
                original_record = record
                if (
                    record is None
                    or record.idempotency_record_id
                    != submission.idempotency_record_id
                ):
                    raise self._conflict(submission)
                bound_record = (
                    unit_of_work.idempotency_records.bind_task_input_revision(
                        record,
                        revision.task_input_revision_id,
                    )
                )
                if (
                    bound_record is None
                    or unit_of_work.tasks.update(
                        finalized_task,
                        expected_status=TASK_NEEDS_INPUT,
                    )
                    is None
                ):
                    raise self._conflict(submission)
                unit_of_work.commit()
        except PersistenceError as error:
            if (
                original_task is not None
                and original_record is not None
                and finalized_task is not None
                and revision is not None
                and bound_record is not None
            ):
                recovered = self._recover_bound_supplement_commit(
                    actor_context,
                    submission,
                    original_task=original_task,
                    original_record=original_record,
                    expected_task=finalized_task,
                    expected_call=call,
                    expected_revision=revision,
                    expected_assistant=assistant,
                    expected_record=bound_record,
                )
                if recovered is not None:
                    return recovered
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return ChatOrchestrationProjection(
            conversation_id=submission.conversation_id,
            user_message=submission.user_message,
            task=finalized_task,
            llm_call=call,
            assistant_message=assistant,
            revision=revision,
        )

    def _recover_bound_supplement_commit(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        original_task: Task,
        original_record: object,
        expected_task: Task,
        expected_call: LLMCall,
        expected_revision: TaskInputRevision | None,
        expected_assistant: Message | None,
        expected_record: object,
    ) -> ChatOrchestrationProjection | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                conversation = unit_of_work.conversations.get_owned(
                    submission.conversation_id,
                    actor_context.actor_id,
                )
                user_message = unit_of_work.messages.get(
                    submission.user_message.message_id
                )
                task = unit_of_work.tasks.get_owned_for_update(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
                if conversation is None or user_message is None or task is None:
                    raise self._conflict(submission)
                self._require_source_identity(
                    actor_context,
                    submission,
                    user_message,
                    task,
                )
                calls = [
                    call
                    for call in unit_of_work.llm_calls.list_for_task(
                        task.task_id,
                        request_id=user_message.request_id,
                    )
                    if not (
                        call.status == LLM_FAILED
                        and call.error_code
                        == "PROCESS_INTERRUPTED_BEFORE_START"
                    )
                ]
                revisions = [
                    item
                    for item in unit_of_work.task_input_revisions.list_for_task(
                        task.task_id
                    )
                    if item.request_id == user_message.request_id
                ]
                assistants = [
                    item
                    for item in unit_of_work.messages.list_for_task(task.task_id)
                    if item.role == ASSISTANT
                    and item.request_id == user_message.request_id
                ]
                record = unit_of_work.idempotency_records.get_by_first_request_id(
                    user_message.request_id
                )
        except PersistenceError:
            return None

        expected_revisions = (
            [] if expected_revision is None else [expected_revision]
        )
        expected_assistants = (
            [] if expected_assistant is None else [expected_assistant]
        )
        if (
            task == expected_task
            and calls == [expected_call]
            and revisions == expected_revisions
            and assistants == expected_assistants
            and record == expected_record
        ):
            return ChatOrchestrationProjection(
                conversation_id=conversation.conversation_id,
                user_message=user_message,
                task=task,
                llm_call=calls[0],
                assistant_message=assistants[0] if assistants else None,
                revision=revisions[0] if revisions else None,
            )
        if (
            task == original_task
            and calls == []
            and revisions == []
            and assistants == []
            and record == original_record
        ):
            return None
        raise self._conflict(submission)

    def load_current_submission(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> ChatOrchestrationProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                conversation = unit_of_work.conversations.get_owned(
                    submission.conversation_id,
                    actor_context.actor_id,
                )
                message = unit_of_work.messages.get(
                    submission.user_message.message_id
                )
                task = unit_of_work.tasks.get_owned_for_update(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
                if conversation is None or message is None or task is None:
                    raise ResourceNotFoundError()
                self._require_source_identity(
                    actor_context,
                    submission,
                    message,
                    task,
                )
                expected_purpose = (
                    CHAT_ORCHESTRATION
                    if submission.submission_mode == NEW_TASK
                    else TOOL_INPUT_EXTRACTION
                )
                calls = [
                    call
                    for call in unit_of_work.llm_calls.list_for_task(
                        task.task_id,
                        request_id=message.request_id,
                    )
                    if call.purpose == expected_purpose
                ]
                current_calls = [
                    call
                    for call in calls
                    if not (
                        call.status == LLM_FAILED
                        and call.error_code
                        == "PROCESS_INTERRUPTED_BEFORE_START"
                    )
                ]
                if len(current_calls) > 1:
                    raise self._conflict(submission)
                call = current_calls[0] if current_calls else None
                assistant = (
                    None
                    if call is None
                    else unit_of_work.messages.get_by_llm_call_id(
                        call.llm_call_id
                    )
                )
                revisions = (
                    []
                    if call is None
                    else unit_of_work.task_input_revisions.list_for_llm_call_id(
                        call.llm_call_id
                    )
                )
                if len(revisions) > 1:
                    raise self._conflict(submission)
                if call is None:
                    expected_without_call = (
                        TASK_PENDING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    )
                    if (
                        task.current_status != expected_without_call
                        or assistant is not None
                        or revisions
                    ):
                        raise self._conflict(submission)
                elif call.status == LLM_PENDING:
                    expected_pending = (
                        TASK_PENDING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    )
                    if (
                        task.current_status != expected_pending
                        or assistant is not None
                        or revisions
                    ):
                        raise self._conflict(submission)
                elif call.status == LLM_RUNNING:
                    expected_running = (
                        TASK_RUNNING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    )
                    if (
                        task.current_status != expected_running
                        or assistant is not None
                        or revisions
                    ):
                        raise self._conflict(submission)
                else:
                    all_revisions = (
                        unit_of_work.task_input_revisions.list_for_task(
                            task.task_id
                        )
                    )
                    task_messages = unit_of_work.messages.list_for_task(
                        task.task_id
                    )
                    self._require_projection_identity(
                        actor_context,
                        submission,
                        call,
                        assistant,
                        revisions,
                        all_revisions,
                        task_messages,
                    )
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return ChatOrchestrationProjection(
            conversation_id=conversation.conversation_id,
            user_message=message,
            task=task,
            llm_call=call,
            assistant_message=assistant,
            revision=revisions[0] if revisions else None,
        )

    def finalize_result(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        llm_call_id: str,
        result: KnowledgeAnswer | _ResolvedToolRoute,
        usage: Mapping[str, int] | None = None,
        provider_request_id: str | None = None,
    ) -> ChatOrchestrationProjection:
        if not isinstance(
            result,
            (KnowledgeAnswer, _ResolvedToolRoute),
        ):
            raise TypeError("result must be a chat orchestration result.")
        completed_at = _validated_utc_now(self._clock)
        already_finalized = False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=None,
                )
                running_call = unit_of_work.llm_calls.get(llm_call_id)
                if running_call is None or (
                    running_call.task_id != task.task_id
                    or running_call.conversation_id != submission.conversation_id
                    or running_call.request_id != submission.user_message.request_id
                    or running_call.purpose != CHAT_ORCHESTRATION
                ):
                    raise self._conflict(submission)
                if running_call.status in {LLM_SUCCEEDED, LLM_FAILED}:
                    if task.current_status in {
                        TASK_NEEDS_INPUT,
                        TASK_READY,
                        TASK_SUCCEEDED,
                        TASK_FAILED,
                    } or (
                        self._tool_chain_enabled
                        and task.current_status == TASK_RUNNING
                    ):
                        already_finalized = True
                    else:
                        raise self._conflict(submission)
                elif (
                    running_call.status != LLM_RUNNING
                    or task.current_status != TASK_RUNNING
                ):
                    raise self._conflict(submission)
                else:
                    self._write_success_outcome(
                        unit_of_work,
                        actor_context,
                        submission,
                        task,
                        running_call,
                        result,
                        completed_at,
                        usage,
                        provider_request_id,
                    )
                    unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_successful_finalize(
                actor_context,
                submission,
                llm_call_id=llm_call_id,
                result=result,
                usage=usage,
                provider_request_id=provider_request_id,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        if already_finalized:
            return self._load_projection(
                actor_context,
                submission,
                llm_call_id,
            )
        return self._load_projection(
            actor_context,
            submission,
            llm_call_id,
        )

    def _recover_successful_finalize(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        llm_call_id: str,
        result: KnowledgeAnswer | _ResolvedToolRoute,
        usage: Mapping[str, int] | None,
        provider_request_id: str | None,
    ) -> ChatOrchestrationProjection | None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                call = unit_of_work.llm_calls.get(llm_call_id)
                task = unit_of_work.tasks.get_owned(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
        except PersistenceError:
            return None
        if call is None or task is None:
            return None
        if call.status in {LLM_PENDING, LLM_RUNNING}:
            return None
        projection = self._load_projection(
            actor_context,
            submission,
            llm_call_id,
        )
        if not self._is_equivalent_success_projection(
            projection,
            result=result,
            usage=usage,
            provider_request_id=provider_request_id,
        ):
            raise self._conflict(submission)
        return projection

    def _is_equivalent_success_projection(
        self,
        projection: ChatOrchestrationProjection,
        *,
        result: KnowledgeAnswer | _ResolvedToolRoute,
        usage: Mapping[str, int] | None = None,
        provider_request_id: str | None = None,
    ) -> bool:
        call = projection.llm_call
        if call is None or call.status != LLM_SUCCEEDED:
            return False
        if (
            call.usage != usage
            or call.provider_request_id != provider_request_id
        ):
            return False
        if isinstance(result, KnowledgeAnswer):
            answer_text = result.answer_text.strip()
            return (
                call.structured_output_summary
                == {
                    "route": "KNOWLEDGE_ANSWER",
                    "answer_length": len(answer_text),
                    "answer_digest": sha256(
                        answer_text.encode("utf-8")
                    ).hexdigest(),
                }
                and projection.assistant_message is not None
                and projection.assistant_message.content_text == answer_text
                and projection.assistant_message.structured_content is None
                and projection.task.bound_tool_ref is None
                and projection.revision is None
            )
        if not isinstance(result, _ResolvedToolRoute) or projection.revision is None:
            return False
        expected_summary = self._candidate_summary(result.proposal_set)
        revision = projection.revision
        if self._plain_json(call.structured_output_summary) != expected_summary:
            return False
        candidate_refs = tuple(
            {
                "tool_id": ref.tool_id,
                "version": ref.version,
                "schema_hash": ref.schema_hash,
            }
            for ref in result.candidate_refs
        )
        persisted_candidate_refs = tuple(
            self._plain_json(ref) for ref in revision.candidate_tool_refs
        )
        if result.selected_ref is None:
            return (
                result.selected_input is None
                and result.normalization is None
                and len(candidate_refs) >= 2
                and projection.task.bound_tool_ref is None
                and projection.task.current_status == TASK_NEEDS_INPUT
                and self._plain_json(revision.raw_input)
                == {"candidates": expected_summary["candidates"]}
                and revision.normalized_input is None
                and revision.missing_fields == []
                and revision.ambiguous_fields == []
                and persisted_candidate_refs == candidate_refs
                and projection.assistant_message is not None
                and projection.assistant_message.content_text == FOLLOW_UP_TEXT
                and projection.assistant_message.structured_content is None
            )
        if (
            result.selected_input is None
            or result.normalization is None
            or result.candidate_refs != (result.selected_ref,)
            or projection.task.bound_tool_ref != result.selected_ref
            or persisted_candidate_refs
            or self._plain_json(revision.raw_input)
            != self._plain_json(result.selected_input)
            or self._plain_json(revision.normalized_input)
            != self._plain_json(result.normalization.normalized_input)
        ):
            return False
        if isinstance(result.normalization, InvalidNormalization):
            return (
                projection.task.current_status == TASK_FAILED
                and projection.task.error_code == "VALIDATION_FAILED"
                and self._plain_json(revision.validation_errors)
                == self._plain_json(result.normalization.validation_errors)
                and revision.missing_fields == []
                and revision.ambiguous_fields == []
                and projection.assistant_message is None
            )
        if revision.validation_errors != []:
            return False
        if isinstance(result.normalization, ReadyNormalization):
            try:
                definition = self._tool_registry.resolve(
                    result.selected_ref.tool_id,
                )
            except UnknownToolError:
                return False
            if getattr(definition, "ref", None) != result.selected_ref:
                return False
            supported_outputs = getattr(definition, "supported_outputs", None)
            if not isinstance(supported_outputs, tuple) or not supported_outputs:
                return False
            normalized_input = revision.normalized_input
            if normalized_input is None:
                return False
            normalized_json = self._plain_json(normalized_input)
            if not isinstance(normalized_json, Mapping):
                return False
            input_schema = getattr(definition, "input_schema", None)
            schema_properties = (
                input_schema.get("properties")
                if isinstance(input_schema, Mapping)
                else None
            )
            schema_declares_outputs = (
                isinstance(schema_properties, Mapping)
                and "requested_outputs" in schema_properties
            )
            if "requested_outputs" not in normalized_json:
                outputs_match = (
                    not schema_declares_outputs
                    and result.normalization.requested_outputs == supported_outputs
                )
            else:
                outputs_match = (
                    self._plain_json(result.normalization.requested_outputs)
                    == normalized_json["requested_outputs"]
                )
            return (
                projection.task.current_status == TASK_READY
                and outputs_match
                and revision.missing_fields == []
                and revision.ambiguous_fields == []
                and projection.assistant_message is None
            )
        expected_ambiguous = [
            {
                "field": field_name,
                "candidates": self._ambiguity_candidates(
                    result.selected_input.get(field_name)
                ),
            }
            for field_name in result.normalization.ambiguous_fields
        ]
        return (
            projection.task.current_status == TASK_NEEDS_INPUT
            and revision.missing_fields
            == list(result.normalization.missing_fields)
            and revision.ambiguous_fields == expected_ambiguous
            and projection.assistant_message is not None
            and projection.assistant_message.content_text == FOLLOW_UP_TEXT
            and projection.assistant_message.structured_content is None
        )

    def _resolve_tool_candidates(
        self,
        result: object,
        catalog: RoutingCatalogSnapshot,
        *,
        context_result: ContextBuildResult,
        actor_context: ActorContext,
        current_text: str,
        conversation_id: str,
    ) -> _ResolvedToolRoute:
        if not isinstance(result, ToolCandidateSet):
            raise ChatOrchestrationProtocolError(
                "Router returned a non-generic Tool result."
            )
        valid: list[
            tuple[object, ToolCandidateProposal, Mapping[str, object]]
        ] = []
        for proposal in result.candidates:
            try:
                definition = self._tool_registry.resolve(
                    proposal.tool_id,
                    snapshot=catalog,
                )
            except UnknownToolError:
                raise ChatOrchestrationProtocolError(
                    "Router returned a Tool candidate outside its routing snapshot.",
                    error_code="LLM_SCHEMA_MISMATCH",
                ) from None
            candidate_input = self._candidate_input_with_history(
                proposal,
                context_result=context_result,
                actor_context=actor_context,
                current_text=current_text,
                conversation_id=conversation_id,
            )
            valid.append((definition, proposal, candidate_input))
        if not valid:
            raise ChatOrchestrationProtocolError(
                "Router returned no resolvable Tool candidate.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        refs = tuple(definition.ref for definition, _, _ in valid)
        if len(valid) > 1:
            return _ResolvedToolRoute(result, refs, None, None, None)
        definition, proposal, candidate_input = valid[0]
        try:
            normalization = definition.normalize(
                candidate_input,
                prior_normalized_input=None,
            )
        except Exception:
            raise ChatOrchestrationProtocolError(
                "Tool candidate normalization failed.",
                error_code="LLM_SCHEMA_MISMATCH",
            ) from None
        if not isinstance(
            normalization,
            (ReadyNormalization, NeedsInputNormalization, InvalidNormalization),
        ):
            raise ChatOrchestrationProtocolError(
                "Tool normalizer returned an invalid result.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        controlled_normalization = self._controlled_normalization(
            definition,
            normalization,
        )
        if controlled_normalization is None:
            raise ChatOrchestrationProtocolError(
                "Tool normalizer returned an inconsistent result.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        try:
            authorization = self._tool_registry.authorize(
                registration=definition,
                action=ToolAction.NEW_BINDING,
                bound_ref=None,
            )
        except ToolAuthorizationError:
            raise ChatOrchestrationProtocolError(
                "Tool candidate is not authorized for a new binding.",
                error_code="LLM_SCHEMA_MISMATCH",
            ) from None
        return _ResolvedToolRoute(
            result,
            refs,
            authorization.authorized_ref,
            candidate_input,
            controlled_normalization,
        )

    def _candidate_input_with_history(
        self,
        proposal: ToolCandidateProposal,
        *,
        context_result: ContextBuildResult,
        actor_context: ActorContext,
        current_text: str,
        conversation_id: str,
    ) -> Mapping[str, object]:
        delta = self._plain_json(proposal.candidate_input_delta)
        if not isinstance(delta, dict):
            raise ChatOrchestrationProtocolError(
                "Tool candidate delta is invalid.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        history_reference = proposal.history_reference
        if history_reference is None:
            return delta
        if not isinstance(history_reference, HistoryReference):
            raise ChatOrchestrationProtocolError(
                "Tool history reference is invalid.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        normalized_reference = self._normalized_reference_text(
            history_reference.reference_text
        )
        normalized_current = self._normalized_reference_text(current_text)
        if (
            not normalized_reference
            or normalized_reference not in normalized_current
        ):
            raise ChatOrchestrationProtocolError(
                "Tool history reference text is not present in the current message.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        resolution = context_result.reference_resolutions.get(
            history_reference.context_ref
        )
        if (
            resolution is None
            or resolution.conversation_id != conversation_id
            or resolution.tool_ref.tool_id != proposal.tool_id
        ):
            raise ChatOrchestrationProtocolError(
                "Tool history reference is outside the current Context Window.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    resolution.task_id,
                    actor_context.actor_id,
                )
                revision = unit_of_work.task_input_revisions.get(
                    resolution.task_input_revision_id
                )
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=conversation_id,
            ) from None
        if (
            task is None
            or revision is None
            or task.conversation_id != conversation_id
            or revision.task_id != task.task_id
            or task.bound_tool_ref != resolution.tool_ref
            or revision.normalized_input is None
            or not self._standard_json_equivalent(
                revision.normalized_input,
                resolution.normalized_input,
            )
        ):
            raise ChatOrchestrationProtocolError(
                "Tool history reference no longer resolves to the selected facts.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        base = self._plain_json(revision.normalized_input)
        if not isinstance(base, dict):
            raise ChatOrchestrationProtocolError(
                "Tool history reference input is invalid.",
                error_code="LLM_SCHEMA_MISMATCH",
            )
        base.update(delta)
        return base

    @staticmethod
    def _normalized_reference_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _controlled_normalization(
        definition: object,
        normalization: ToolNormalization,
    ) -> ToolNormalization | None:
        try:
            if isinstance(normalization, ReadyNormalization):
                controlled: ToolNormalization = ReadyNormalization(
                    normalized_input=normalization.normalized_input,
                    requested_outputs=normalization.requested_outputs,
                )
            elif isinstance(normalization, NeedsInputNormalization):
                controlled = NeedsInputNormalization(
                    normalized_input=normalization.normalized_input,
                    missing_fields=normalization.missing_fields,
                    ambiguous_fields=normalization.ambiguous_fields,
                    follow_up_suggestion=normalization.follow_up_suggestion,
                )
            else:
                controlled = InvalidNormalization(
                    normalized_input=normalization.normalized_input,
                    validation_errors=normalization.validation_errors,
                )
        except (
            AttributeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None
        supported_outputs = getattr(definition, "supported_outputs", None)
        if not isinstance(supported_outputs, tuple) or not supported_outputs:
            return None
        normalized = controlled.normalized_input
        if not isinstance(normalized, Mapping) or not normalized:
            return None
        if isinstance(controlled, InvalidNormalization):
            return controlled
        has_normalized_outputs = "requested_outputs" in normalized
        normalized_outputs = normalized.get("requested_outputs")
        input_schema = getattr(definition, "input_schema", None)
        schema_properties = (
            input_schema.get("properties")
            if isinstance(input_schema, Mapping)
            else None
        )
        schema_declares_outputs = (
            isinstance(schema_properties, Mapping)
            and "requested_outputs" in schema_properties
        )
        if isinstance(controlled, ReadyNormalization):
            if (
                not controlled.requested_outputs
                or not set(controlled.requested_outputs) <= set(supported_outputs)
            ):
                return None
            if not has_normalized_outputs:
                return (
                    controlled
                    if not schema_declares_outputs
                    and controlled.requested_outputs == supported_outputs
                    else None
                )
            if (
                not isinstance(normalized_outputs, tuple)
                or controlled.requested_outputs != normalized_outputs
            ):
                return None
            return controlled
        unresolved = (
            *controlled.missing_fields,
            *controlled.ambiguous_fields,
        )
        if any(
            field_name not in normalized or normalized[field_name] is not None
            for field_name in unresolved
        ):
            return None
        if "requested_outputs" in unresolved:
            return controlled
        if not has_normalized_outputs:
            return None if schema_declares_outputs else controlled
        if (
            not isinstance(normalized_outputs, tuple)
            or not normalized_outputs
            or not set(normalized_outputs) <= set(supported_outputs)
        ):
            return None
        return controlled

    @staticmethod
    def _plain_json(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                key: ChatOrchestrationService._plain_json(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [ChatOrchestrationService._plain_json(item) for item in value]
        return value

    @classmethod
    def _catalog_hash(cls, catalog: RoutingCatalogSnapshot) -> str:
        payload = [
            {
                "tool_id": entry.tool_id,
                "version": entry.version,
                "schema_hash": entry.schema_hash,
                "display_name": entry.display_name,
                "description": entry.description,
                "candidate_input_schema": cls._plain_json(
                    entry.candidate_input_schema
                ),
                "supported_outputs": list(entry.supported_outputs),
            }
            for entry in catalog.entries
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @classmethod
    def _candidate_summary(cls, result: ToolCandidateSet) -> dict[str, object]:
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [
                {
                    "tool_id": candidate.tool_id,
                    "candidate_input_delta": cls._plain_json(
                        candidate.candidate_input_delta
                    ),
                    **(
                        {}
                        if candidate.history_reference is None
                        else {
                            "history_reference": {
                                "context_ref": candidate.history_reference.context_ref,
                                "reference_text": candidate.history_reference.reference_text,
                            }
                        }
                    ),
                }
                for candidate in result.candidates
            ],
        }

    @classmethod
    def _ambiguity_candidates(cls, value: object) -> list[object]:
        if isinstance(value, Mapping):
            candidates = value.get("candidates")
            if isinstance(candidates, (list, tuple)):
                return [cls._plain_json(item) for item in candidates]
            for nested in value.values():
                found = cls._ambiguity_candidates(nested)
                if found:
                    return found
        return []

    def _prepare_call(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        metadata: ChatOrchestrationRequestMetadata,
        orchestration_input: ChatOrchestrationInput,
        *,
        context_snapshot: Mapping[str, object],
    ) -> LLMCall:
        timestamp = _validated_utc_now(self._clock)
        call = LLMCall(
            llm_call_id=self._id_factory("llm"),
            task_id=submission.task.task_id,
            conversation_id=submission.conversation_id,
            request_id=submission.user_message.request_id,
            purpose=CHAT_ORCHESTRATION,
            input_result_id=None,
            provider=metadata.provider,
            model_name=metadata.model_name,
            prompt_template_id=metadata.prompt_template_id,
            prompt_template_version=metadata.prompt_template_version,
            prompt_digest=metadata.prompt_digest,
            generation_parameters=metadata.generation_parameters,
            structured_output_summary=None,
            usage=None,
            provider_request_id=None,
            status=LLM_PENDING,
            created_at=timestamp,
            started_at=None,
            completed_at=None,
            duration_ms=None,
            error_code=None,
            safe_error_message=None,
            catalog_snapshot_refs=tuple(
                entry.ref for entry in orchestration_input.routing_catalog.entries
            ),
            catalog_hash=self._catalog_hash(orchestration_input.routing_catalog),
            context_snapshot=context_snapshot,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=None,
                )
                existing_calls = [
                    existing
                    for existing in unit_of_work.llm_calls.list_for_task(
                        submission.task.task_id,
                        request_id=submission.user_message.request_id,
                    )
                    if existing.purpose == CHAT_ORCHESTRATION
                    and not (
                        existing.status == LLM_FAILED
                        and existing.error_code
                        == "PROCESS_INTERRUPTED_BEFORE_START"
                    )
                ]
                if len(existing_calls) > 1:
                    raise self._conflict(submission)
                if existing_calls:
                    existing = existing_calls[0]
                    if not self._call_matches_request(
                        existing,
                        submission=submission,
                        metadata=metadata,
                        orchestration_input=orchestration_input,
                        context_snapshot=context_snapshot,
                    ):
                        raise self._conflict(submission)
                    return existing
                expected_task_status = (
                    TASK_PENDING
                    if submission.submission_mode == NEW_TASK
                    else TASK_NEEDS_INPUT
                )
                if task.current_status != expected_task_status:
                    raise self._conflict(submission)
                unit_of_work.llm_calls.add(call)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_call_after_uncertain_commit(
                actor_context,
                submission,
                expected=call,
            )
            if recovered is not None:
                return recovered
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return call

    def _start_call(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        call: LLMCall,
    ) -> _StartedChatCall:
        if call.status in {
            LLM_RUNNING,
            LLM_SUCCEEDED,
            LLM_FAILED,
        }:
            return _StartedChatCall(call=call, invoke_adapter=False)
        timestamp = _validated_utc_now(self._clock)
        running_call = replace(
            call,
            status=LLM_RUNNING,
            started_at=timestamp,
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=None,
                )
                persisted_call = unit_of_work.llm_calls.get(call.llm_call_id)
                if persisted_call != call:
                    if (
                        persisted_call is not None
                        and persisted_call.task_id == call.task_id
                        and persisted_call.conversation_id == call.conversation_id
                        and persisted_call.request_id == call.request_id
                        and persisted_call.purpose == call.purpose
                        and persisted_call.status
                        in {LLM_RUNNING, LLM_SUCCEEDED, LLM_FAILED}
                    ):
                        return _StartedChatCall(
                            call=persisted_call,
                            invoke_adapter=False,
                        )
                    raise self._conflict(submission)
                expected_task_status = (
                    TASK_PENDING
                    if submission.submission_mode == NEW_TASK
                    else TASK_NEEDS_INPUT
                )
                if task.current_status != expected_task_status:
                    raise self._conflict(submission)
                running_task = replace(
                    task,
                    current_status=TASK_RUNNING,
                    started_at=task.started_at or timestamp,
                    updated_at=timestamp,
                    completed_at=None,
                    error_code=None,
                    safe_error_message=None,
                )
                if unit_of_work.llm_calls.update(
                    running_call,
                    expected_status=LLM_PENDING,
                ) is None or unit_of_work.tasks.update(
                    running_task,
                    expected_status=expected_task_status,
                ) is None:
                    raise self._conflict(submission)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_call_after_uncertain_commit(
                actor_context,
                submission,
                expected=running_call,
            )
            if recovered == running_call:
                return _StartedChatCall(
                    call=recovered,
                    invoke_adapter=True,
                )
            if recovered is not None and recovered.status in {
                LLM_RUNNING,
                LLM_SUCCEEDED,
                LLM_FAILED,
            }:
                return _StartedChatCall(
                    call=recovered,
                    invoke_adapter=False,
                )
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return _StartedChatCall(
            call=running_call,
            invoke_adapter=True,
        )

    def _call_matches_request(
        self,
        call: LLMCall,
        *,
        submission: PreparedSubmission,
        metadata: ChatOrchestrationRequestMetadata,
        orchestration_input: ChatOrchestrationInput,
        context_snapshot: Mapping[str, object],
    ) -> bool:
        return (
            call.task_id == submission.task.task_id
            and call.conversation_id == submission.conversation_id
            and call.request_id == submission.user_message.request_id
            and call.purpose == CHAT_ORCHESTRATION
            and call.provider == metadata.provider
            and call.model_name == metadata.model_name
            and call.prompt_template_id == metadata.prompt_template_id
            and call.prompt_template_version == metadata.prompt_template_version
            and call.prompt_digest == metadata.prompt_digest
            and dict(call.generation_parameters)
            == dict(metadata.generation_parameters)
            and call.catalog_hash
            == self._catalog_hash(orchestration_input.routing_catalog)
            and self._plain_json(call.context_snapshot)
            == self._plain_json(context_snapshot)
        )

    def _recover_call_after_uncertain_commit(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        expected: LLMCall,
    ) -> LLMCall | None:
        try:
            projection = self.load_current_submission(
                actor_context,
                submission,
            )
        except PersistenceError:
            return None
        current = projection.llm_call
        if current is None:
            return None
        if not self._same_call_identity(current, expected):
            raise self._conflict(submission)
        if current.status in {
            LLM_PENDING,
            LLM_RUNNING,
            LLM_SUCCEEDED,
            LLM_FAILED,
        }:
            return current
        raise self._conflict(submission)

    @staticmethod
    def _same_call_identity(left: LLMCall, right: LLMCall) -> bool:
        return all(
            getattr(left, field_name) == getattr(right, field_name)
            for field_name in (
                "llm_call_id",
                "task_id",
                "conversation_id",
                "request_id",
                "purpose",
                "input_result_id",
                "provider",
                "model_name",
                "prompt_template_id",
                "prompt_template_version",
                "prompt_digest",
                "generation_parameters",
                "catalog_snapshot_refs",
                "catalog_hash",
                "tool_context_ref",
                "context_snapshot",
                "created_at",
            )
        )

    def _write_success_outcome(
        self,
        unit_of_work: object,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        task: Task,
        running_call: LLMCall,
        result: KnowledgeAnswer | _ResolvedToolRoute,
        completed_at: datetime,
        usage: Mapping[str, int] | None,
        provider_request_id: str | None,
    ) -> None:
        duration_ms = _duration_ms(running_call.started_at, completed_at)
        assistant_message: Message | None = None
        revision: TaskInputRevision | None = None
        if isinstance(result, KnowledgeAnswer):
            if submission.submission_mode != NEW_TASK:
                raise self._conflict(submission)
            answer_text = result.answer_text.strip()
            summary = {
                "route": "KNOWLEDGE_ANSWER",
                "answer_length": len(answer_text),
                "answer_digest": sha256(
                    answer_text.encode("utf-8")
                ).hexdigest(),
            }
            assistant_message = Message(
                message_id=self._id_factory("msg"),
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
                actor_id=actor_context.actor_id,
                request_id=submission.user_message.request_id,
                role=ASSISTANT,
                generation_source=LLM,
                content_text=answer_text,
                structured_content=None,
                llm_call_id=running_call.llm_call_id,
                created_at=completed_at,
            )
            finalized_task = replace(
                task,
                task_type=KNOWLEDGE_QA,
                current_status=TASK_SUCCEEDED,
                selected_tool_run_id=None,
                selected_result_id=None,
                updated_at=completed_at,
                completed_at=completed_at,
                error_code=None,
                safe_error_message=None,
            )
        else:
            if submission.submission_mode != NEW_TASK:
                raise self._conflict(submission)
            if task.bound_tool_ref != submission.task.bound_tool_ref:
                raise self._conflict(submission)
            summary = self._candidate_summary(result.proposal_set)
            candidate_refs = result.candidate_refs if result.selected_ref is None else ()
            validation_errors: list[dict[str, object]] = []
            if result.selected_ref is None:
                raw_input = {
                    "candidates": summary["candidates"],
                }
                normalized_input = None
                missing_fields: list[str] = []
                ambiguous_fields: list[dict[str, object]] = []
                status = TASK_NEEDS_INPUT
            else:
                if result.selected_input is None or result.normalization is None:
                    raise self._conflict(submission)
                raw_input = self._plain_json(result.selected_input)
                normalized_input = self._plain_json(
                    result.normalization.normalized_input
                )
                missing_fields = []
                ambiguous_fields = []
                if isinstance(result.normalization, InvalidNormalization):
                    validation_errors = [
                        self._plain_json(error)
                        for error in result.normalization.validation_errors
                    ]
                    status = TASK_FAILED
                elif isinstance(result.normalization, NeedsInputNormalization):
                    missing_fields = list(result.normalization.missing_fields)
                    ambiguous_fields = [
                        {
                            "field": field_name,
                            "candidates": self._ambiguity_candidates(
                                result.selected_input.get(field_name)
                            ),
                        }
                        for field_name in result.normalization.ambiguous_fields
                    ]
                    status = TASK_NEEDS_INPUT
                else:
                    status = TASK_READY
            if status == TASK_NEEDS_INPUT:
                assistant_message = Message(
                    message_id=self._id_factory("msg"),
                    conversation_id=submission.conversation_id,
                    task_id=submission.task.task_id,
                    actor_id=actor_context.actor_id,
                    request_id=submission.user_message.request_id,
                    role=ASSISTANT,
                    generation_source=LLM,
                    content_text=FOLLOW_UP_TEXT,
                    structured_content=None,
                    llm_call_id=running_call.llm_call_id,
                    created_at=completed_at,
                )
            bindable_task = replace(task)
            if result.selected_ref is not None:
                bindable_task.bind_tool(result.selected_ref)
            finalized_task = replace(
                bindable_task,
                task_type=TOOL_EXECUTION,
                current_status=status,
                selected_tool_run_id=None,
                selected_result_id=None,
                updated_at=completed_at,
                completed_at=(completed_at if status == TASK_FAILED else None),
                error_code=(
                    "VALIDATION_FAILED" if status == TASK_FAILED else None
                ),
                safe_error_message=(
                    VALIDATION_ERROR_MESSAGE if status == TASK_FAILED else None
                ),
            )
            prior_revisions = unit_of_work.task_input_revisions.list_for_task(
                task.task_id
            )
            source_messages = [
                message.message_id
                for message in unit_of_work.messages.list_for_task(task.task_id)
                if message.role == "USER"
            ]
            revision = TaskInputRevision(
                task_input_revision_id=self._id_factory("revision"),
                task_id=task.task_id,
                request_id=submission.user_message.request_id,
                source_llm_call_id=running_call.llm_call_id,
                source_message_ids=source_messages,
                revision=(
                    1
                    if submission.submission_mode == NEW_TASK
                    else max(item.revision for item in prior_revisions) + 1
                ),
                raw_input=raw_input,  # type: ignore[arg-type]
                normalized_input=normalized_input,  # type: ignore[arg-type]
                missing_fields=missing_fields,
                ambiguous_fields=ambiguous_fields,
                validation_errors=validation_errors,
                created_at=completed_at,
                candidate_tool_refs=candidate_refs,
            )
        succeeded_call = replace(
            running_call,
            status=LLM_SUCCEEDED,
            structured_output_summary=summary,
            usage=usage,
            provider_request_id=provider_request_id,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )
        if revision is not None:
            unit_of_work.task_input_revisions.add(revision)
            if submission.submission_mode == SUPPLEMENT_TASK:
                record = (
                    unit_of_work.idempotency_records.get_by_first_request_id(
                        submission.user_message.request_id
                    )
                )
                if (
                    record is None
                    or record.idempotency_record_id
                    != submission.idempotency_record_id
                    or unit_of_work.idempotency_records.bind_task_input_revision(
                        record,
                        revision.task_input_revision_id,
                    )
                    is None
                ):
                    raise self._conflict(submission)
        if assistant_message is not None:
            unit_of_work.messages.add(assistant_message)
        if unit_of_work.llm_calls.update(
            succeeded_call,
            expected_status=LLM_RUNNING,
        ) is None or unit_of_work.tasks.update(
            finalized_task,
            expected_status=TASK_RUNNING,
        ) is None:
            raise self._conflict(submission)

    def _finalize_failure(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        llm_call_id: str,
        *,
        llm_error_code: str,
        llm_safe_error_message: str,
        task_error_code: str,
        task_safe_error_message: str,
        provider_request_id: str | None,
    ) -> None:
        completed_at = _validated_utc_now(self._clock)
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=TASK_RUNNING,
                )
                running_call = unit_of_work.llm_calls.get(llm_call_id)
                if running_call is None or (
                    running_call.status != LLM_RUNNING
                    or running_call.task_id != task.task_id
                    or running_call.conversation_id != submission.conversation_id
                    or running_call.request_id != submission.user_message.request_id
                    or running_call.purpose != CHAT_ORCHESTRATION
                ):
                    raise self._conflict(submission)
                failed_call = replace(
                    running_call,
                    status=LLM_FAILED,
                    structured_output_summary=None,
                    usage=None,
                    provider_request_id=provider_request_id,
                    completed_at=completed_at,
                    duration_ms=_duration_ms(
                        running_call.started_at,
                        completed_at,
                    ),
                    error_code=llm_error_code,
                    safe_error_message=llm_safe_error_message,
                )
                failed_task = replace(
                    task,
                    current_status=TASK_FAILED,
                    selected_tool_run_id=None,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=task_error_code,
                    safe_error_message=task_safe_error_message,
                )
                if unit_of_work.llm_calls.update(
                    failed_call,
                    expected_status=LLM_RUNNING,
                ) is None or unit_of_work.tasks.update(
                    failed_task,
                    expected_status=TASK_RUNNING,
                ) is None:
                    raise self._conflict(submission)
                unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_failed_finalize(
                actor_context,
                submission,
                llm_call_id=llm_call_id,
                llm_error_code=llm_error_code,
                llm_safe_error_message=llm_safe_error_message,
                task_error_code=task_error_code,
                task_safe_error_message=task_safe_error_message,
                provider_request_id=provider_request_id,
            )
            if recovered:
                return
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None

    def _recover_failed_finalize(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        llm_call_id: str,
        llm_error_code: str,
        llm_safe_error_message: str,
        task_error_code: str,
        task_safe_error_message: str,
        provider_request_id: str | None,
    ) -> bool:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                call = unit_of_work.llm_calls.get(llm_call_id)
                task = unit_of_work.tasks.get_owned(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
        except PersistenceError:
            return False
        if call is None or task is None:
            return False
        if (
            call.status == LLM_RUNNING
            and task.current_status == TASK_RUNNING
        ):
            return False
        projection = self._load_projection(
            actor_context,
            submission,
            llm_call_id,
        )
        persisted_call = projection.llm_call
        if (
            persisted_call is None
            or persisted_call.status != LLM_FAILED
            or persisted_call.structured_output_summary is not None
            or persisted_call.error_code != llm_error_code
            or persisted_call.safe_error_message != llm_safe_error_message
            or persisted_call.provider_request_id != provider_request_id
            or projection.task.current_status != TASK_FAILED
            or projection.task.error_code != task_error_code
            or projection.task.safe_error_message
            != task_safe_error_message
        ):
            raise self._conflict(submission)
        return True

    @staticmethod
    def _outcome_error(
        submission: PreparedSubmission,
        *,
        status_code: int,
        code: str,
        message: str,
        details: list[dict[str, str]] | None = None,
    ) -> OrchestrationOutcomeError:
        error_type = (
            AgentInternalError
            if code == "AGENT_INTERNAL_ERROR"
            else OrchestrationOutcomeError
        )
        return error_type(
            message,
            code=code,
            status_code=status_code,
            details=details,
            conversation_id=submission.conversation_id,
            task_id=submission.task.task_id,
        )

    def _load_projection(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        llm_call_id: str,
    ) -> ChatOrchestrationProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                conversation = unit_of_work.conversations.get_owned(
                    submission.conversation_id,
                    actor_context.actor_id,
                )
                user_message = unit_of_work.messages.get(
                    submission.user_message.message_id
                )
                task = unit_of_work.tasks.get_owned(
                    submission.task.task_id,
                    actor_context.actor_id,
                )
                call = unit_of_work.llm_calls.get(llm_call_id)
                if conversation is None or user_message is None or task is None or call is None:
                    raise ResourceNotFoundError()
                self._require_source_identity(
                    actor_context,
                    submission,
                    user_message,
                    task,
                )
                assistant_message = unit_of_work.messages.get_by_llm_call_id(
                    llm_call_id
                )
                revisions = (
                    unit_of_work.task_input_revisions.list_for_llm_call_id(
                        llm_call_id
                    )
                )
                all_revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                task_messages = unit_of_work.messages.list_for_task(task.task_id)
                self._require_projection_identity(
                    actor_context,
                    submission,
                    call,
                    assistant_message,
                    revisions,
                    all_revisions,
                    task_messages,
                )
                self._require_terminal_outcome_consistency(
                    submission,
                    task,
                    call,
                    assistant_message,
                    revisions,
                )
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=submission.conversation_id,
                task_id=submission.task.task_id,
            ) from None
        return ChatOrchestrationProjection(
            conversation_id=conversation.conversation_id,
            user_message=user_message,
            task=task,
            llm_call=call,
            assistant_message=assistant_message,
            revision=revisions[0] if revisions else None,
        )

    def _require_submission_sources(
        self,
        unit_of_work: object,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        *,
        expected_task_status: str | None,
    ) -> Task:
        conversation = unit_of_work.conversations.get_owned(
            submission.conversation_id,
            actor_context.actor_id,
        )
        user_message = unit_of_work.messages.get(
            submission.user_message.message_id
        )
        task = unit_of_work.tasks.get_owned_for_update(
            submission.task.task_id,
            actor_context.actor_id,
        )
        if conversation is None or user_message is None or task is None:
            raise ResourceNotFoundError()
        self._require_source_identity(
            actor_context,
            submission,
            user_message,
            task,
        )
        if (
            expected_task_status is not None
            and task.current_status != expected_task_status
        ):
            raise self._conflict(submission)
        return task

    @staticmethod
    def _require_source_identity(
        actor_context: ActorContext,
        submission: PreparedSubmission,
        user_message: Message,
        task: Task,
    ) -> None:
        if (
            user_message.role != "USER"
            or user_message.actor_id != actor_context.actor_id
            or task.actor_id != actor_context.actor_id
            or user_message.conversation_id != submission.conversation_id
            or task.conversation_id != submission.conversation_id
            or user_message.task_id != task.task_id
            or user_message.request_id != submission.user_message.request_id
            or user_message != submission.user_message
            or task.task_id != submission.task.task_id
        ):
            raise ChatOrchestrationService._conflict(submission)

    def _require_projection_identity(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
        call: LLMCall,
        assistant_message: Message | None,
        revisions: list[TaskInputRevision],
        all_revisions: list[TaskInputRevision],
        task_messages: list[Message],
    ) -> None:
        source_identity = (
            submission.task.task_id,
            submission.conversation_id,
            submission.user_message.request_id,
        )
        if (
            (call.task_id, call.conversation_id, call.request_id)
            != source_identity
            or call.purpose
            != (
                CHAT_ORCHESTRATION
                if submission.submission_mode == NEW_TASK
                else TOOL_INPUT_EXTRACTION
            )
            or call.status not in {LLM_SUCCEEDED, LLM_FAILED}
        ):
            raise ChatOrchestrationService._conflict(submission)
        if assistant_message is not None and (
            assistant_message.role != ASSISTANT
            or assistant_message.generation_source != LLM
            or assistant_message.actor_id != actor_context.actor_id
            or (
                assistant_message.task_id,
                assistant_message.conversation_id,
                assistant_message.request_id,
            )
            != source_identity
            or assistant_message.llm_call_id != call.llm_call_id
        ):
            raise ChatOrchestrationService._conflict(submission)
        if assistant_message is not None and (
            call.purpose == TOOL_INPUT_EXTRACTION
            or (
                call.purpose == CHAT_ORCHESTRATION
                and isinstance(call.structured_output_summary, Mapping)
                and call.structured_output_summary.get("route")
                == "TOOL_CANDIDATES"
            )
        ) and assistant_message.content_text != FOLLOW_UP_TEXT:
            raise ChatOrchestrationService._conflict(submission)
        for revision in revisions:
            expected_source_messages = [
                message.message_id
                for message in task_messages
                if message.role == "USER"
            ]
            expected_revision = (
                1
                if submission.submission_mode == NEW_TASK
                else max(item.revision for item in all_revisions)
            )
            if (
                revision.task_id != submission.task.task_id
                or revision.request_id != submission.user_message.request_id
                or revision.source_llm_call_id != call.llm_call_id
                or revision.source_message_ids != expected_source_messages
                or revision.revision != expected_revision
            ):
                raise ChatOrchestrationService._conflict(submission)

    def _require_terminal_outcome_consistency(
        self,
        submission: PreparedSubmission,
        task: Task,
        call: LLMCall,
        assistant_message: Message | None,
        revisions: list[TaskInputRevision],
    ) -> None:
        def reject() -> None:
            raise ChatOrchestrationService._conflict(submission)

        if (
            task.selected_tool_run_id is not None
            or task.selected_result_id is not None
            or (
                assistant_message is not None
                and assistant_message.structured_content is not None
            )
        ):
            reject()

        if call.status == LLM_FAILED:
            if (
                call.purpose == TOOL_INPUT_EXTRACTION
                and call.error_code == "CONTEXT_BUDGET_EXCEEDED"
                and task.current_status == TASK_NEEDS_INPUT
                and task.error_code is None
                and task.safe_error_message is None
                and call.structured_output_summary is None
                and assistant_message is None
                and not revisions
            ):
                return
            detailed_public_code = {
                "LLM_TIMEOUT": "UPSTREAM_TIMEOUT",
                "LLM_AUTHENTICATION_FAILED": "CHAT_ORCHESTRATION_FAILED",
                "LLM_BALANCE_EXHAUSTED": "CHAT_ORCHESTRATION_FAILED",
                "LLM_RATE_LIMITED": "CHAT_ORCHESTRATION_FAILED",
                "LLM_PROVIDER_UNAVAILABLE": "CHAT_ORCHESTRATION_FAILED",
                "LLM_REQUEST_REJECTED": "CHAT_ORCHESTRATION_FAILED",
                "LLM_EMPTY_RESPONSE": "CHAT_ORCHESTRATION_FAILED",
                "LLM_INVALID_JSON": "CHAT_ORCHESTRATION_FAILED",
                "LLM_SCHEMA_MISMATCH": "AGENT_INTERNAL_ERROR",
            }.get(call.error_code)
            legacy_error_pair = (
                call.error_code == task.error_code
                and call.safe_error_message == task.safe_error_message
            )
            detailed_error_pair = (
                detailed_public_code is not None
                and task.error_code == detailed_public_code
                and call.safe_error_message is not None
                and task.safe_error_message is not None
            )
            if (
                task.current_status != TASK_FAILED
                or call.error_code is None
                or not (legacy_error_pair or detailed_error_pair)
                or call.structured_output_summary is not None
                or assistant_message is not None
                or revisions
            ):
                reject()
            return

        summary = call.structured_output_summary
        if call.status != LLM_SUCCEEDED or summary is None:
            reject()
        if call.purpose == TOOL_INPUT_EXTRACTION:
            if (
                set(summary) != {"candidate_input_delta"}
                or task.task_type != TOOL_EXECUTION
                or task.bound_tool_ref is None
                or len(revisions) != 1
                or task.error_code is not None
                or task.safe_error_message is not None
            ):
                reject()
            revision = revisions[0]
            if (
                revision.candidate_tool_refs
                or self._plain_json(revision.raw_input)
                != self._plain_json(summary["candidate_input_delta"])
            ):
                reject()
            if task.current_status == TASK_READY:
                if assistant_message is not None or not revision.is_complete:
                    reject()
                return
            if task.current_status == TASK_NEEDS_INPUT:
                if (
                    assistant_message is None
                    or assistant_message.content_text != FOLLOW_UP_TEXT
                    or revision.is_complete
                ):
                    reject()
                return
            reject()
        route = summary.get("route")

        if route == "KNOWLEDGE_ANSWER":
            if (
                task.task_type != KNOWLEDGE_QA
                or task.current_status != TASK_SUCCEEDED
                or task.bound_tool_ref is not None
                or assistant_message is None
                or revisions
                or task.error_code is not None
                or task.safe_error_message is not None
            ):
                reject()
            return

        if (
            route == "TOOL_CANDIDATES"
            and task.task_type == TOOL_EXECUTION
            and task.current_status == TASK_FAILED
            and task.error_code == "VALIDATION_FAILED"
            and task.safe_error_message == VALIDATION_ERROR_MESSAGE
            and task.bound_tool_ref is not None
            and len(revisions) == 1
            and revisions[0].validation_errors
            and not revisions[0].missing_fields
            and not revisions[0].ambiguous_fields
            and not revisions[0].candidate_tool_refs
            and assistant_message is None
        ):
            return

        if route != "TOOL_CANDIDATES" or (
            task.task_type != TOOL_EXECUTION
            or len(revisions) != 1
            or task.error_code is not None
            or task.safe_error_message is not None
        ):
            reject()
        revision = revisions[0]
        if task.current_status == TASK_READY:
            if (
                task.bound_tool_ref is None
                or assistant_message is not None
                or not revision.is_complete
                or revision.candidate_tool_refs
            ):
                reject()
            return
        if task.current_status == TASK_NEEDS_INPUT:
            if (
                assistant_message is None
                or assistant_message.content_text != FOLLOW_UP_TEXT
            ):
                reject()
            if task.bound_tool_ref is None and len(revision.candidate_tool_refs) < 2:
                reject()
            if task.bound_tool_ref is not None and revision.candidate_tool_refs:
                reject()
            return
        reject()

    @staticmethod
    def _conflict(submission: PreparedSubmission) -> ApplicationConflictError:
        return ApplicationConflictError(
            conversation_id=submission.conversation_id,
            task_id=submission.task.task_id,
        )

    @staticmethod
    def _log_event(
        event: str,
        submission: PreparedSubmission,
        llm_call_id: str,
        *,
        status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        try:
            # Alembic fileConfig may disable loggers created before migration.
            logger.disabled = False
            logger.info(
                event,
                extra={
                    "event": event,
                    "request_id": submission.user_message.request_id,
                    "conversation_id": submission.conversation_id,
                    "task_id": submission.task.task_id,
                    "llm_call_id": llm_call_id,
                    "status": status,
                    "error_code": error_code,
                },
            )
        except Exception:
            # Observability is best-effort and must not change business facts.
            return

    @staticmethod
    def _context_budget(
        adapter: object,
        *,
        purpose: str,
    ) -> ContextBudget:
        configured = getattr(adapter, "context_budget", None)
        if isinstance(configured, ContextBudget):
            return configured
        if purpose == CHAT_ORCHESTRATION:
            prompt_limit, history_limit, max_output = 16_384, 8_192, 1_024
        else:
            prompt_limit, history_limit, max_output = 8_192, 4_096, 1_024
        safety_margin = 1_024
        return ContextBudget(
            prompt_limit_tokens=prompt_limit,
            history_token_budget=history_limit,
            safety_margin_tokens=safety_margin,
            context_window_tokens=prompt_limit + max_output + safety_margin,
            max_output_tokens=max_output,
        )

    def _chat_prompt_tokens(self, value: ChatOrchestrationInput) -> int:
        counter = getattr(self._orchestration_port, "count_prompt_tokens", None)
        if callable(counter):
            count = counter(value)
        else:
            count = self._token_counter.count_text(
                json.dumps(
                    {
                        "content_text": value.content_text,
                        "catalog": self._catalog_hash(value.routing_catalog),
                        "context": [
                            [turn.user_content, turn.assistant_content]
                            for turn in value.context_window.recent_turns
                        ],
                        "agent_state": self._plain_json(
                            value.context_window.agent_state
                        ),
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        if type(count) is not int or count < 0:
            raise ChatOrchestrationProtocolError(
                "Chat prompt token counter returned an invalid value."
            )
        return count

    def _tool_prompt_tokens(
        self,
        extractor: object,
        value: ToolInputExtractionInput,
    ) -> int:
        counter = getattr(extractor, "count_prompt_tokens", None)
        if callable(counter):
            count = counter(value)
        else:
            count = self._token_counter.count_text(
                json.dumps(
                    {
                        "content_text": value.content_text,
                        "tool_id": value.tool_context_ref.tool_id,
                        "schema": self._plain_json(value.candidate_input_schema),
                        "missing_fields": value.missing_fields,
                        "ambiguous_fields": value.ambiguous_fields,
                        "context": [
                            [turn.user_content, turn.assistant_content]
                            for turn in value.context_window.recent_turns
                        ],
                        "agent_state": self._plain_json(
                            value.context_window.agent_state
                        ),
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        if type(count) is not int or count < 0:
            raise ToolInputExtractionProtocolError(
                "Tool prompt token counter returned an invalid value."
            )
        return count

    def _required_adapter_text(self, field_name: str) -> str:
        value = getattr(self._orchestration_port, field_name, None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"orchestration_port.{field_name} is required.")
        return value.strip()

def _duration_ms(started_at: datetime | None, completed_at: datetime) -> int:
    if started_at is None or completed_at < started_at:
        raise ApplicationConflictError()
    return int((completed_at - started_at).total_seconds() * 1000)
