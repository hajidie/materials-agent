from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal

from materialsagent.domain.models.explanation import SUCCEEDED as EXPLANATION_SUCCEEDED
from materialsagent.domain.models.message import ASSISTANT, USER, Message
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.ports.conversation_context import (
    ContextBudget,
    ContextBuildResult,
    ContextReferenceResolution,
    ContextSource,
    ContextTurn,
    PromptContextWindow,
)
from materialsagent.domain.ports.tool_registry import ToolRef
from materialsagent.domain.ports.unit_of_work import UnitOfWorkFactory


PromptTokenCounter = Callable[[PromptContextWindow], int]
ContextPurpose = Literal["CHAT_ORCHESTRATION", "TOOL_INPUT_EXTRACTION"]


@dataclass(frozen=True, slots=True)
class _CandidateTurn:
    turn: ContextTurn
    sources: tuple[ContextSource, ...]
    resolutions: tuple[ContextReferenceResolution, ...]


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Context projection objects must use text keys.")
            result[key] = _plain_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError("Context projection must contain standard JSON values.")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _context_payload(window: PromptContextWindow) -> dict[str, object]:
    return {
        "summary_segments": list(window.summary_segments),
        "recent_turns": [
            {
                "user": turn.user_content,
                "assistant": turn.assistant_content,
            }
            for turn in window.recent_turns
        ],
        "agent_state": _plain_json(window.agent_state),
    }


class ConversationContextBuilder:
    def __init__(self, unit_of_work_factory: UnitOfWorkFactory, tool_registry: object) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._tool_registry = tool_registry

    def build(
        self,
        *,
        purpose: ContextPurpose,
        actor_id: str,
        conversation_id: str,
        current_message: Message,
        task_id: str | None,
        agent_state: Mapping[str, object],
        budget: ContextBudget,
        prompt_token_counter: PromptTokenCounter,
    ) -> ContextBuildResult:
        candidates = self._load_candidates(
            actor_id=actor_id,
            conversation_id=conversation_id,
            current_message=current_message,
            task_id=task_id,
        )
        empty_window = PromptContextWindow(agent_state=agent_state)
        base_tokens = prompt_token_counter(empty_window)
        effective_budget = budget.effective_prompt_budget
        available_history = max(
            0,
            min(
                budget.history_token_budget,
                effective_budget - base_tokens,
            ),
        )
        selected: tuple[_CandidateTurn, ...] = ()
        final_tokens = base_tokens
        if base_tokens <= effective_budget:
            for start in range(len(candidates) - 1, -1, -1):
                proposed = tuple(candidates[start:])
                proposed_window = PromptContextWindow(
                    recent_turns=tuple(item.turn for item in proposed),
                    agent_state=agent_state,
                )
                proposed_tokens = prompt_token_counter(proposed_window)
                if (
                    proposed_tokens <= effective_budget
                    and proposed_tokens - base_tokens <= available_history
                ):
                    selected = proposed
                    final_tokens = proposed_tokens
                    continue
                break
        window = PromptContextWindow(
            recent_turns=tuple(item.turn for item in selected),
            agent_state=agent_state,
        )
        selected_sources = tuple(
            source for item in selected for source in item.sources
        )
        resolutions = {
            resolution.context_ref: resolution
            for item in selected
            for resolution in item.resolutions
        }
        context_digest = sha256(
            _canonical_json(_context_payload(window)).encode("utf-8")
        ).hexdigest()
        return ContextBuildResult(
            window=window,
            reference_resolutions=resolutions,
            selected_sources=selected_sources,
            candidate_turn_count=len(candidates),
            selected_turn_count=len(selected),
            omitted_turn_count=len(candidates) - len(selected),
            base_prompt_tokens=base_tokens,
            history_tokens=max(0, final_tokens - base_tokens),
            final_prompt_tokens=final_tokens,
            budget=budget,
            context_digest=context_digest,
            budget_exceeded=base_tokens > effective_budget,
        )

    def _load_candidates(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        current_message: Message,
        task_id: str | None,
    ) -> tuple[_CandidateTurn, ...]:
        with self._unit_of_work_factory() as unit_of_work:
            if task_id is None:
                list_before = getattr(
                    unit_of_work.messages,
                    "list_for_conversation_before",
                    None,
                )
                if not callable(list_before):
                    return ()
                messages = list_before(
                    conversation_id,
                    actor_id,
                    before_created_at=current_message.created_at,
                    before_message_id=current_message.message_id,
                )
            else:
                messages = [
                    item
                    for item in unit_of_work.messages.list_for_task(task_id)
                    if (
                        item.created_at,
                        item.message_id,
                    ) < (
                        current_message.created_at,
                        current_message.message_id,
                    )
                ]
            messages = [
                item
                for item in messages
                if item.actor_id == actor_id
                and item.conversation_id == conversation_id
            ]
            by_request: dict[tuple[str, str], list[Message]] = {}
            for message in messages:
                by_request.setdefault(
                    (message.task_id, message.request_id), []
                ).append(message)
            ordered_users = sorted(
                (item for item in messages if item.role == USER),
                key=lambda item: (item.created_at, item.message_id),
            )
            latest_user_by_task = {
                item.task_id: max(
                    (
                        candidate
                        for candidate in ordered_users
                        if candidate.task_id == item.task_id
                    ),
                    key=lambda candidate: (
                        candidate.created_at,
                        candidate.message_id,
                    ),
                ).message_id
                for item in ordered_users
            }
            candidates: list[_CandidateTurn] = []
            reference_index = 0
            for user_message in ordered_users:
                task = unit_of_work.tasks.get(user_message.task_id)
                if (
                    task is None
                    or task.actor_id != actor_id
                    or task.conversation_id != conversation_id
                ):
                    continue
                request_messages = sorted(
                    by_request[(user_message.task_id, user_message.request_id)],
                    key=lambda item: (item.created_at, item.message_id),
                )
                assistants = [
                    item for item in request_messages if item.role == ASSISTANT
                ]
                revisions = unit_of_work.task_input_revisions.list_for_task(
                    task.task_id
                )
                revision = self._revision_for_request(
                    revisions,
                    user_message.request_id,
                )
                reference: ContextReferenceResolution | None = None
                if (
                    revision is not None
                    and revision.normalized_input is not None
                    and task.bound_tool_ref is not None
                ):
                    reference_index += 1
                    reference = ContextReferenceResolution(
                        context_ref=f"ctx_ref_{reference_index:04d}",
                        conversation_id=conversation_id,
                        task_id=task.task_id,
                        task_input_revision_id=revision.task_input_revision_id,
                        tool_ref=task.bound_tool_ref,
                        normalized_input=revision.normalized_input,
                    )
                result = (
                    unit_of_work.tool_results.get(task.selected_result_id)
                    if task.selected_result_id is not None
                    and latest_user_by_task.get(task.task_id)
                    == user_message.message_id
                    else None
                )
                selected_tool_run_id = getattr(task, "selected_tool_run_id", None)
                tool_runs = getattr(unit_of_work, "tool_runs", None)
                run = (
                    tool_runs.get(selected_tool_run_id)
                    if tool_runs is not None
                    and selected_tool_run_id is not None
                    and latest_user_by_task.get(task.task_id)
                    == user_message.message_id
                    else None
                )
                if run is not None and run.task_id != task.task_id:
                    run = None
                explanation = None
                if result is not None:
                    explanations = unit_of_work.explanations.list_for_result(
                        result.result_id
                    )
                    completed = [
                        item
                        for item in explanations
                        if item.status == EXPLANATION_SUCCEEDED
                        and item.text is not None
                    ]
                    if completed:
                        explanation = max(
                            completed,
                            key=lambda item: (
                                item.attempt_no,
                                item.explanation_id,
                            ),
                        )
                if (
                    not assistants
                    and revision is None
                    and run is None
                    and result is None
                ):
                    continue
                payload: dict[str, object] = {
                    "kind": "conversation_memory_v1",
                    "assistant_text": "\n".join(
                        item.content_text for item in assistants
                    ) or None,
                }
                sources: list[ContextSource] = [
                    ContextSource(
                        source_type="MESSAGE",
                        task_id=task.task_id,
                        message_ids=tuple(
                            item.message_id
                            for item in (user_message, *assistants)
                        ),
                    )
                ]
                if revision is not None:
                    payload["task_fact"] = {
                        "context_ref": (
                            reference.context_ref if reference is not None else None
                        ),
                        "tool_id": task.tool_id,
                        "normalized_input": _plain_json(revision.normalized_input),
                        "missing_fields": list(revision.missing_fields),
                        "ambiguous_fields": _plain_json(revision.ambiguous_fields),
                    }
                    sources.append(
                        ContextSource(
                            source_type="TASK_INPUT_REVISION",
                            task_id=task.task_id,
                            message_ids=(user_message.message_id,),
                            context_ref=(
                                reference.context_ref
                                if reference is not None
                                else None
                            ),
                            task_input_revision_id=revision.task_input_revision_id,
                        )
                    )
                if run is not None:
                    payload["tool_run"] = {
                        "status": str(run.current_status),
                        "attempt_no": run.attempt_no,
                        "requested_outputs": list(run.requested_outputs),
                        "completed_outputs": list(run.completed_outputs),
                        "failed_outputs": list(run.failed_outputs),
                        "error": (
                            None
                            if run.error_code is None
                            else {
                                "code": run.error_code,
                                "safe_message": run.safe_error_message,
                            }
                        ),
                    }
                    sources.append(
                        ContextSource(
                            source_type="TOOL_RUN",
                            task_id=task.task_id,
                            message_ids=(user_message.message_id,),
                            tool_run_id=run.tool_run_id,
                            projection_type="METADATA_ONLY",
                            projection_version="tool-run-metadata-v1",
                        )
                    )
                if result is not None:
                    projection, projection_type, projection_version = (
                        self._project_tool_result(result)
                    )
                    payload["tool_result"] = projection
                    sources.append(
                        ContextSource(
                            source_type="TOOL_RESULT",
                            task_id=task.task_id,
                            message_ids=(user_message.message_id,),
                            tool_result_id=result.result_id,
                            projection_type=projection_type,
                            projection_version=projection_version,
                        )
                    )
                if explanation is not None:
                    payload["explanation"] = explanation.text
                    sources.append(
                        ContextSource(
                            source_type="EXPLANATION",
                            task_id=task.task_id,
                            message_ids=(user_message.message_id,),
                            explanation_id=explanation.explanation_id,
                        )
                    )
                candidates.append(
                    _CandidateTurn(
                        turn=ContextTurn(
                            user_content=_canonical_json(
                                {
                                    "kind": "conversation_memory_user_v1",
                                    "content_text": user_message.content_text,
                                }
                            ),
                            assistant_content=_canonical_json(payload),
                        ),
                        sources=tuple(sources),
                        resolutions=(() if reference is None else (reference,)),
                    )
                )
        return tuple(candidates)

    @staticmethod
    def _revision_for_request(
        revisions: list[TaskInputRevision],
        request_id: str,
    ) -> TaskInputRevision | None:
        matching = [item for item in revisions if item.request_id == request_id]
        if not matching:
            return None
        return max(
            matching,
            key=lambda item: (item.revision, item.task_input_revision_id),
        )

    def _project_tool_result(
        self,
        result: ToolResult,
    ) -> tuple[dict[str, object], str, str]:
        metadata: dict[str, object] = {
            "status": result.status,
            "requested_outputs": list(result.requested_outputs),
            "completed_outputs": list(result.completed_outputs),
            "failed_outputs": list(result.failed_outputs),
        }
        try:
            definition = self._tool_registry.resolve(result.tool_id)
        except Exception:
            return metadata, "METADATA_ONLY", "tool-result-metadata-v1"
        projector = getattr(definition, "context_projector", None)
        version = getattr(definition, "context_projection_version", "1")
        if not callable(projector):
            return metadata, "METADATA_ONLY", "tool-result-metadata-v1"
        source = {
            **metadata,
            "data": _plain_json(result.data),
            "warnings": _plain_json(result.warnings),
            "error": _plain_json(result.error),
        }
        try:
            projected = _plain_json(projector(source))
            if not isinstance(projected, dict):
                raise ValueError("Tool context projection must be an object.")
            if len(_canonical_json(projected).encode("utf-8")) > 8192:
                raise ValueError("Tool context projection is too large.")
        except Exception:
            return metadata, "METADATA_ONLY", "tool-result-metadata-v1"
        return projected, "TOOL_DEFINED", str(version)
