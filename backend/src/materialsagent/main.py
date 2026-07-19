from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from materialsagent.api.routes.conversations import (
    router as conversations_router,
)
from materialsagent.api.routes.health import router as health_router
from materialsagent.api.routes.tasks import router as tasks_router
from materialsagent.application.context import ActorContext
from materialsagent.application.conversations import (
    ConversationService,
    IdFactory,
)
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationError,
    ApplicationInternalError,
    ApplicationValidationError,
    DependencyUnavailableError,
    InvalidCursorError,
    ResourceNotFoundError,
)
from materialsagent.application.messages import (
    MessageSubmissionService,
    TitleGenerator,
)
from materialsagent.application.readiness import (
    ReadinessService,
    build_readiness_service,
)
from materialsagent.application.tasks import TaskQueryService
from materialsagent.domain.ports.unit_of_work import UnitOfWorkFactory
from materialsagent.infrastructure.config import (
    AppSettings,
    ConfigurationError,
    load_settings,
)
from materialsagent.infrastructure.db.session import (
    create_engine_from_settings,
    create_session_factory,
)
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.logging import configure_logging


Clock = Callable[[], datetime]


def _resource_projection(request: Request) -> dict[str, str | None]:
    return {
        "conversation_id": request.path_params.get("conversation_id"),
        "task_id": request.path_params.get("task_id"),
        "tool_run_id": None,
        "result_id": None,
    }


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid4().hex}")


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "request_id": _request_id(request),
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
            },
            "resource": _resource_projection(request),
        },
    )


async def _application_error_handler(
    request: Request,
    error: ApplicationError,
) -> JSONResponse:
    if isinstance(error, (InvalidCursorError, ApplicationValidationError)):
        return _error_response(
            request,
            status_code=422,
            code="VALIDATION_FAILED",
            message=str(error),
        )
    if isinstance(error, ResourceNotFoundError):
        return _error_response(
            request,
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message=str(error),
        )
    if isinstance(error, ApplicationConflictError):
        return _error_response(
            request,
            status_code=409,
            code="RESOURCE_CONFLICT",
            message=str(error),
        )
    if isinstance(error, DependencyUnavailableError):
        return _error_response(
            request,
            status_code=503,
            code="DEPENDENCY_UNAVAILABLE",
            message=str(error),
        )
    return _error_response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message=ApplicationInternalError.default_message,
    )


async def _request_validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    errors = error.errors()
    if any(item.get("type") == "json_invalid" for item in errors):
        return _error_response(
            request,
            status_code=400,
            code="INVALID_REQUEST_BODY",
            message="请求正文不是有效的 JSON。",
        )
    details: list[dict[str, str]] = []
    for item in errors[:20]:
        location = item.get("loc", ())
        public_parts = [
            str(part)
            for part in location
            if part not in {"body", "query", "path"}
        ]
        field = (
            "request"
            if {"actor_id", "user_id"}.intersection(public_parts)
            else ".".join(public_parts) or "request"
        )
        details.append(
            {
                "field": field,
                "code": str(item.get("type", "invalid"))[:128],
                "message": "字段值无效。",
            }
        )
    return _error_response(
        request,
        status_code=422,
        code="VALIDATION_FAILED",
        message="请求字段验证失败。",
        details=details,
    )


async def _internal_error_handler(
    request: Request,
    _error: Exception,
) -> JSONResponse:
    return _error_response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message=ApplicationInternalError.default_message,
    )


def create_app(
    *,
    settings: AppSettings | None = None,
    readiness_service: ReadinessService | None = None,
    unit_of_work_factory: UnitOfWorkFactory | None = None,
    actor_context: ActorContext | None = None,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    title_generator: TitleGenerator | None = None,
    conversation_service: ConversationService | None = None,
    message_submission_service: MessageSubmissionService | None = None,
    task_query_service: TaskQueryService | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_readiness_service = readiness_service or build_readiness_service(
        resolved_settings
    )
    owned_engine: Engine | None = None
    resolved_unit_of_work_factory = unit_of_work_factory
    if resolved_unit_of_work_factory is None:
        try:
            owned_engine = create_engine_from_settings(resolved_settings)
            session_factory = create_session_factory(owned_engine)
            resolved_unit_of_work_factory = lambda: SQLAlchemyUnitOfWork(
                session_factory
            )
        except ConfigurationError:
            owned_engine = None

    resolved_actor_context = actor_context
    if resolved_actor_context is None:
        try:
            resolved_actor_context = ActorContext(
                actor_id=resolved_settings.local_actor_id,
                user_id=None,
            )
        except (TypeError, ValueError):
            resolved_actor_context = None

    resolved_conversation_service = conversation_service
    resolved_message_submission_service = message_submission_service
    resolved_task_query_service = task_query_service
    if resolved_unit_of_work_factory is not None:
        if resolved_conversation_service is None:
            resolved_conversation_service = ConversationService(
                resolved_unit_of_work_factory,
                clock=clock,
                id_factory=id_factory,
            )
        if resolved_message_submission_service is None:
            resolved_message_submission_service = MessageSubmissionService(
                resolved_unit_of_work_factory,
                clock=clock,
                id_factory=id_factory,
                title_generator=title_generator,
            )
        if resolved_task_query_service is None:
            resolved_task_query_service = TaskQueryService(
                resolved_unit_of_work_factory
            )

    request_logger = configure_logging(resolved_settings.log_level)
    request_logger.disabled = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(
        title="Materials Agent Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.readiness_service = resolved_readiness_service
    app.state.actor_context = resolved_actor_context
    app.state.conversation_service = resolved_conversation_service
    app.state.message_submission_service = resolved_message_submission_service
    app.state.task_query_service = resolved_task_query_service

    @app.middleware("http")
    async def add_request_context(
        request: Request,
        call_next,
    ) -> Response:
        request_id = f"req_{uuid4().hex}"
        request.state.request_id = request_id
        started_at = perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
            request_logger.info(
                "http_request_completed",
                extra={
                    "event": "http_request_completed",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )

    app.include_router(health_router)
    app.include_router(conversations_router)
    app.include_router(tasks_router)
    app.add_exception_handler(ApplicationError, _application_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        _request_validation_error_handler,
    )
    app.add_exception_handler(Exception, _internal_error_handler)
    return app
