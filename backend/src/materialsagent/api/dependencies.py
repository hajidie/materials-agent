from __future__ import annotations

from fastapi import Request

from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import ConversationService
from materialsagent.application.errors import DependencyUnavailableError
from materialsagent.application.messages import MessageSubmissionService
from materialsagent.application.tasks import TaskQueryService


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


def get_task_query_service(request: Request) -> TaskQueryService:
    return _required_app_state(request, "task_query_service")
