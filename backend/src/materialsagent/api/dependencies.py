from __future__ import annotations

from fastapi import Request

from materialsagent.application.asset_service import AssetService
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.conversation_cleanup import ConversationCleanupService
from materialsagent.application.errors import DependencyUnavailableError
from materialsagent.application.result_service import ToolResultQueryService
from materialsagent.application.tool_execution import (
    ToolExecutionService,
    ToolRunQueryService,
)
from materialsagent.application.tools import ToolCatalogService
from materialsagent.application.tool_invocations import InvocationService


def _required_app_state(request: Request, name: str):
    value = getattr(request.app.state, name, None)
    if value is None:
        raise DependencyUnavailableError()
    return value


def get_actor_context(request: Request) -> ActorContext:
    return _required_app_state(request, "actor_context")


def get_conversation_service(request: Request) -> ConversationService:
    return _required_app_state(request, "conversation_service")


def get_conversation_cleanup_service(request: Request) -> ConversationCleanupService:
    return _required_app_state(request, "conversation_cleanup_service")














def get_tool_catalog_service(request: Request) -> ToolCatalogService:
    return _required_app_state(request, "tool_catalog_service")


def get_invocation_service(request: Request) -> InvocationService:
    return _required_app_state(request, "invocation_service")


def get_tool_execution_service(request: Request) -> ToolExecutionService:
    return _required_app_state(request, "tool_execution_service")


def get_tool_run_query_service(request: Request) -> ToolRunQueryService:
    return _required_app_state(request, "tool_run_query_service")


def get_asset_service(request: Request) -> AssetService:
    return _required_app_state(request, "asset_service")


def get_tool_result_query_service(
    request: Request,
) -> ToolResultQueryService:
    return _required_app_state(request, "tool_result_query_service")
