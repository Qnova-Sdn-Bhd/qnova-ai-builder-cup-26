"""Public Cloud Run API. Production startup is Google-only and fails closed."""

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, Depends, FastAPI, File, Form, Header, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .config import Settings
from .contracts import validate
from .errors import AppError, require
from .google_adapters import FirebaseIdentity, FirestoreRepository, GCSStore, TasksDispatcher
from .observability import configure_logging, emit
from .service import Guardian, identifier

configure_logging()


def create_app(guardian=None, identity=None):
    @asynccontextmanager
    async def lifespan(app):
        if guardian is None:
            settings = Settings.from_env()
            app.state.guardian = Guardian(
                settings, FirestoreRepository(settings), GCSStore(settings), TasksDispatcher(settings)
            )
            app.state.identity = FirebaseIdentity(settings.firebase_project)
        else:
            app.state.guardian, app.state.identity = guardian, identity
        yield

    app = FastAPI(title="Handoff Guardian API", version="0.1.0", lifespan=lifespan)
    origins = (
        guardian.settings.cors_origins if guardian else os.environ.get("CORS_ALLOW_ORIGINS", "").split(",")
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = identifier("req")
        started = time.monotonic()
        if request.headers.get("X-Demo-Actor-Id"):
            return error_response(
                request, AppError("UNAUTHENTICATED", "A verified sign-in is required.", 401)
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "private, no-store"
        emit(
            "request_completed",
            request_id=request.state.request_id,
            http_status=response.status_code,
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        return response

    def error_response(request, exc):
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

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(request, AppError("INVALID_REQUEST", "The request shape is invalid.", 422))

    @app.exception_handler(HTTPException)
    async def routing_error(request, exc):
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST"
        return error_response(
            request, AppError(code, "The requested operation is unavailable.", exc.status_code)
        )

    @app.exception_handler(Exception)
    async def dependency_error(request, exc):
        emit(
            "request_failed",
            request_id=getattr(request.state, "request_id", "unknown"),
            code="TEMPORARY_DEPENDENCY_FAILURE",
            exception_type=type(exc).__name__,
        )
        return error_response(
            request,
            AppError("TEMPORARY_DEPENDENCY_FAILURE", "A required Google service is unavailable.", 503),
        )

    def services(request: Request):
        return request.app.state.guardian

    def authenticate(request: Request, authorization: Annotated[str | None, Header()] = None):
        require(
            authorization is not None and authorization.startswith("Bearer "),
            "UNAUTHENTICATED",
            "Sign in with your email address and password to continue.",
            401,
        )
        uid = request.app.state.identity.verify(authorization[7:], request.method != "GET")
        return request.app.state.guardian.actor_for_uid(uid)

    ActorDep = Annotated[object, Depends(authenticate)]
    ServiceDep = Annotated[Guardian, Depends(services)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(service: ServiceDep):
        return {"status": "configured", "provider": "google_cloud", "live_dependencies_verified": False}

    @app.get("/api/v1/projects")
    def projects(actor: ActorDep, service: ServiceDep):
        return service.list_projects(actor)

    @app.post("/api/v1/projects", status_code=201)
    def create_project(
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        return service.create_project(actor, body, idempotency_key)

    @app.get("/api/v1/projects/{project_id}/workspace")
    def workspace(project_id: str, actor: ActorDep, service: ServiceDep):
        validate("Id", project_id)
        return service.workspace(actor, project_id)

    @app.post("/api/v1/projects/{project_id}/sources", status_code=202)
    async def upload(
        project_id: str,
        request: Request,
        actor: ActorDep,
        service: ServiceDep,
        file: Annotated[UploadFile, File()],
        metadata: Annotated[str, Form()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        form = await request.form()
        require(
            set(form) == {"file", "metadata"}
            and len(form.getlist("file")) == 1
            and len(form.getlist("metadata")) == 1,
            "INVALID_REQUEST",
            "Unexpected multipart fields.",
            422,
        )
        # Authorize before reading bytes or writing GCS, including multipart replays.
        service.authorize(service.repo, actor, project_id, True)
        raw = await file.read(service.settings.max_upload_bytes + 1)
        try:
            value = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise AppError("INVALID_REQUEST", "Metadata must be valid JSON.", 422) from exc
        return service.upload(
            actor, project_id, value, raw, file.filename or "", file.content_type, idempotency_key
        )

    @app.get("/api/v1/projects/{project_id}/sources/{source_version_id}")
    def source(project_id: str, source_version_id: str, actor: ActorDep, service: ServiceDep):
        validate("Id", project_id)
        return service.source(actor, project_id, source_version_id)

    @app.get("/api/v1/projects/{project_id}/sources/{source_version_id}/original")
    def original(project_id: str, source_version_id: str, actor: ActorDep, service: ServiceDep):
        validate("Id", project_id)
        value, mime_type, filename = service.original(actor, project_id, source_version_id)
        return Response(
            value,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/v1/projects/{project_id}/sources/{source_version_id}/review")
    def review_source(
        project_id: str,
        source_version_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
    ):
        validate("Id", project_id)
        return service.review_source(actor, project_id, source_version_id, body)

    @app.post("/api/v1/projects/{project_id}/sources/{source_version_id}/retry-ingestion", status_code=202)
    def retry_source(
        project_id: str,
        source_version_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        return service.remedy_source(actor, project_id, source_version_id, body, idempotency_key)

    @app.post("/api/v1/projects/{project_id}/sources/{source_version_id}/withdraw-failed")
    def withdraw_source(
        project_id: str,
        source_version_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        return service.remedy_source(actor, project_id, source_version_id, body, idempotency_key, True)

    @app.post("/api/v1/projects/{project_id}/analyses", status_code=202)
    def analyze(
        project_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        return service.start_analysis(actor, project_id, body, idempotency_key)

    @app.get("/api/v1/projects/{project_id}/analyses/{analysis_run_id}")
    def analysis(project_id: str, analysis_run_id: str, actor: ActorDep, service: ServiceDep):
        validate("Id", project_id)
        return service.analysis(actor, project_id, analysis_run_id)

    @app.post("/api/v1/projects/{project_id}/action-proposals", status_code=201)
    def propose(project_id: str, actor: ActorDep, service: ServiceDep, body: Annotated[dict, Body()]):
        validate("Id", project_id)
        return service.propose(actor, project_id, body)

    @app.post("/api/v1/projects/{project_id}/action-proposals/{action_id}/review")
    def review_action(
        project_id: str, action_id: str, actor: ActorDep, service: ServiceDep, body: Annotated[dict, Body()]
    ):
        validate("Id", project_id)
        return service.review_action(actor, project_id, action_id, body)

    @app.post("/api/v1/projects/{project_id}/action-proposals/{action_id}/execute", status_code=201)
    def execute(
        project_id: str,
        action_id: str,
        actor: ActorDep,
        service: ServiceDep,
        response: Response,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        require(set(body) == {"binding"}, "INVALID_REQUEST", "Execution accepts only the exact binding.", 422)
        result = service.execute(actor, project_id, action_id, body["binding"], idempotency_key)
        response.status_code = 200 if result["replayed"] else 201
        return result

    @app.post("/api/v1/projects/{project_id}/tasks/{task_id}/complete")
    def complete_task(
        project_id: str,
        task_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str, Header()],
    ):
        validate("Id", project_id)
        return service.complete_task(actor, project_id, task_id, body, idempotency_key)

    @app.post("/api/v1/projects/{project_id}/commitments/{commitment_id}/review")
    def review_commitment(
        project_id: str,
        commitment_id: str,
        actor: ActorDep,
        service: ServiceDep,
        body: Annotated[dict, Body()],
    ):
        validate("Id", project_id)
        return service.review_entity(actor, project_id, commitment_id, body, True)

    @app.post("/api/v1/projects/{project_id}/findings/{finding_id}/review")
    def review_finding(
        project_id: str, finding_id: str, actor: ActorDep, service: ServiceDep, body: Annotated[dict, Body()]
    ):
        validate("Id", project_id)
        return service.review_entity(actor, project_id, finding_id, body)

    return app


app = create_app()
