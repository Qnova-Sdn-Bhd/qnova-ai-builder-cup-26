from pathlib import Path

import pytest
from conftest import TestProvider, TextOnlyExtractor
from fastapi.testclient import TestClient

from handoff_guardian.api import create_app
from handoff_guardian.config import Settings
from handoff_guardian.errors import AppError
from handoff_guardian.jobs import JobRunner
from handoff_guardian.worker import create_worker


class Identity:
    def __init__(self):
        self.privileged = []

    def verify(self, token, privileged):
        self.privileged.append(privileged)
        if token != "valid-test-token":
            raise AppError("UNAUTHENTICATED", "Invalid token.", 401)
        return "reviewer"


def test_api_requires_verified_token_and_hides_internal_fields(system):
    guardian, _ = system
    with TestClient(create_app(guardian, Identity())) as client:
        assert client.get("/api/v1/projects").status_code == 401
        assert client.get("/api/v1/projects", headers={"Authorization": "Bearer forged"}).status_code == 401
        response = client.get("/api/v1/projects", headers={"Authorization": "Bearer valid-test-token"})
        assert response.status_code == 200 and len(response.json()["projects"]) == 1
        assert client.get("/internal/recover").status_code == 404


def test_task_completion_requires_token_key_and_current_reviewer(system, metadata):
    from conftest import approved

    guardian, actor = system
    action, _ = approved(system, metadata)
    task = guardian.execute(actor, "p", action["id"], action["binding"], "create-task-0001")["task"]
    route = f"/api/v1/projects/p/tasks/{task['id']}/complete"
    body = {"expected_task_version": 1, "expected_board_revision": 1}
    identity = Identity()
    with TestClient(create_app(guardian, identity)) as client:
        assert client.post(route, json=body).status_code == 401
        headers = {"Authorization": "Bearer valid-test-token"}
        assert client.post(route, json=body, headers=headers).status_code == 422
        headers["Idempotency-Key"] = "complete-task-0001"
        response = client.post(route, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "done"
        guardian.repo.set("projects/p/memberships/reviewer", {"user_id": "reviewer", "role": "viewer"})
        assert client.post(route, json=body, headers=headers).status_code == 403
    assert all(identity.privileged)


def test_demo_actor_header_rejected(system):
    guardian, _ = system
    with TestClient(create_app(guardian, Identity())) as client:
        result = client.get("/api/v1/projects", headers={"X-Demo-Actor-Id": "reviewer"})
        assert result.status_code == 401


def test_invalid_bodies_do_not_echo_document_text_and_write_checks_revocation(system):
    guardian, _ = system
    identity = Identity()
    with TestClient(create_app(guardian, identity)) as client:
        result = client.post(
            "/api/v1/projects/p/analyses",
            json={"secret_document_text": "DO NOT ECHO THIS"},
            headers={"Authorization": "Bearer valid-test-token", "Idempotency-Key": "request-key-0001"},
        )
        assert result.status_code == 422 and "DO NOT ECHO THIS" not in result.text
        assert identity.privileged == [True]
        assert set(result.json()) == {"error"}


def test_uid_binding_is_required_and_cannot_be_inferred_from_email(system):
    guardian, _ = system
    with pytest.raises(AppError, match="IDENTITY_NOT_PROVISIONED"):
        guardian.actor_for_uid("unprovisioned@example.test")


def test_worker_rejects_browser_token_and_arbitrary_job_inputs(system):
    guardian, _ = system

    def verify(token, expected):
        if token != "service-test-token":
            raise AppError("UNAUTHENTICATED", "Service identity required.", 401)

    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    with TestClient(create_worker(runner, verify)) as client:
        assert (
            client.post(
                "/internal/recover", json={}, headers={"Authorization": "Bearer valid-test-token"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/internal/jobs/job/run",
                json={"dispatch_generation": 1, "source": "foreign"},
                headers={"Authorization": "Bearer service-test-token"},
            ).status_code
            == 422
        )
        assert client.get("/api/v1/projects").status_code == 404


def valid_env():
    return {
        "APP_ENV": "prototype",
        "AI_PROVIDER": "vertex",
        "AGENT_RUNTIME": "vertex_agent_engine",
        "AUTH_MODE": "identity_platform",
        "STORAGE_BACKEND": "firestore",
        "FILE_STORAGE_BACKEND": "gcs",
        "QUEUE_BACKEND": "cloud_tasks",
        "TASK_BOARD_BACKEND": "firestore",
        "ALLOW_LIVE_MODEL_CALLS": "true",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "FIREBASE_PROJECT_ID": "test-project",
        "GCS_BUCKET": "private-bucket",
        "DOCUMENT_AI_LOCATION": "us",
        "DOCUMENT_AI_PROCESSOR_ID": "processor",
        "DOCUMENT_AI_PROCESSOR_VERSION": "version",
        "VERTEX_AGENT_ENGINE_RESOURCE_NAME": "projects/test-project/locations/us-central1/reasoningEngines/1",
        "GEMINI_MODEL": "configured-model",
        "WORKER_SERVICE_URL": "https://worker.example.run.app",
        "TASK_INVOKER_SERVICE_ACCOUNT": "tasks@test-project.iam.gserviceaccount.com",
        "SCHEDULER_INVOKER_SERVICE_ACCOUNT": "scheduler@test-project.iam.gserviceaccount.com",
        "CLOUD_TASKS_LOCATION": "us-central1",
        "CLOUD_TASKS_QUEUE": "work",
        "CORS_ALLOW_ORIGINS": "https://web.example.run.app",
    }


def test_google_only_settings_validate_complete_resources():
    assert Settings.from_env(valid_env()).project == "test-project"


@pytest.mark.parametrize(
    "key,value",
    [
        ("AI_PROVIDER", "mock"),
        ("AUTH_MODE", "local_demo"),
        ("STORAGE_BACKEND", "sqlite"),
        ("FILE_STORAGE_BACKEND", "local"),
        ("FIRESTORE_EMULATOR_HOST", "localhost:8080"),
        ("GEMINI_MODEL", ""),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/secret/key.json"),
        ("CORS_ALLOW_ORIGINS", "*"),
        ("WORKER_SERVICE_URL", "http://localhost:8080"),
        ("VERTEX_AGENT_ENGINE_RESOURCE_NAME", "https://foreign.example"),
    ],
)
def test_deployed_settings_fail_closed(key, value):
    env = valid_env()
    env[key] = value
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_backend_bundled_contract_matches_authoritative_contract():
    root = Path(__file__).resolve().parents[2]
    assert (root / "contracts/openapi.yaml").read_bytes() == (
        root / "backend/src/handoff_guardian/openapi.yaml"
    ).read_bytes()
