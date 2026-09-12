from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from materialsagent.domain.models.agent import AgentRun, ArgumentDraft, ExecutionRecord, Observation, TokenUsage


class AgentConflictError(RuntimeError):
    pass


class AgentFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedAgentCall:
    role: str
    messages: list[dict[str, str]]
    output_limit: int
    input_estimate: int
    timeout: float


@dataclass(frozen=True)
class AgentModelResponse:
    value: Any
    usage: TokenUsage
    error_code: str | None = None


class AgentModelPort(Protocol):
    def prepare(self, role: str, payload: dict[str, Any], remaining_tokens: int, timeout: float) -> PreparedAgentCall: ...
    def invoke(self, request: PreparedAgentCall) -> AgentModelResponse: ...


class AgentStore(Protocol):
    def get(self, run_id: str, actor_id: str) -> AgentRun: ...
    def save(self, run: AgentRun) -> None:
        """CAS by run.version; increment version only after successful commit."""
        ...


class AgentToolGateway(Protocol):
    def catalog(self) -> list[dict[str, Any]]: ...
    def resolve(self, run: AgentRun, tool_name: str, arguments: dict[str, Any]) -> ArgumentDraft: ...
    def prepare(self, run: AgentRun, record: ExecutionRecord) -> ExecutionRecord: ...
    def execute(self, run: AgentRun, record: ExecutionRecord, timeout: float) -> Observation: ...
    def confirm(self, run: AgentRun, record: ExecutionRecord, approved: bool) -> None: ...
    def repair(self, run: AgentRun, record: ExecutionRecord) -> Observation | None: ...
