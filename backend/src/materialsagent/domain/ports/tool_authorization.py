from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from materialsagent.domain.ports.tool_registry import ToolRef


@dataclass(frozen=True, slots=True)
class ToolAuthorizationRequest:
    actor_id: str
    user_id: str | None
    tool_ref: ToolRef
    required_permissions: tuple[str, ...]
    action: str


@dataclass(frozen=True, slots=True)
class ToolAuthorizationDecision:
    allowed: bool
    reason_code: str


class ToolAuthorizationPort(Protocol):
    def authorize(
        self,
        request: ToolAuthorizationRequest,
    ) -> ToolAuthorizationDecision: ...


class EmptyPermissionAuthorizationService:
    """Single-user default: only Tools requiring no permissions are allowed."""

    def authorize(
        self,
        request: ToolAuthorizationRequest,
    ) -> ToolAuthorizationDecision:
        if request.required_permissions:
            return ToolAuthorizationDecision(False, "PERMISSION_UNVERIFIABLE")
        return ToolAuthorizationDecision(True, "NO_PERMISSION_REQUIRED")


class ExactPermissionAuthorizationService:
    """Explicit test/dev-only authorizer with an exact allowlist."""

    def __init__(self, allowed_permissions: tuple[str, ...]) -> None:
        self._allowed = frozenset(allowed_permissions)

    def authorize(
        self,
        request: ToolAuthorizationRequest,
    ) -> ToolAuthorizationDecision:
        required = frozenset(request.required_permissions)
        if required <= self._allowed:
            return ToolAuthorizationDecision(True, "DEV_EXACT_PERMISSION_GRANT")
        return ToolAuthorizationDecision(False, "PERMISSION_UNVERIFIABLE")
