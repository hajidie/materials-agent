from __future__ import annotations

from fastapi import Request

from materialsagent.application.chat_orchestration import (
    ChatOrchestrationService,
)
from materialsagent.application.asset_service import AssetService
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.errors import (
    DependencyUnavailableError,
    ResourceNotFoundError,
)
from materialsagent.application.messages import MessageSubmissionService
from materialsagent.application.tasks import TaskQueryService
from materialsagent.application.timeline import TimelineQueryService
from materialsagent.application.retries import (
    ExplanationRetryService,
    ToolRetryService,
)
from materialsagent.application.result_service import ToolResultQueryService
from materialsagent.application.tool_workflow import ToolWorkflowService
from materialsagent.application.tool_execution import (
    ToolExecutionService,
    ToolRunQueryService,
)
from materialsagent.application.tools import ToolCatalogService


def _required_app_state(request: Request, name: str):
    value = getattr(request.app.state, name, None)
    if value is None:
        raise DependencyUnavailableError()
    return value


def get_actor_context(request: Request) -> ActorContext:
    return _required_app_state(request, "actor_context")


def get_conversation_service(request: Request) -> ConversationService:
    return _required_app_state(request, "conversation_service")


def get_message_submission_service(
    request: Request,
) -> MessageSubmissionService:
    return _required_app_state(request, "message_submission_service")


def get_chat_orchestration_service(
    request: Request,
) -> ChatOrchestrationService:
    return _required_app_state(request, "chat_orchestration_service")


def get_task_query_service(request: Request) -> TaskQueryService:
    return _required_app_state(request, "task_query_service")


def get_timeline_query_service(request: Request) -> TimelineQueryService:
    return _required_app_state(request, "timeline_query_service")


def get_tool_retry_service(request: Request) -> ToolRetryService:
    return _required_app_state(request, "tool_retry_service")


def get_explanation_retry_service(
    request: Request,
) -> ExplanationRetryService:
    return _required_app_state(request, "explanation_retry_service")


def get_tool_catalog_service(request: Request) -> ToolCatalogService:
    return _required_app_state(request, "tool_catalog_service")


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


def get_optional_tool_workflow_service(
    request: Request,
) -> ToolWorkflowService | None:
    return getattr(request.app.state, "tool_workflow_service", None)


def require_m5_dev_routes(request: Request) -> None:
    if getattr(request.app.state, "m5_dev_routes_enabled", False) is not True:
        raise ResourceNotFoundError()
