from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from materialsagent.api.routes.health import router as health_router
from materialsagent.infrastructure.config import load_settings
from materialsagent.infrastructure.logging import configure_logging


def create_app() -> FastAPI:
    settings = load_settings()
    request_logger = configure_logging(settings.log_level)
    app = FastAPI(title="Materials Agent Backend", version="0.1.0")

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
    return app
