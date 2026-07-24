from __future__ import annotations

from typing import Protocol, Self

from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.domain.models.idempotency_record import IdempotencyRecord
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun


class PersistenceError(RuntimeError):
    """Safe persistence failure without driver or SQL details."""


class PersistenceConflictError(PersistenceError):
    """A stable persistence uniqueness or concurrency conflict."""


class DatabaseUnavailableError(PersistenceError):
    """The configured database could not be reached or authenticated."""


class ActorRepository(Protocol):
    def get(self, actor_id: str) -> Actor | None: ...

    def get_for_update(self, actor_id: str) -> Actor | None: ...

    def add(self, actor: Actor) -> None: ...


class ConversationRepository(Protocol):
    def get(self, conversation_id: str) -> Conversation | None: ...

    def get_owned(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Conversation | None: ...

    def list_owned(self, actor_id: str) -> list[Conversation]: ...

    def add(self, conversation: Conversation) -> None: ...

    def update(self, conversation: Conversation) -> Conversation | None: ...


class MessageRepository(Protocol):
    def get(self, message_id: str) -> Message | None: ...

    def get_by_llm_call_id(self, llm_call_id: str) -> Message | None: ...

    def get_latest_for_conversation(
        self,
        conversation_id: str,
        actor_id: str,
    ) -> Message | None: ...

    def list_for_task(self, task_id: str) -> list[Message]: ...

    def add(self, message: Message) -> None: ...


class TaskRepository(Protocol):
    def get(self, task_id: str) -> Task | None: ...

    def get_owned(self, task_id: str, actor_id: str) -> Task | None: ...

    def get_owned_for_update(
        self,
        task_id: str,
        actor_id: str,
    ) -> Task | None: ...

    def add(self, task: Task) -> None: ...

    def update(
        self,
        task: Task,
        *,
        expected_status: str,
    ) -> Task | None: ...


class TaskInputRevisionRepository(Protocol):
    def get(
        self,
        task_input_revision_id: str,
    ) -> TaskInputRevision | None: ...

    def add(self, revision: TaskInputRevision) -> None: ...

    def list_for_task(self, task_id: str) -> list[TaskInputRevision]: ...

    def list_for_llm_call_id(
        self,
        llm_call_id: str,
    ) -> list[TaskInputRevision]: ...


class LLMCallRepository(Protocol):
    def get(self, llm_call_id: str) -> LLMCall | None: ...

    def add(self, call: LLMCall) -> None: ...

    def list_for_task(
        self,
        task_id: str,
        *,
        request_id: str | None = None,
    ) -> list[LLMCall]: ...

    def update(
        self,
        call: LLMCall,
        *,
        expected_status: str,
    ) -> LLMCall | None: ...


class ToolRunRepository(Protocol):
    def get(self, tool_run_id: str) -> ToolRun | None: ...

    def get_owned(self, tool_run_id: str, actor_id: str) -> ToolRun | None: ...

    def get_owned_for_update(
        self,
        tool_run_id: str,
        actor_id: str,
    ) -> ToolRun | None: ...

    def list_for_task(self, task_id: str) -> list[ToolRun]: ...

    def add(self, tool_run: ToolRun) -> None: ...

    def update(
        self,
        tool_run: ToolRun,
        *,
        expected_status: str,
    ) -> ToolRun | None: ...


class AssetRepository(Protocol):
    def get(self, asset_id: str) -> Asset | None: ...

    def get_owned(self, asset_id: str, actor_id: str) -> Asset | None: ...

    def get_for_update(self, asset_id: str) -> Asset | None: ...

    def list_for_tool_run(self, tool_run_id: str) -> list[Asset]: ...

    def add(self, asset: Asset) -> None: ...

    def update(
        self,
        asset: Asset,
        *,
        expected_status: str,
    ) -> Asset | None: ...


class ToolResultRepository(Protocol):
    def get(self, result_id: str) -> ToolResult | None: ...

    def get_owned(self, result_id: str, actor_id: str) -> ToolResult | None: ...

    def get_owned_for_update(
        self,
        result_id: str,
        actor_id: str,
    ) -> ToolResult | None: ...

    def get_for_tool_run(self, tool_run_id: str) -> ToolResult | None: ...

    def add(self, result: ToolResult) -> None: ...


class ResultAssetLinkRepository(Protocol):
    def list_for_result(self, result_id: str) -> list[ResultAssetLink]: ...

    def add(self, link: ResultAssetLink) -> None: ...


class ExplanationRepository(Protocol):
    def get(
        self,
        explanation_id: str,
    ) -> NaturalLanguageExplanation | None: ...

    def get_for_result_attempt(
        self,
        result_id: str,
        attempt_no: int,
    ) -> NaturalLanguageExplanation | None: ...

    def list_for_result(
        self,
        result_id: str,
    ) -> list[NaturalLanguageExplanation]: ...

    def add(self, explanation: NaturalLanguageExplanation) -> None: ...

    def update(
        self,
        explanation: NaturalLanguageExplanation,
        *,
        expected_status: str,
    ) -> NaturalLanguageExplanation | None: ...


class IdempotencyRecordRepository(Protocol):
    def get_by_scope(
        self,
        actor_id: str,
        operation: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None: ...

    def get_by_first_request_id(
        self,
        first_request_id: str,
    ) -> IdempotencyRecord | None: ...

    def get_unbound_supplement_for_task(
        self,
        task_id: str,
    ) -> IdempotencyRecord | None: ...

    def add(self, record: IdempotencyRecord) -> None: ...

    def bind_task_input_revision(
        self,
        record: IdempotencyRecord,
        task_input_revision_id: str,
    ) -> IdempotencyRecord | None: ...


class UnitOfWork(Protocol):
    actors: ActorRepository
    conversations: ConversationRepository
    messages: MessageRepository
    tasks: TaskRepository
    task_input_revisions: TaskInputRevisionRepository
    llm_calls: LLMCallRepository
    tool_runs: ToolRunRepository
    assets: AssetRepository
    tool_results: ToolResultRepository
    result_asset_links: ResultAssetLinkRepository
    explanations: ExplanationRepository
    idempotency_records: IdempotencyRecordRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
