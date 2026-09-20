import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
import json
import secrets

from fastapi import FastAPI, Request, APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from .schemas import Strict, TrainingRequest, PredictionRequest, OperationPreparation, OperationLookup, resource_view
from .request_execution import resource_prediction
from sqlalchemy.exc import SQLAlchemyError

from materials_ml import EngineError
from .application import MLService
from .config import load_settings
from .domain import DatasetAsset, TrainingRun, ModelAsset, Prediction, ServiceError, MEMBERS, MAX_DATASET_BYTES
from .infrastructure.database import PostgresRepository
from .infrastructure.storage import MinioStorage


class Owner(Strict):
    scope_id: str
    session: str
    claim: str


class StartRequest(Owner):
    job_name: str


class StopRequest(Owner):
    reason: str


class ClaimRequest(Strict):
    session: str
    key: str


class RecoveryRequest(Strict):
    scope_id: str
    previous_claim: str
    new_session: str


def worker_view(run):
    if run is None:
        return None
    return {**resource_view(run), "session": run.worker_session, "claim": run.claim_id,
            "job_name": MLService.job_name(run), "frozen_spec": run.data["spec"],
            "process_stopped": run.process_stopped}


class DomainAuthentication:
    """One independently configured path and credential domain, with no fallback."""
    def __init__(self, app, prefix, token):
        self.app, self.prefix, self.expected = app, prefix, ("Bearer " + token).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            if not secrets.compare_digest(dict(scope["headers"]).get(b"authorization", b""), self.expected):
                return await JSONResponse({"error": {"code": "UNAUTHORIZED"}}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)


class Boundaries:
    """Enforce byte limits even without Content-Length, after authentication."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        maximum = 256 * 1024**2 if path.startswith("/internal/v1/") else MAX_DATASET_BYTES + 65536
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > maximum:
                raise ServiceError("REQUEST_TOO_LARGE", 413)
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except ServiceError as error:
            await JSONResponse({"error": {"code": error.code}}, error.status)(scope, receive, send)


def create_app(settings=None, service=None, *, maintenance=True):
    settings = settings or load_settings()
    owned_service = service is None
    service = service or MLService(PostgresRepository(settings.database_url.get_secret_value()), MinioStorage(settings), settings)

    @asynccontextmanager
    async def lifespan(app):
        service.start_predictions()
        stop = asyncio.Event()

        async def maintain():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(service.maintain)
                except (ServiceError, SQLAlchemyError):
                    pass  # Remains in durable records; health/ready reports dependency failure.
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

        try:
            async with AsyncExitStack() as stack:
                if settings.mcp_enabled:
                    await stack.enter_async_context(mcp_adapter.manager.run())
                task = asyncio.create_task(maintain()) if maintenance else None
                try:
                    yield
                finally:
                    stop.set()
                    if task:
                        await task
        finally:
            # Same thread owns the Windows named mutex from startup through shutdown.
            service.close_predictions()
            if owned_service:
                service.storage.close(); service.repo.close()

    app = FastAPI(title="Materials ML Resources", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    if settings.mcp_enabled:
        from .mcp_adapter import MCPAdapter
        mcp_adapter = MCPAdapter(service)
        app.router.add_route("/mcp", mcp_adapter, methods=["GET", "POST", "DELETE"])
        app.add_middleware(DomainAuthentication, prefix="/mcp", token=settings.mcp_token.get_secret_value())
    app.add_middleware(Boundaries)
    app.add_middleware(DomainAuthentication, prefix="/api/v1/", token=settings.resource_token.get_secret_value())
    app.add_middleware(DomainAuthentication, prefix="/internal/v1/", token=settings.worker_token.get_secret_value())

    def require_resource(request: Request):
        expected = ("Bearer " + settings.resource_token.get_secret_value()).encode("utf-8")
        if not secrets.compare_digest(request.headers.get("authorization", "").encode("utf-8"), expected):
            raise ServiceError("UNAUTHORIZED", 401)

    def require_worker(request: Request):
        expected = ("Bearer " + settings.worker_token.get_secret_value()).encode("utf-8")
        if not secrets.compare_digest(request.headers.get("authorization", "").encode("utf-8"), expected):
            raise ServiceError("UNAUTHORIZED", 401)

    resources = APIRouter(prefix="/api/v1/scopes/{scope_id}", dependencies=[Depends(require_resource)])
    internal = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_worker)])

    @app.exception_handler(ServiceError)
    async def service_error(_, error):
        return JSONResponse({"error": {"code": error.code}}, error.status)

    @app.exception_handler(EngineError)
    async def engine_error(_, error):
        return JSONResponse({"error": {"code": error.code}}, 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, error):
        return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, 422)

    from pydantic import ValidationError

    @app.exception_handler(ValidationError)
    async def contract_validation_error(_, error):
        return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_, error):
        return JSONResponse({"error": {"code": "DATABASE_UNAVAILABLE"}}, 503)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        with service.repo.transaction() as tx:
            tx.list(DatasetAsset, limit=1)
        try:
            if not service.storage.client.bucket_exists(settings.minio_bucket):
                raise ValueError()
        except Exception:
            raise ServiceError("STORAGE_UNAVAILABLE", 503) from None
        return {"status": "ready"}

    @resources.post("/operation-identities/prepare")
    def prepare_operation(scope_id: str, body: OperationPreparation):
        return service.prepare_operation(scope_id, body.operation, body.arguments)

    @resources.post("/operation-receipts/lookup")
    def lookup_operation(scope_id: str, body: OperationLookup):
        return service.lookup_operation(scope_id, body.operation, body.idempotency_key,
                                        body.request_digest, body.digest_version)

    @resources.post("/dataset-upload-identities/prepare")
    @resources.post("/datasets")
    async def upload(scope_id: str, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        try:
            async with request.form(max_files=1, max_fields=1, max_part_size=65536) as form:
                if set(form) - {"file", "metadata"} or "file" not in form:
                    raise ValueError()
                metadata = json.loads(form.get("metadata", "{}"))
                if not isinstance(metadata, dict) or set(metadata) - {"units", "display_name"}:
                    raise ValueError()
                payload = await form["file"].read(MAX_DATASET_BYTES + 1)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError("INVALID_UPLOAD", 422) from None
        if request.url.path.endswith("/prepare"):
            return await asyncio.to_thread(service.prepare_dataset_upload, scope_id, payload,
                metadata.get("units", {}), metadata.get("display_name"))
        resource = await asyncio.to_thread(service.upload_dataset, scope_id, key, payload,
                                           metadata.get("units", {}), metadata.get("display_name"),
                                           expected_digest=request.headers.get("X-ML-Expected-Request-Digest"))
        return JSONResponse(resource_view(resource), 410 if resource.status == "DELETED" else 202 if resource.status == "PENDING" else 200)

    class ScopeCloseRequest(Strict):
        operation_id: str

    @resources.get("/resource-identities/{kind}/{identity}")
    def resource_identity(scope_id: str, kind: str, identity: str, identity_contract_version: str = "ml-resource-identity-v1"):
        return service.resource_identity(scope_id, kind, identity, identity_contract_version)

    @resources.post("/close-operations")
    def close_scope(scope_id: str, body: ScopeCloseRequest):
        return service.close_scope(scope_id, body.operation_id)

    @resources.get("/close-operations/{operation_id}")
    def scope_close(scope_id: str, operation_id: str):
        return service.lookup_scope_close(scope_id, operation_id)

    @resources.get("/datasets")
    def datasets(scope_id: str, limit: int = 20, after: str | None = None):
        if not 1 <= limit <= 100:
            raise ServiceError("INVALID_PAGE", 422)
        items = service.list(DatasetAsset, scope_id, limit, after)
        return {"items": [resource_view(r) for r in items], "next_cursor": items[-1].id if len(items) == limit else None}

    @resources.get("/datasets/{identity}")
    def dataset(scope_id: str, identity: str):
        return resource_view(service.get(DatasetAsset, scope_id, identity))

    @resources.get("/datasets/{identity}/content")
    def dataset_content(scope_id: str, identity: str):
        return Response(service.dataset_content(scope_id, identity), media_type="text/csv")

    @resources.delete("/datasets/{identity}", status_code=202)
    def delete_dataset(scope_id: str, identity: str):
        return resource_view(service.delete_dataset(scope_id, identity))

    @resources.post("/training-runs", status_code=202)
    def submit(scope_id: str, body: TrainingRequest, request: Request):
        return resource_view(service.submit_training(scope_id, request.headers.get("Idempotency-Key", ""),
            body.dataset_id, body.model_dump(exclude={"dataset_id"})))

    @resources.get("/training-runs")
    def runs(scope_id: str, limit: int = 20, after: str | None = None):
        if not 1 <= limit <= 100:
            raise ServiceError("INVALID_PAGE", 422)
        items = service.list(TrainingRun, scope_id, limit, after)
        return {"items": [resource_view(r) for r in items], "next_cursor": items[-1].id if len(items) == limit else None}

    @resources.get("/training-runs/{identity}")
    def run(scope_id: str, identity: str):
        return resource_view(service.get(TrainingRun, scope_id, identity))

    @resources.post("/training-runs/{identity}/cancel")
    def cancel(scope_id: str, identity: str):
        return resource_view(service.cancel(scope_id, identity))

    @resources.get("/models/{identity}")
    def model(scope_id: str, identity: str):
        return resource_view(service.get(ModelAsset, scope_id, identity))

    @resources.post("/predictions")
    async def prediction_submit(scope_id: str, body: PredictionRequest, request: Request):
        result = await resource_prediction(service, scope_id, request.headers.get("Idempotency-Key", ""), body, request)
        return resource_view(result)

    @resources.get("/predictions")
    def predictions(scope_id: str, limit: int = 20, after: str | None = None):
        if not 1 <= limit <= 100:
            raise ServiceError("INVALID_PAGE", 422)
        items = service.list(Prediction, scope_id, limit, after)
        return {"items": [resource_view(r) for r in items], "next_cursor": items[-1].id if len(items) == limit else None}

    @resources.get("/predictions/{identity}")
    def prediction(scope_id: str, identity: str):
        return resource_view(service.get(Prediction, scope_id, identity))

    @resources.get("/predictions/{identity}/content")
    def prediction_content(scope_id: str, identity: str):
        return Response(service.prediction_content(scope_id, identity), media_type="application/json")

    @resources.post("/predictions/{identity}/cancel")
    def prediction_cancel(scope_id: str, identity: str):
        return resource_view(service.cancel_prediction(scope_id, identity))

    @resources.get("/models/{identity}/evaluation")
    def evaluation(scope_id: str, identity: str):
        return Response(service.model_content(scope_id, identity, "evaluation.json"), media_type="application/json")

    @resources.get("/models/{identity}/files/{member}")
    def member(scope_id: str, identity: str, member: str):
        return Response(service.model_content(scope_id, identity, member), media_type=MEMBERS[member][1])

    @internal.post("/claim")
    def claim(body: ClaimRequest):
        return {"run": worker_view(service.claim(body.session, body.key))}

    @internal.get("/recovery")
    def recovery():
        return {"runs": [worker_view(r) for r in service.recoverable()]}

    @internal.post("/runs/{identity}/recover")
    def recover(identity: str, body: RecoveryRequest):
        return worker_view(service.recover(identity, body.scope_id, body.previous_claim, body.new_session))

    @internal.post("/runs/{identity}/status")
    def receipt(identity: str, body: Owner):
        return worker_view(service.owned(identity, body.scope_id, body.session, body.claim))

    @internal.post("/runs/{identity}/start")
    def start(identity: str, body: StartRequest):
        return worker_view(service.start(identity, body.scope_id, body.session, body.claim, body.job_name))

    @internal.post("/runs/{identity}/heartbeat")
    def heartbeat(identity: str, body: Owner):
        return worker_view(service.heartbeat(identity, body.scope_id, body.session, body.claim))

    @internal.post("/runs/{identity}/input")
    def input_data(identity: str, body: Owner):
        with service.repo.transaction() as tx:
            run = service._owned(tx, identity, body.scope_id, body.session, body.claim)
            if run.status not in ("PENDING", "RUNNING") or run.cancel_requested:
                raise ServiceError("RUN_NOT_ACTIVE")
        return Response(service.dataset_content(body.scope_id, run.dataset_id), media_type="text/csv")

    @internal.put("/runs/{identity}/files/{member}")
    async def upload_member(identity: str, member: str, request: Request):
        try:
            owner = Owner.model_validate_json(request.headers.get("X-ML-Claim", ""))
        except ValueError:
            raise ServiceError("INVALID_CLAIM", 422) from None
        payload = await request.body()
        artifact = await asyncio.to_thread(service.upload_member, identity, owner.scope_id, owner.session,
                                           owner.claim, member, payload)
        return {"artifact_id": artifact.id, "status": artifact.status}

    @internal.post("/runs/{identity}/complete")
    def complete(identity: str, body: Owner):
        return worker_view(service.complete(identity, body.scope_id, body.session, body.claim))

    @internal.post("/runs/{identity}/stopped")
    def stopped(identity: str, body: StopRequest):
        return worker_view(service.stopped(identity, body.scope_id, body.session, body.claim, body.reason))

    app.include_router(resources)
    app.include_router(internal)
    return app


def main():
    import uvicorn
    try:
        app = create_app()
    except Exception:
        raise SystemExit("ML_CONFIGURATION_ERROR") from None
    uvicorn.run(app, host="127.0.0.1", port=8200, access_log=False)


if __name__ == "__main__":
    main()
