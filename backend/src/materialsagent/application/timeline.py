from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping
from typing import Literal

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationInternalError,
    ApplicationValidationError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.timeline_cursor import (
    TimelineCursor,
    TimelineCursorCodec,
)
from materialsagent.application.explanation_service import (
    select_explanation_attempts,
)
from materialsagent.application.result_service import (
    ResultArtifactProjection,
)
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.explanation import (
    NaturalLanguageExplanation,
)
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.timeline_query import (
    TimelineKey,
    TimelineQueryPort,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError


_RANKS = {
    "USER_MESSAGE": 10,
    "ASSISTANT_MESSAGE": 20,
    "TOOL_TASK": 30,
}
_INPUT_THREAD_LIMIT = 50


@dataclass(frozen=True, slots=True)
class PublicMessage:
    message_id: str
    role: Literal["USER", "ASSISTANT"]
    content_text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SafeTimelineError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PublicTaskSummary:
    task_id: str
    task_type: str | None
    status: str
    selected_tool_run_id: str | None
    selected_result_id: str | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    error_code: str | None
    safe_error_message: str | None
    tool_id: str | None = None
    bound_tool_version: str | None = None
    bound_schema_hash: str | None = None


@dataclass(frozen=True, slots=True)
class PublicToolRunSummary:
    tool_run_id: str
    attempt_no: int
    tool_id: str
    tool_version: str
    schema_hash: str
    status: str
    requested_outputs: tuple[str, ...]
    completed_outputs: tuple[str, ...]
    failed_outputs: tuple[str, ...]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    diagnostics_summary: tuple[dict[str, object], ...]
    error: SafeTimelineError | None
    is_selected: bool


@dataclass(frozen=True, slots=True)
class PublicToolRunCollection:
    attempt_count: int
    selected_tool_run: PublicToolRunSummary | None
    has_history: bool


@dataclass(frozen=True, slots=True)
class PublicResultSummary:
    result_id: str
    tool_run_id: str
    status: str
    requested_outputs: tuple[str, ...]
    completed_outputs: tuple[str, ...]
    failed_outputs: tuple[str, ...]
    data: dict[str, object]
    warnings: tuple[object, ...]
    provenance: dict[str, object]
    error: dict[str, object] | None
    tool_id: str
    tool_version: str
    schema_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PublicExplanationSummary:
    explanation_id: str
    attempt_no: int
    status: str
    language: str
    text: str | None
    completed_at: datetime | None
    duration_ms: int | None
    error_code: str | None
    safe_error_message: str | None


@dataclass(frozen=True, slots=True)
class NeedsInputSummary:
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[dict[str, object], ...]
    normalized_input: dict[str, object] | None
    candidate_tool_refs: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class MessageTimelineItem:
    item_type: Literal["USER_MESSAGE", "ASSISTANT_MESSAGE"]
    item_id: str
    task_id: str
    anchor_at: datetime
    message: PublicMessage


@dataclass(frozen=True, slots=True)
class ToolTaskTimelineItem:
    item_type: Literal["TOOL_TASK"]
    item_id: str
    task_id: str
    anchor_at: datetime
    initial_user_message: PublicMessage | None
    task: PublicTaskSummary
    input_thread: tuple[PublicMessage, ...]
    input_thread_count: int
    input_thread_truncated: bool
    tool_runs: PublicToolRunCollection
    result: PublicResultSummary | None
    assets: tuple[ResultArtifactProjection, ...]
    explanation: PublicExplanationSummary | None
    latest_explanation_failure: PublicExplanationSummary | None
    needs_input: NeedsInputSummary | None
    errors: tuple[SafeTimelineError, ...]


TimelineItem = MessageTimelineItem | ToolTaskTimelineItem


@dataclass(frozen=True, slots=True)
class TimelinePage:
    conversation: Conversation
    items: tuple[TimelineItem, ...]
    next_cursor: str | None
    has_more: bool


def _public_message(message: Message) -> PublicMessage:
    return PublicMessage(
        message_id=message.message_id,
        role=message.role,  # type: ignore[arg-type]
        content_text=message.content_text,
        created_at=message.created_at,
    )


def _public_task(task: Task) -> PublicTaskSummary:
    return PublicTaskSummary(
        task_id=task.task_id,
        task_type=task.task_type,
        status=task.current_status,
        selected_tool_run_id=task.selected_tool_run_id,
        selected_result_id=task.selected_result_id,
        created_at=task.created_at,
        started_at=task.started_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
        error_code=task.error_code,
        safe_error_message=task.safe_error_message,
        tool_id=task.tool_id,
        bound_tool_version=task.bound_tool_version,
        bound_schema_hash=task.bound_schema_hash,
    )


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _plain_json(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, list):
        return [_plain_json(item) for item in value]
    return value


def _safe_diagnostics(
    diagnostics: list[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    def safe_text(value: object, maximum: int) -> str | None:
        return (
            value
            if (
                isinstance(value, str)
                and bool(value.strip())
                and len(value) <= maximum
                and value.isprintable()
            )
            else None
        )

    summaries: list[dict[str, object]] = []
    for item in diagnostics:
        duration = item.get("duration_ms")
        summaries.append(
            {
                "step": safe_text(item.get("step"), 128),
                "status": safe_text(item.get("status"), 64),
                "duration_ms": (
                    duration
                    if (
                        type(duration) is int
                        and duration >= 0
                    )
                    else None
                ),
                "error_code": safe_text(
                    item.get("error_code"),
                    128,
                ),
                "safe_error_message": safe_text(
                    item.get("safe_error_message"),
                    256,
                ),
            }
        )
    return tuple(summaries)


def project_tool_run(
    tool_run: ToolRun,
    *,
    is_selected: bool,
) -> PublicToolRunSummary:
    return PublicToolRunSummary(
        tool_run_id=tool_run.tool_run_id,
        attempt_no=tool_run.attempt_no,
        tool_id=tool_run.tool_id,
        tool_version=tool_run.tool_version,
        schema_hash=tool_run.schema_hash,
        status=tool_run.current_status,
        requested_outputs=tuple(tool_run.requested_outputs),
        completed_outputs=tuple(tool_run.completed_outputs),
        failed_outputs=tuple(tool_run.failed_outputs),
        created_at=tool_run.created_at,
        started_at=tool_run.started_at,
        completed_at=tool_run.completed_at,
        duration_ms=tool_run.duration_ms,
        diagnostics_summary=_safe_diagnostics(tool_run.diagnostics),
        error=(
            None
            if (
                tool_run.error_code is None
                and tool_run.safe_error_message is None
            )
            else SafeTimelineError(
                code=tool_run.error_code or "TOOL_RUN_FAILED",
                message=(
                    tool_run.safe_error_message
                    or "材料工具执行失败。"
                ),
            )
        ),
        is_selected=is_selected,
    )


def _public_result(result: ToolResult) -> PublicResultSummary:
    return PublicResultSummary(
        result_id=result.result_id,
        tool_run_id=result.tool_run_id,
        status=result.status,
        requested_outputs=tuple(result.requested_outputs),
        completed_outputs=tuple(result.completed_outputs),
        failed_outputs=tuple(result.failed_outputs),
        data=_plain_json(result.data),  # type: ignore[arg-type]
        warnings=tuple(_plain_json(result.warnings)),  # type: ignore[arg-type]
        provenance=_plain_json(result.provenance),  # type: ignore[arg-type]
        error=(
            None
            if result.error is None
            else _plain_json(result.error)  # type: ignore[arg-type]
        ),
        tool_id=result.tool_id,
        tool_version=result.tool_version,
        schema_hash=result.schema_hash,
        created_at=result.created_at,
    )


def _public_explanation(
    explanation: NaturalLanguageExplanation | None,
) -> PublicExplanationSummary | None:
    if explanation is None:
        return None
    return PublicExplanationSummary(
        explanation_id=explanation.explanation_id,
        attempt_no=explanation.attempt_no,
        status=explanation.status,
        language=explanation.language,
        text=explanation.text,
        completed_at=explanation.completed_at,
        duration_ms=explanation.duration_ms,
        error_code=explanation.error_code,
        safe_error_message=explanation.safe_error_message,
    )


def _needs_input(
    task: Task,
    revisions: list[TaskInputRevision],
) -> NeedsInputSummary | None:
    if task.current_status != "NEEDS_INPUT":
        return None
    if not revisions:
        raise ApplicationInternalError(task_id=task.task_id)
    latest = max(
        revisions,
        key=lambda item: (
            item.revision,
            item.task_input_revision_id,
        ),
    )
    return NeedsInputSummary(
        missing_fields=tuple(latest.missing_fields),
        ambiguous_fields=tuple(
            dict(item) for item in latest.ambiguous_fields
        ),
        normalized_input=(
            None
            if latest.normalized_input is None
            else dict(latest.normalized_input)
        ),
        candidate_tool_refs=tuple(
            dict(item) for item in latest.candidate_tool_refs
        ),
    )


def _key_tuple(key: TimelineKey) -> tuple[datetime, int, str]:
    return (key.anchor_at, key.item_type_rank, key.item_id)


class TimelineQueryService:
    def __init__(
        self,
        query: TimelineQueryPort,
        cursor_codec: TimelineCursorCodec,
    ) -> None:
        self._query = query
        self._cursor_codec = cursor_codec

    def get_page(
        self,
        actor_context: ActorContext,
        *,
        conversation_id: str,
        limit: int,
        cursor: str | None,
    ) -> TimelinePage:
        if (
            not isinstance(conversation_id, str)
            or not conversation_id.strip()
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ApplicationValidationError()
        after = (
            None
            if cursor is None
            else self._cursor_codec.decode(
                cursor,
                expected_conversation_id=conversation_id,
            )
        )
        try:
            page = self._query.fetch_owned_page(
                actor_id=actor_context.actor_id,
                conversation_id=conversation_id,
                after=after,
                limit=limit,
            )
        except PersistenceError as error:
            raise from_persistence_error(
                error,
                conversation_id=conversation_id,
            ) from None
        if page is None:
            raise ResourceNotFoundError(conversation_id=conversation_id)
        try:
            items = self._assemble(
                actor_context,
                conversation_id=conversation_id,
                page=page,
            )
        except ApplicationInternalError:
            raise
        except Exception:
            raise ApplicationInternalError(
                conversation_id=conversation_id
            ) from None
        next_cursor = None
        if page.has_more:
            if not items:
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
            last = items[-1]
            next_cursor = self._cursor_codec.encode(
                TimelineCursor(
                    conversation_id=conversation_id,
                    anchor_at=last.anchor_at,
                    item_type_rank=_RANKS[last.item_type],
                    item_id=last.item_id,
                )
            )
        return TimelinePage(
            conversation=page.conversation,
            items=items,
            next_cursor=next_cursor,
            has_more=page.has_more,
        )

    @staticmethod
    def _assemble(
        actor_context: ActorContext,
        *,
        conversation_id: str,
        page,
    ) -> tuple[TimelineItem, ...]:
        if (
            page.conversation.conversation_id != conversation_id
            or page.conversation.actor_id != actor_context.actor_id
            or len(page.keys) > 50
            or tuple(sorted(page.keys, key=_key_tuple)) != page.keys
            or len({_key_tuple(key) for key in page.keys})
            != len(page.keys)
        ):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        messages = {item.message_id: item for item in page.messages}
        tasks = {item.task_id: item for item in page.tasks}
        if len(messages) != len(page.messages) or len(tasks) != len(page.tasks):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        for message in page.messages:
            if (
                message.actor_id != actor_context.actor_id
                or message.conversation_id != conversation_id
            ):
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
        for task in page.tasks:
            if (
                task.actor_id != actor_context.actor_id
                or task.conversation_id != conversation_id
            ):
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
        key_task_ids = {key.task_id for key in page.keys}
        if not key_task_ids <= set(tasks):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        tool_task_ids = {
            key.task_id
            for key in page.keys
            if key.item_type == "TOOL_TASK"
        }
        if any(
            tool_run.task_id not in tool_task_ids
            for tool_run in page.tool_runs
        ) or any(
            revision.task_id not in tool_task_ids
            for revision in page.revisions
        ):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        selected_result_ids = {
            task.selected_result_id
            for task in page.tasks
            if (
                task.task_id in tool_task_ids
                and task.selected_result_id is not None
            )
        }
        if {
            item.result_id for item in page.results
        } != selected_result_ids:
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        if any(
            link.result_id not in selected_result_ids
            for link in page.result_asset_links
        ):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        linked_asset_ids = {
            link.asset_id for link in page.result_asset_links
        }
        if {
            asset.asset_id for asset in page.assets
        } != linked_asset_ids:
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        if any(
            explanation.result_id not in selected_result_ids
            or explanation.task_id not in tool_task_ids
            for explanation in page.explanations
        ):
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )
        explanation_call_ids = {
            explanation.llm_call_id
            for explanation in page.explanations
        }
        if {
            call.llm_call_id for call in page.llm_calls
        } != explanation_call_ids:
            raise ApplicationInternalError(
                conversation_id=conversation_id
            )

        result: list[TimelineItem] = []
        for key in page.keys:
            if (
                key.item_type_rank != _RANKS.get(key.item_type)
                or key.item_id
                != (
                    key.task_id
                    if key.item_type == "TOOL_TASK"
                    else key.item_id
                )
            ):
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
            if key.item_type != "TOOL_TASK":
                message = messages.get(key.item_id)
                message_task = tasks.get(key.task_id)
                if (
                    message is None
                    or message_task is None
                    or message_task.task_type == "TOOL_EXECUTION"
                    or message.task_id != key.task_id
                    or message.role
                    != (
                        "USER"
                        if key.item_type == "USER_MESSAGE"
                        else "ASSISTANT"
                    )
                    or message.created_at != key.anchor_at
                ):
                    raise ApplicationInternalError(
                        conversation_id=conversation_id
                    )
                result.append(
                    MessageTimelineItem(
                        item_type=key.item_type,
                        item_id=key.item_id,
                        task_id=key.task_id,
                        anchor_at=key.anchor_at,
                        message=_public_message(message),
                    )
                )
                continue

            task = tasks.get(key.task_id)
            if task is None or task.task_type != "TOOL_EXECUTION":
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
            task_messages = sorted(
                (
                    message
                    for message in page.messages
                    if message.task_id == task.task_id
                ),
                key=lambda item: (item.created_at, item.message_id),
            )
            user_messages = [
                message
                for message in task_messages
                if message.role == "USER"
            ]
            initial = user_messages[0] if user_messages else None
            expected_anchor = (
                initial.created_at if initial is not None else task.created_at
            )
            if key.anchor_at != expected_anchor:
                raise ApplicationInternalError(
                    conversation_id=conversation_id
                )
            remaining = [
                message
                for message in task_messages
                if initial is None or message.message_id != initial.message_id
            ]
            selected_thread = remaining[-_INPUT_THREAD_LIMIT:]
            task_runs = sorted(
                (
                    tool_run
                    for tool_run in page.tool_runs
                    if tool_run.task_id == task.task_id
                ),
                key=lambda item: (
                    item.created_at,
                    item.attempt_no,
                    item.tool_run_id,
                ),
            )
            if any(
                tool_run.task_id != task.task_id
                for tool_run in page.tool_runs
                if tool_run.tool_run_id
                == task.selected_tool_run_id
            ):
                raise ApplicationInternalError(task_id=task.task_id)
            selected_run = next(
                (
                    tool_run
                    for tool_run in task_runs
                    if tool_run.tool_run_id
                    == task.selected_tool_run_id
                ),
                None,
            )
            selected_result = next(
                (
                    item
                    for item in page.results
                    if item.result_id == task.selected_result_id
                ),
                None,
            )
            if (
                (task.selected_tool_run_id is not None)
                != (selected_run is not None)
                or (task.selected_result_id is not None)
                != (selected_result is not None)
            ):
                raise ApplicationInternalError(task_id=task.task_id)
            if selected_result is not None:
                if (
                    selected_run is None
                    or selected_result.actor_id != task.actor_id
                    or selected_result.task_id != task.task_id
                    or selected_result.tool_run_id
                    != selected_run.tool_run_id
                    or selected_result.status
                    != selected_run.current_status
                    or tuple(selected_result.requested_outputs)
                    != tuple(selected_run.requested_outputs)
                    or tuple(selected_result.completed_outputs)
                    != tuple(selected_run.completed_outputs)
                    or tuple(selected_result.failed_outputs)
                    != tuple(selected_run.failed_outputs)
                    or selected_result.tool_id != selected_run.tool_id
                    or selected_result.tool_version
                    != selected_run.tool_version
                    or selected_result.schema_hash
                    != selected_run.schema_hash
                ):
                    raise ApplicationInternalError(task_id=task.task_id)

            links = sorted(
                (
                    link
                    for link in page.result_asset_links
                    if selected_result is not None
                    and link.result_id == selected_result.result_id
                ),
                key=lambda item: (
                    item.artifact_order,
                    item.asset_id,
                ),
            )
            assets_by_id = {
                asset.asset_id: asset for asset in page.assets
            }
            if len(assets_by_id) != len(page.assets):
                raise ApplicationInternalError(task_id=task.task_id)
            artifacts: list[ResultArtifactProjection] = []
            for link in links:
                asset = assets_by_id.get(link.asset_id)
                if (
                    selected_run is None
                    or asset is None
                    or asset.actor_id != task.actor_id
                    or asset.task_id != task.task_id
                    or asset.producer_tool_run_id
                    != selected_run.tool_run_id
                    or asset.current_status != "AVAILABLE"
                    or asset.media_type is None
                    or asset.width is None
                    or asset.height is None
                    or asset.bit_depth is None
                    or asset.size_bytes is None
                    or asset.sha256 is None
                ):
                    raise ApplicationInternalError(task_id=task.task_id)
                artifacts.append(
                    ResultArtifactProjection(
                        asset_id=asset.asset_id,
                        status=asset.current_status,
                        role=asset.role,
                        media_type=asset.media_type,
                        width=asset.width,
                        height=asset.height,
                        bit_depth=asset.bit_depth,
                        size_bytes=asset.size_bytes,
                        sha256=asset.sha256,
                        content_url=(
                            f"/api/v1/assets/{asset.asset_id}/content"
                        ),
                    )
                )
            explanations = [
                explanation
                for explanation in page.explanations
                if selected_result is not None
                and explanation.result_id == selected_result.result_id
            ]
            calls_by_id = {
                call.llm_call_id: call for call in page.llm_calls
            }
            if len(calls_by_id) != len(page.llm_calls):
                raise ApplicationInternalError(task_id=task.task_id)
            for explanation in explanations:
                call = calls_by_id.get(explanation.llm_call_id)
                if (
                    selected_result is None
                    or explanation.task_id != task.task_id
                    or call is None
                    or call.task_id != task.task_id
                    or call.conversation_id != conversation_id
                    or call.purpose != "TOOL_RESULT_EXPLANATION"
                    or call.input_result_id != selected_result.result_id
                ):
                    raise ApplicationInternalError(task_id=task.task_id)
            explanation_selection = select_explanation_attempts(
                explanations
            )
            task_revisions = [
                revision
                for revision in page.revisions
                if revision.task_id == task.task_id
            ]
            errors: list[SafeTimelineError] = []
            if initial is None:
                errors.append(
                    SafeTimelineError(
                        code="CONTEXT_UNAVAILABLE",
                        message="任务输入上下文不可用。",
                    )
                )
            if task.error_code is not None:
                errors.append(
                    SafeTimelineError(
                        code=task.error_code,
                        message=(
                            task.safe_error_message
                            or "任务处理失败。"
                        ),
                    )
                )
            result.append(
                ToolTaskTimelineItem(
                    item_type="TOOL_TASK",
                    item_id=key.item_id,
                    task_id=key.task_id,
                    anchor_at=key.anchor_at,
                    initial_user_message=(
                        None
                        if initial is None
                        else _public_message(initial)
                    ),
                    task=_public_task(task),
                    input_thread=tuple(
                        _public_message(message)
                        for message in selected_thread
                    ),
                    input_thread_count=len(remaining),
                    input_thread_truncated=(
                        len(remaining) > _INPUT_THREAD_LIMIT
                    ),
                    tool_runs=PublicToolRunCollection(
                        attempt_count=len(task_runs),
                        selected_tool_run=(
                            None
                            if selected_run is None
                            else project_tool_run(
                                selected_run,
                                is_selected=True,
                            )
                        ),
                        has_history=len(task_runs) > 1,
                    ),
                    result=(
                        None
                        if selected_result is None
                        else _public_result(selected_result)
                    ),
                    assets=tuple(artifacts),
                    explanation=_public_explanation(
                        explanation_selection.primary
                    ),
                    latest_explanation_failure=_public_explanation(
                        explanation_selection.latest_failed
                    ),
                    needs_input=_needs_input(task, task_revisions),
                    errors=tuple(errors),
                )
            )
        return tuple(result)
