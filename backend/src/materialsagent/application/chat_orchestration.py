from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import logging
from typing import Final

from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import (
    Clock,
    IdFactory,
    _default_clock,
    _default_id_factory,
    _validated_utc_now,
)
from materialsagent.application.errors import (
    ApplicationConflictError,
    OrchestrationOutcomeError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.messages import (
    NEW_TASK,
    SUPPLEMENT_TASK,
    PreparedSubmission,
)
from materialsagent.application.zta35g_input import (
    ZTA35GValidationResult,
    normalize_zta35g_candidate,
)
from materialsagent.domain.models.llm_call import (
    CHAT_ORCHESTRATION,
    FAILED as LLM_FAILED,
    PENDING as LLM_PENDING,
    RUNNING as LLM_RUNNING,
    SUCCEEDED as LLM_SUCCEEDED,
    LLMCall,
)
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
    ChatOrchestrationPort,
    ChatOrchestrationTimeoutError,
    KnowledgeAnswer,
    NeedsInputCandidate,
    ToolCandidate,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWorkFactory,
)
PROMPT_TEMPLATE_ID: Final = "chat-orchestration"
PROMPT_TEMPLATE_VERSION: Final = "1"
GENERATION_PARAMETERS: Final = {"temperature": 0, "max_tokens": 256}
FOLLOW_UP_TEXT: Final = "请补充缺失参数或明确存在歧义的参数。"
VALIDATION_ERROR_MESSAGE: Final = "输入参数未通过验证。"
TOOL_UNAVAILABLE_MESSAGE: Final = "当前阶段尚未开放材料工具执行。"
TIMEOUT_MESSAGE: Final = "聊天编排服务响应超时。"
ORCHESTRATION_FAILURE_MESSAGE: Final = "聊天编排服务暂不可用。"
PROTOCOL_FAILURE_MESSAGE: Final = "聊天编排服务返回了无效响应。"


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


class ChatOrchestrationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        orchestration_port: ChatOrchestrationPort,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        tool_chain_enabled: bool = False,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._orchestration_port = orchestration_port
        self._provider = self._required_adapter_text("provider")
        self._model_name = self._required_adapter_text("model_name")
        self._clock = clock or _default_clock
        self._id_factory = id_factory or _default_id_factory
        self._tool_chain_enabled = tool_chain_enabled

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
        )

    def orchestrate_submission(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> ChatOrchestrationProjection:
        call = self._prepare_call(actor_context, submission)
        started = self._start_call(actor_context, submission, call)
        if not started.invoke_adapter:
            return self.load_current_submission(actor_context, submission)
        call = started.call
        self._log_event("chat_orchestration_started", submission, call.llm_call_id)
        try:
            result = self._orchestration_port.orchestrate(
                ChatOrchestrationInput(
                    task_id=submission.task.task_id,
                    conversation_id=submission.conversation_id,
                    request_id=submission.user_message.request_id,
                    content_text=submission.user_message.content_text,
                )
            )
            if not isinstance(
                result,
                (KnowledgeAnswer, ToolCandidate, NeedsInputCandidate),
            ):
                raise ChatOrchestrationProtocolError(
                    "Chat orchestration result has an invalid runtime type."
                )
            if (
                submission.submission_mode == SUPPLEMENT_TASK
                and isinstance(result, KnowledgeAnswer)
            ):
                raise ChatOrchestrationProtocolError(
                    "A task supplement must remain a tool candidate."
                )
        except ChatOrchestrationTimeoutError:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                error_code="UPSTREAM_TIMEOUT",
                safe_error_message=TIMEOUT_MESSAGE,
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
        except ChatOrchestrationProviderError:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                error_code="CHAT_ORCHESTRATION_FAILED",
                safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
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
        except ChatOrchestrationProtocolError:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                error_code="CHAT_ORCHESTRATION_FAILED",
                safe_error_message=PROTOCOL_FAILURE_MESSAGE,
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
                status_code=502,
                code="CHAT_ORCHESTRATION_FAILED",
                message=PROTOCOL_FAILURE_MESSAGE,
            ) from None
        except Exception:
            self._finalize_failure(
                actor_context,
                submission,
                call.llm_call_id,
                error_code="CHAT_ORCHESTRATION_FAILED",
                safe_error_message=ORCHESTRATION_FAILURE_MESSAGE,
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
            result=result,
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
                calls = [
                    call
                    for call in unit_of_work.llm_calls.list_for_task(
                        task.task_id,
                        request_id=message.request_id,
                    )
                    if call.purpose == CHAT_ORCHESTRATION
                ]
                if len(calls) > 1:
                    raise self._conflict(submission)
                call = calls[0] if calls else None
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
                    if (
                        task.current_status != TASK_RUNNING
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
        result: KnowledgeAnswer | ToolCandidate | NeedsInputCandidate,
    ) -> ChatOrchestrationProjection:
        if not isinstance(
            result,
            (KnowledgeAnswer, ToolCandidate, NeedsInputCandidate),
        ):
            raise TypeError("result must be a chat orchestration result.")
        validation = (
            None
            if isinstance(result, KnowledgeAnswer)
            else normalize_zta35g_candidate(result)
        )
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
                        validation,
                        completed_at,
                    )
                    unit_of_work.commit()
        except PersistenceError as error:
            recovered = self._recover_successful_finalize(
                actor_context,
                submission,
                llm_call_id=llm_call_id,
                result=result,
                validation=validation,
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
        result: KnowledgeAnswer | ToolCandidate | NeedsInputCandidate,
        validation: ZTA35GValidationResult | None,
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
            validation=validation,
        ):
            raise self._conflict(submission)
        return projection

    @staticmethod
    def _is_equivalent_success_projection(
        projection: ChatOrchestrationProjection,
        *,
        result: KnowledgeAnswer | ToolCandidate | NeedsInputCandidate,
        validation: ZTA35GValidationResult | None,
    ) -> bool:
        call = projection.llm_call
        if call is None or call.status != LLM_SUCCEEDED:
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
                and projection.revision is None
            )
        if validation is None or projection.revision is None:
            return False
        payloads = validation.to_revision_payloads()
        has_missing_or_ambiguous = bool(
            validation.missing_fields or validation.ambiguous_fields
        )
        expected_summary = (
            {
                "route": "NEEDS_INPUT",
                "tool_id": result.tool_id,
                "missing_fields": tuple(validation.missing_fields),
                "ambiguous_fields": tuple(
                    item.field for item in validation.ambiguous_fields
                ),
            }
            if has_missing_or_ambiguous
            else {
                "route": "TOOL_EXECUTION",
                "tool_id": result.tool_id,
            }
        )
        expected_validation_errors = (
            []
            if has_missing_or_ambiguous
            else payloads["validation_errors"]
        )
        revision = projection.revision
        return (
            call.structured_output_summary == expected_summary
            and (
                not has_missing_or_ambiguous
                or (
                    projection.assistant_message is not None
                    and projection.assistant_message.content_text
                    == FOLLOW_UP_TEXT
                )
            )
            and revision.raw_input == payloads["raw_input"]
            and revision.normalized_input == payloads["normalized_input"]
            and revision.missing_fields == payloads["missing_fields"]
            and revision.ambiguous_fields == payloads["ambiguous_fields"]
            and revision.validation_errors == expected_validation_errors
        )

    def _prepare_call(
        self,
        actor_context: ActorContext,
        submission: PreparedSubmission,
    ) -> LLMCall:
        timestamp = _validated_utc_now(self._clock)
        call = LLMCall(
            llm_call_id=self._id_factory("llm"),
            task_id=submission.task.task_id,
            conversation_id=submission.conversation_id,
            request_id=submission.user_message.request_id,
            purpose=CHAT_ORCHESTRATION,
            input_result_id=None,
            provider=self._provider,
            model_name=self._model_name,
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            prompt_digest=self._prompt_digest(submission.user_message),
            generation_parameters=GENERATION_PARAMETERS,
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
        )
        try:
            with self._unit_of_work_factory() as unit_of_work:
                self._require_submission_sources(
                    unit_of_work,
                    actor_context,
                    submission,
                    expected_task_status=(
                        TASK_PENDING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    ),
                )
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
                    expected_task_status=(
                        TASK_PENDING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    ),
                )
                persisted_call = unit_of_work.llm_calls.get(call.llm_call_id)
                if persisted_call != call:
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
                    expected_status=(
                        TASK_PENDING
                        if submission.submission_mode == NEW_TASK
                        else TASK_NEEDS_INPUT
                    ),
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
        result: KnowledgeAnswer | ToolCandidate | NeedsInputCandidate,
        validation: ZTA35GValidationResult | None,
        completed_at: datetime,
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
            if validation is None:
                raise self._conflict(submission)
            payloads = validation.to_revision_payloads()
            has_missing_or_ambiguous = bool(
                validation.missing_fields or validation.ambiguous_fields
            )
            has_validation_errors = bool(validation.validation_errors)
            if has_missing_or_ambiguous:
                summary = {
                    "route": "NEEDS_INPUT",
                    "tool_id": result.tool_id,
                    "missing_fields": list(validation.missing_fields),
                    "ambiguous_fields": [
                        item.field for item in validation.ambiguous_fields
                    ],
                }
                public_validation_errors: list[dict[str, object]] = []
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
                finalized_task = replace(
                    task,
                    task_type=TOOL_EXECUTION,
                    current_status=TASK_NEEDS_INPUT,
                    selected_tool_run_id=None,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=None,
                    error_code=None,
                    safe_error_message=None,
                )
            else:
                summary = {
                    "route": "TOOL_EXECUTION",
                    "tool_id": result.tool_id,
                }
                public_validation_errors = payloads["validation_errors"]
                if has_validation_errors:
                    error_code = "VALIDATION_FAILED"
                    safe_error_message = VALIDATION_ERROR_MESSAGE
                else:
                    error_code = (
                        None
                        if self._tool_chain_enabled
                        else "TOOL_UNAVAILABLE"
                    )
                    safe_error_message = (
                        None
                        if self._tool_chain_enabled
                        else TOOL_UNAVAILABLE_MESSAGE
                    )
                finalized_task = replace(
                    task,
                    task_type=TOOL_EXECUTION,
                    current_status=(
                        TASK_RUNNING
                        if self._tool_chain_enabled
                        and not has_validation_errors
                        else TASK_FAILED
                    ),
                    selected_tool_run_id=None,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=(
                        None
                        if self._tool_chain_enabled
                        and not has_validation_errors
                        else completed_at
                    ),
                    error_code=error_code,
                    safe_error_message=safe_error_message,
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
                raw_input=payloads["raw_input"],
                normalized_input=payloads["normalized_input"],
                missing_fields=payloads["missing_fields"],
                ambiguous_fields=payloads["ambiguous_fields"],
                validation_errors=public_validation_errors,
                created_at=completed_at,
            )
        succeeded_call = replace(
            running_call,
            status=LLM_SUCCEEDED,
            structured_output_summary=summary,
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
        error_code: str,
        safe_error_message: str,
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
                    completed_at=completed_at,
                    duration_ms=_duration_ms(
                        running_call.started_at,
                        completed_at,
                    ),
                    error_code=error_code,
                    safe_error_message=safe_error_message,
                )
                failed_task = replace(
                    task,
                    current_status=TASK_FAILED,
                    selected_tool_run_id=None,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=error_code,
                    safe_error_message=safe_error_message,
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
                error_code=error_code,
                safe_error_message=safe_error_message,
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
        error_code: str,
        safe_error_message: str,
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
            or persisted_call.error_code != error_code
            or persisted_call.safe_error_message != safe_error_message
            or projection.task.current_status != TASK_FAILED
            or projection.task.error_code != error_code
            or projection.task.safe_error_message != safe_error_message
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
        return OrchestrationOutcomeError(
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
        task = unit_of_work.tasks.get_owned(
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
            or call.purpose != CHAT_ORCHESTRATION
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
        ):
            reject()

        if call.status == LLM_FAILED:
            if (
                task.current_status != TASK_FAILED
                or call.error_code is None
                or call.error_code != task.error_code
                or call.safe_error_message != task.safe_error_message
                or call.structured_output_summary is not None
                or assistant_message is not None
                or revisions
            ):
                reject()
            return

        summary = call.structured_output_summary
        if call.status != LLM_SUCCEEDED or summary is None:
            reject()
        route = summary.get("route")

        if route == "KNOWLEDGE_ANSWER":
            if (
                task.task_type != KNOWLEDGE_QA
                or task.current_status != TASK_SUCCEEDED
                or assistant_message is None
                or revisions
                or task.error_code is not None
                or task.safe_error_message is not None
            ):
                reject()
            return

        if route == "NEEDS_INPUT":
            if (
                task.task_type != TOOL_EXECUTION
                or task.current_status != TASK_NEEDS_INPUT
                or assistant_message is None
                or len(revisions) != 1
                or task.error_code is not None
                or task.safe_error_message is not None
            ):
                reject()
            return

        if route != "TOOL_EXECUTION" or (
            task.task_type != TOOL_EXECUTION
            or assistant_message is not None
            or len(revisions) != 1
        ):
            reject()

        revision = revisions[0]
        if (
            self._tool_chain_enabled
            and task.current_status == TASK_RUNNING
            and task.error_code is None
            and task.safe_error_message is None
            and not revision.validation_errors
        ):
            return
        if task.current_status != TASK_FAILED:
            reject()
        if task.error_code == "VALIDATION_FAILED":
            if not revision.validation_errors:
                reject()
            return
        if task.error_code == "TOOL_UNAVAILABLE":
            if revision.validation_errors:
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

    def _required_adapter_text(self, field_name: str) -> str:
        value = getattr(self._orchestration_port, field_name, None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"orchestration_port.{field_name} is required.")
        return value.strip()

    @staticmethod
    def _prompt_digest(user_message: Message) -> str:
        canonical = (
            f"{PROMPT_TEMPLATE_ID}:{PROMPT_TEMPLATE_VERSION}\n"
            f"{user_message.content_text}"
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


def _duration_ms(started_at: datetime | None, completed_at: datetime) -> int:
    if started_at is None or completed_at < started_at:
        raise ApplicationConflictError()
    return int((completed_at - started_at).total_seconds() * 1000)
