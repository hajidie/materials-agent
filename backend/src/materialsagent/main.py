from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from time import perf_counter
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from materialsagent.api.routes.conversations import (
    router as conversations_router,
)
from materialsagent.api.routes.assets import router as assets_router
from materialsagent.api.routes.health import router as health_router
from materialsagent.api.routes.tools import router as tools_router
from materialsagent.api.routes.tool_results import (
    router as tool_results_router,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.conversation_cleanup import ConversationCleanupService
from materialsagent.application.asset_service import AssetService
from materialsagent.application.bootstrap import ensure_object_storage_bucket
from materialsagent.application.conversations import ConversationService
from materialsagent.application.errors import ApplicationError, ApplicationInternalError
from materialsagent.application.readiness import build_readiness_service
from materialsagent.application.result_service import (
    ResultService,
    ToolResultQueryService,
)
from materialsagent.application.tool_execution import (
    ToolExecutionService,
    ToolRunQueryService,
)
from materialsagent.application.tools import (
    ToolCatalogService,
    build_tool_registry,
)
from materialsagent.application.tool_invocations import (
    ExecutorRouter,
    InvocationService,
    ManagedExecutor,
    StandardSyncExecutor,
)
from materialsagent.application.fake_side_effect_tool import (
    FAKE_SIDE_EFFECT_PERMISSION,
)
from materialsagent.domain.ports.tool_authorization import (
    EmptyPermissionAuthorizationService,
    ExactPermissionAuthorizationService,
)
from materialsagent.domain.ports.tool_registry import ToolExecutionProfile
from materialsagent.domain.ports.storage import StoredObjectMetadata
from materialsagent.infrastructure.config import (
    AppSettings,
    ConfigurationError,
    load_settings,
    parse_minio_config,
    parse_zta35g_runtime_config,
)
from materialsagent.infrastructure.db.session import (
    create_engine_from_settings,
    create_session_factory,
)
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.logging import configure_logging
from materialsagent.infrastructure.tool_clients.local_zta35g import (
    LocalZTA35GToolClientAdapter,
)


Clock = Callable[[], datetime]


class _LazyConfiguredStorage:
    """Create and bootstrap the configured MinIO adapter on first I/O only."""

    def __init__(self, settings: AppSettings) -> None:
        parse_minio_config(settings)
        self._settings = settings
        self._storage = None
        self._lock = Lock()

    def _resolved(self):
        if self._storage is not None:
            return self._storage
        with self._lock:
            if self._storage is None:
                from materialsagent.infrastructure.storage.minio import (
                    create_minio_storage,
                )

                storage = create_minio_storage(self._settings)
                try:
                    ensure_object_storage_bucket(storage)
                except Exception:
                    storage.close()
                    raise
                self._storage = storage
        return self._storage

    def put(
        self,
        object_key: str,
        payload: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObjectMetadata:
        return self._resolved().put(
            object_key,
            payload,
            content_type,
            metadata,
        )

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        return self._resolved().head(object_key)

    def get(self, object_key: str, *, max_bytes: int) -> bytes:
        return self._resolved().get(object_key, max_bytes=max_bytes)

    def delete(self, object_key: str) -> None:
        self._resolved().delete(object_key)

    def close(self) -> None:
        if self._storage is not None:
            self._storage.close()


def _resource_projection(
    request: Request,
    error: ApplicationError | None = None,
) -> dict[str, str | None]:
    return {
        "conversation_id": (
            error.conversation_id
            if error is not None and error.conversation_id is not None
            else request.path_params.get("conversation_id")
        ),
        "task_id": (
            error.task_id
            if error is not None and error.task_id is not None
            else request.path_params.get("task_id")
        ),
        "tool_run_id": (
            getattr(error, "tool_run_id", None)
            if error is not None
            else request.path_params.get("tool_run_id")
        ),
        "result_id": request.path_params.get("result_id"),
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
    application_error: ApplicationError | None = None,
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
            "resource": _resource_projection(request, application_error),
        },
    )


async def _application_error_handler(
    request: Request,
    error: ApplicationError,
) -> JSONResponse:
    message = str(error)
    if isinstance(error, ApplicationInternalError):
        message = ApplicationInternalError.default_message
    return _error_response(
        request,
        status_code=error.status_code,
        code=error.code,
        message=message,
        details=error.details,
        application_error=error,
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
    *, settings=None, readiness_service=None, unit_of_work_factory=None,
    actor_context=None, clock=None, id_factory=None, conversation_service=None,
    tool_registry=None,
    tool_catalog_service=None, tool_execution_service=None, tool_run_query_service=None,
    storage_service=None, asset_service=None, tool_result_query_service=None,
    result_service=None, conversation_cleanup_service=None, invocation_service=None,
    agent_store=None, agent_model=None, agent_runtime=None,
) -> FastAPI:
    from materialsagent.application.agent_runtime import AgentRuntime
    from materialsagent.application.agent_tools import RegistryAgentGateway, ManagedToolWorkflow
    from materialsagent.domain.models.agent import RunBudget
    from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore
    from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter, MockAgentModel

    resolved_settings = settings or load_settings()
    resolved_readiness_service = readiness_service or build_readiness_service(resolved_settings)
    owned_engine = None
    owned_storage = None
    resolved_unit_of_work_factory = unit_of_work_factory
    session_factory = None
    if resolved_unit_of_work_factory is None:
        try:
            owned_engine = create_engine_from_settings(resolved_settings)
            session_factory = create_session_factory(owned_engine)
            resolved_unit_of_work_factory = lambda: SQLAlchemyUnitOfWork(session_factory)
        except ConfigurationError:
            pass
    resolved_actor_context = actor_context
    if resolved_actor_context is None and resolved_settings.local_actor_id:
        resolved_actor_context = ActorContext(actor_id=resolved_settings.local_actor_id, user_id=None)
    resolved_storage_service = storage_service
    if resolved_storage_service is None:
        try:
            owned_storage = _LazyConfiguredStorage(resolved_settings)
            resolved_storage_service = owned_storage
        except ConfigurationError:
            pass
    runtime_config = parse_zta35g_runtime_config(resolved_settings)
    runtime_client = None
    if runtime_config:
        runtime_client = LocalZTA35GToolClientAdapter(base_url=runtime_config.base_url,
            token=runtime_config.token.get_secret_value(), timeout_seconds=runtime_config.timeout_seconds)
    resolved_tool_registry = tool_registry or build_tool_registry(runtime_client,
        enable_dev_fake_side_effect_tool=resolved_settings.enable_dev_fake_side_effect_tool)
    if resolved_settings.app_env == "production" and any(
        r.execution_profile is ToolExecutionProfile.SIDE_EFFECT for r in resolved_tool_registry.list_registered()
    ):
        raise ConfigurationError("Side-effect Tools are forbidden in the production Catalog.")
    resolved_tool_catalog_service = tool_catalog_service or ToolCatalogService(resolved_tool_registry)
    resolved_conversation_service = conversation_service
    resolved_conversation_cleanup_service = conversation_cleanup_service
    resolved_tool_execution_service = tool_execution_service
    resolved_tool_run_query_service = tool_run_query_service
    resolved_asset_service = asset_service
    resolved_tool_result_query_service = tool_result_query_service
    resolved_result_service = result_service
    resolved_invocation_service = invocation_service
    resolved_tool_workflow_service = None
    process_cutoff = clock() if clock else datetime.now(timezone.utc)
    if resolved_unit_of_work_factory:
        factory = resolved_unit_of_work_factory
        resolved_conversation_service = resolved_conversation_service or ConversationService(factory, clock=clock, id_factory=id_factory)
        resolved_tool_execution_service = resolved_tool_execution_service or ToolExecutionService(factory, resolved_tool_registry, clock=clock)
        resolved_tool_run_query_service = resolved_tool_run_query_service or ToolRunQueryService(factory)
        resolved_tool_result_query_service = resolved_tool_result_query_service or ToolResultQueryService(factory)
        resolved_result_service = resolved_result_service or ResultService(factory, clock=clock)
        if resolved_storage_service:
            try:
                storage_config = parse_minio_config(resolved_settings)
            except ConfigurationError:
                storage_config = None
            if storage_config:
                namespace = f"minio+{'https' if storage_config.secure else 'http'}://{storage_config.endpoint}"
                resolved_asset_service = resolved_asset_service or AssetService(factory, resolved_storage_service,
                    environment=resolved_settings.app_env, bucket=storage_config.bucket, storage_namespace=namespace, clock=clock)
                resolved_conversation_cleanup_service = resolved_conversation_cleanup_service or ConversationCleanupService(
                    factory, resolved_storage_service, environment=resolved_settings.app_env, bucket=storage_config.bucket,
                    storage_namespace=namespace, process_cutoff=process_cutoff, clock=clock, id_factory=id_factory)
        if resolved_asset_service:
            resolved_tool_workflow_service = ManagedToolWorkflow(factory, resolved_tool_execution_service,
                resolved_asset_service, resolved_result_service)
        authorization = ExactPermissionAuthorizationService((FAKE_SIDE_EFFECT_PERMISSION,)) if resolved_settings.enable_dev_fake_side_effect_tool else EmptyPermissionAuthorizationService()
        resolved_invocation_service = resolved_invocation_service or InvocationService(factory, resolved_tool_registry,
            ExecutorRouter((StandardSyncExecutor(), ManagedExecutor())), authorization=authorization,
            clock=clock, id_factory=id_factory, managed_workflow_service=resolved_tool_workflow_service,
            lease_seconds=max(60, int(resolved_settings.zta35g_runtime_timeout_seconds) + 60))
    resolved_agent_store = agent_store or (SQLAlchemyAgentStore(session_factory) if session_factory else None)
    resolved_agent_model = agent_model
    if resolved_agent_model is None:
        if resolved_settings.llm_adapter == "mock":
            resolved_agent_model = MockAgentModel()
        else:
            from materialsagent.infrastructure.llm.configuration import load_llm_configuration
            configuration = load_llm_configuration(resolved_settings)
            resolved_agent_model = AgentModelAdapter({role: configuration.for_role(role) for role in (
                "agent_decision", "tool_arg_resolution", "final_answer")})
    resolved_agent_runtime = agent_runtime
    if resolved_agent_runtime is None and resolved_agent_store and resolved_invocation_service:
        gateway = RegistryAgentGateway(resolved_tool_registry, resolved_unit_of_work_factory,
            resolved_invocation_service, resolved_tool_workflow_service, resolved_tool_result_query_service)
        resolved_agent_runtime = AgentRuntime(resolved_agent_store, resolved_agent_model, gateway, **({"clock": clock} if clock else {}))
    agent_budget = RunBudget(max_action_steps=resolved_settings.agent_max_action_steps,
        max_tool_executions=resolved_settings.agent_max_tool_executions,
        max_active_seconds=resolved_settings.agent_max_active_seconds, max_llm_tokens=resolved_settings.agent_max_llm_tokens,
        standard_timeout_seconds=resolved_settings.agent_standard_timeout_seconds,
        managed_timeout_seconds=resolved_settings.zta35g_runtime_timeout_seconds)

    request_logger = configure_logging(resolved_settings.log_level)
    request_logger.disabled = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if resolved_agent_runtime is not None and hasattr(resolved_agent_runtime.store, "recover_interrupted"):
                resolved_agent_runtime.store.recover_interrupted(resolved_agent_runtime.process_id, repair=resolved_agent_runtime.tools.repair)
            if (
                resolved_conversation_cleanup_service is not None
                and resolved_actor_context is not None
            ):
                try:
                    resolved_conversation_cleanup_service.recover_stale(
                        resolved_actor_context
                    )
                except ApplicationError:
                    request_logger.warning(
                        "process_recovery status=database_unavailable"
                    )
                resolved_conversation_cleanup_service.drain(limit=100)
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()
            if owned_storage is not None:
                owned_storage.close()

    app = FastAPI(
        title="Materials Agent Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.agent_runtime = resolved_agent_runtime
    app.state.agent_budget = agent_budget
    app.state.readiness_service = resolved_readiness_service
    app.state.actor_context = resolved_actor_context
    app.state.conversation_service = resolved_conversation_service
    app.state.conversation_cleanup_service = resolved_conversation_cleanup_service
    app.state.tool_catalog_service = resolved_tool_catalog_service
    app.state.tool_execution_service = resolved_tool_execution_service
    app.state.tool_run_query_service = resolved_tool_run_query_service
    app.state.asset_service = resolved_asset_service
    app.state.tool_result_query_service = resolved_tool_result_query_service
    app.state.tool_workflow_service = resolved_tool_workflow_service
    app.state.invocation_service = resolved_invocation_service
    app.state.m5_dev_routes_enabled = resolved_settings.m5_dev_routes_enabled

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
    app.include_router(assets_router)
    app.include_router(conversations_router)
    app.include_router(tools_router)
    app.include_router(tool_results_router)
    from materialsagent.api.routes.agent_runs import router as agent_runs_router
    app.include_router(agent_runs_router)
    app.add_exception_handler(ApplicationError, _application_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        _request_validation_error_handler,
    )
    app.add_exception_handler(Exception, _internal_error_handler)
    from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure
    async def agent_error(request, error):
        code = error.code if isinstance(error, AgentFailure) else "AGENT_CONFLICT"
        status_code = 404 if code.endswith("NOT_FOUND") else 409
        return JSONResponse(status_code=status_code, content={"request_id": request.state.request_id,
            "error": {"code": code, "message": "当前操作无法继续，请刷新运行状态后重试。", "details": []}})
    app.add_exception_handler(AgentConflictError, agent_error)
    app.add_exception_handler(AgentFailure, agent_error)
    return app
