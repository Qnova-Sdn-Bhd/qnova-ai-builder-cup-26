"""Separate Cloud Run IAM service; this app is never mounted on the public API."""

import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .config import Settings
from .contracts import validate
from .errors import AppError, require
from .google_adapters import (
    AgentEngineProvider,
    DocumentAIExtractor,
    FirestoreRepository,
    GCSStore,
    TasksDispatcher,
)
from .jobs import JobRunner
from .observability import configure_logging, emit
from .service import Guardian, identifier

configure_logging()


def create_worker(runner=None, service_verifier=None):
    @asynccontextmanager
    async def lifespan(app):
        if runner is None:
            settings = Settings.from_env()
            guardian = Guardian(
                settings, FirestoreRepository(settings), GCSStore(settings), TasksDispatcher(settings)
            )
            app.state.runner = JobRunner(
                guardian, DocumentAIExtractor(settings), AgentEngineProvider(settings)
            )
        else:
            app.state.runner = runner
        yield

    app = FastAPI(title="Handoff Guardian private worker", lifespan=lifespan, docs_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = identifier("req")
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        emit(
            "worker_request_completed",
            request_id=request.state.request_id,
            http_status=response.status_code,
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        return response

    @app.exception_handler(AppError)
    async def error(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": getattr(request.state, "request_id", identifier("req")),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return await error(request, AppError("INVALID_REQUEST", "Invalid worker request.", 422))

    @app.exception_handler(HTTPException)
    async def routing_error(request, exc):
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST"
        return await error(
            request, AppError(code, "The requested worker operation is unavailable.", exc.status_code)
        )

    @app.exception_handler(Exception)
    async def dependency_error(request, exc):
        emit(
            "worker_request_failed",
            request_id=getattr(request.state, "request_id", "unknown"),
            code="TEMPORARY_DEPENDENCY_FAILURE",
            exception_type=type(exc).__name__,
        )
        return await error(
            request, AppError("TEMPORARY_DEPENDENCY_FAILURE", "A Google dependency is unavailable.", 503)
        )

    def verify(request, authorization, scheduler=False):
        settings = request.app.state.runner.settings
        require(
            authorization and authorization.startswith("Bearer "),
            "UNAUTHENTICATED",
            "Service identity required.",
            401,
        )
        expected = settings.scheduler_invoker if scheduler else settings.task_invoker
        if service_verifier:
            service_verifier(authorization[7:], expected)
            return
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(
                authorization[7:], GoogleRequest(), audience=settings.worker_url
            )
        except Exception as exc:
            raise AppError("UNAUTHENTICATED", "Invalid service identity token.", 401) from exc
        require(
            claims.get("email_verified") is True and claims.get("email") == expected,
            "UNAUTHENTICATED",
            "The service principal cannot invoke this operation.",
            401,
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/internal/jobs/{job_id}/run")
    async def run(
        job_id: str,
        request: Request,
        body: Annotated[dict, Body()],
        authorization: Annotated[str | None, Header()] = None,
    ):
        verify(request, authorization)
        validate("Id", job_id)
        validate("JobDispatchRequest", body)
        return await request.app.state.runner.run(job_id, body["dispatch_generation"])

    @app.post("/internal/recover")
    def recover(
        request: Request, body: Annotated[dict, Body()], authorization: Annotated[str | None, Header()] = None
    ):
        verify(request, authorization, True)
        validate("RecoveryRequest", body)
        return request.app.state.runner.recover()

    return app


app = create_worker()
