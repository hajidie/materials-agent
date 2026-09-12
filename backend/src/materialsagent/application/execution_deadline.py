"""Per-request external-call deadline, propagated without mutable shared clients."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline: ContextVar[float | None] = ContextVar("tool_execution_deadline", default=None)


@contextmanager
def tool_deadline(seconds: float):
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_timeout(default: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Tool execution deadline exceeded.")
    return min(default, remaining)


_execution_owner: ContextVar[tuple[str, int, str] | None] = ContextVar("agent_execution_owner", default=None)

@contextmanager
def execution_owner(run_id: str, version: int, claim: str):
    token = _execution_owner.set((run_id, version, claim))
    try:
        yield
    finally:
        _execution_owner.reset(token)

def current_execution_owner():
    return _execution_owner.get()
