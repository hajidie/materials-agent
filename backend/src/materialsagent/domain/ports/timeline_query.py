from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.explanation import (
    NaturalLanguageExplanation,
)
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun


TimelineItemType = Literal[
    "USER_MESSAGE",
    "ASSISTANT_MESSAGE",
    "TOOL_TASK",
    "TOOL_INVOCATION",
]


@dataclass(frozen=True, slots=True)
class TimelinePosition:
    conversation_id: str
    anchor_at: datetime
    item_type_rank: int
    item_id: str


@dataclass(frozen=True, slots=True)
class TimelineKey:
    item_type: TimelineItemType
    item_id: str
    task_id: str | None
    anchor_at: datetime
    item_type_rank: int


@dataclass(frozen=True, slots=True)
class TimelineQueryPage:
    conversation: Conversation
    keys: tuple[TimelineKey, ...]
    has_more: bool
    messages: tuple[Message, ...] = ()
    tasks: tuple[Task, ...] = ()
    revisions: tuple[TaskInputRevision, ...] = ()
    tool_runs: tuple[ToolRun, ...] = ()
    results: tuple[ToolResult, ...] = ()
    result_asset_links: tuple[ResultAssetLink, ...] = ()
    assets: tuple[Asset, ...] = ()
    explanations: tuple[NaturalLanguageExplanation, ...] = ()
    llm_calls: tuple[LLMCall, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskDetailQuerySnapshot:
    task: Task
    messages: tuple[Message, ...] = ()
    revisions: tuple[TaskInputRevision, ...] = ()
    tool_runs: tuple[ToolRun, ...] = ()
    selected_result: ToolResult | None = None
    result_asset_links: tuple[ResultAssetLink, ...] = ()
    assets: tuple[Asset, ...] = ()
    explanations: tuple[NaturalLanguageExplanation, ...] = ()
    llm_calls: tuple[LLMCall, ...] = ()


class TimelineQueryPort(Protocol):
    def fetch_owned_page(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        after: TimelinePosition | None,
        limit: int,
    ) -> TimelineQueryPage | None: ...


class TaskDetailQueryPort(Protocol):
    def fetch_owned_task_detail(
        self,
        *,
        actor_id: str,
        task_id: str,
    ) -> TaskDetailQuerySnapshot | None: ...
