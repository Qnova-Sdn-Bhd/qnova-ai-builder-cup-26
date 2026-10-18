import asyncio
import io
import json
import logging

import pytest
from conftest import TestProvider, TextOnlyExtractor, uploaded
from fastapi.testclient import TestClient

from handoff_guardian.api import create_app
from handoff_guardian.errors import AppError
from handoff_guardian.jobs import JobRunner
from handoff_guardian.observability import configure_logging, emit
from handoff_guardian.worker import create_worker


@pytest.fixture
def events():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = configure_logging()
    logger.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        logger.removeHandler(handler)


def test_application_info_logger_is_independent_and_handler_is_not_duplicated(events):
    root, sdk = logging.getLogger(), logging.getLogger("google.auth")
    previous_levels = root.level, sdk.level
    logger = configure_logging()
    configure_logging()
    assert logger.getEffectiveLevel() == logging.INFO and not logger.propagate
    assert sum(bool(getattr(handler, "_handoff_guardian_json", False)) for handler in logger.handlers) == 1
    assert (root.level, sdk.level) == previous_levels
    emit(
        "request_completed",
        request_id="req_test",
        http_status=200,
        latency_ms=3,
        source="SECRET_SOURCE",
        authorization="Bearer SECRET_TOKEN",
        exception="SECRET_EXCEPTION",
    )
    assert events() == [
        {
            "severity": "INFO",
            "event": "request_completed",
            "request_id": "req_test",
            "http_status": 200,
            "latency_ms": 3,
        }
    ]


def test_terminal_job_failure_emits_metric_matching_safe_durable_state(system, metadata, events):
    guardian, actor = system

    class BrokenPDF:
        def extract(self, raw):
            raise AppError("EXTRACTION_FAILED", "SECRET_EXCEPTION_BODY Bearer SECRET_TOKEN", 422)

    source = guardian.upload(
        actor,
        "p",
        metadata,
        b"%PDF-SECRET_SOURCE_BODY",
        "SECRET_FILENAME.pdf",
        "application/pdf",
        "upload-secret-key",
    )
    job_id = guardian.repo.get(f"projects/p/sources/{source['id']}")["_job_id"]
    result = asyncio.run(JobRunner(guardian, BrokenPDF(), TestProvider()).run(job_id, 1))
    assert result["outcome"] == "failed"
    failure = next(event for event in events() if event["event"] == "job_failed")
    assert failure["status"] == "failed" and failure["code"] == "EXTRACTION_FAILED"
    assert failure["job_id"] == job_id and failure["target_id"] == source["id"]
    assert failure["project_id"] == "p" and failure["kind"] == "ingestion"
    assert failure["attempt_count"] == failure["dispatch_generation"] == failure["fencing_token"] == 1
    assert failure["latency_ms"] >= 0
    assert "SECRET_" not in json.dumps(events()) and "Bearer" not in json.dumps(events())


def test_retryable_job_failure_records_budget_and_safe_code_without_exception_body(system, metadata, events):
    guardian, actor = system

    class TimeoutPDF:
        def extract(self, raw):
            raise TimeoutError("SECRET_EXCEPTION_BODY")

    source = guardian.upload(
        actor,
        "p",
        metadata,
        b"%PDF-SECRET_SOURCE_BODY",
        "document.pdf",
        "application/pdf",
        "timeout-upload-key",
    )
    job_id = guardian.repo.get(f"projects/p/sources/{source['id']}")["_job_id"]
    with pytest.raises(AppError, match="TEMPORARY_DEPENDENCY_FAILURE"):
        asyncio.run(JobRunner(guardian, TimeoutPDF(), TestProvider()).run(job_id, 1))
    result = next(event for event in events() if event["event"] == "job_outcome")
    assert result["status"] == "pending" and result["outcome"] == "retryable_error"
    assert result["code"] == "PROVIDER_TIMEOUT" and result["attempt_count"] == 1
    assert result["dispatch_generation"] == 2 and "SECRET_" not in json.dumps(events())


def test_recovery_emits_terminal_budget_failure_and_aggregate_counts(system, metadata, events):
    guardian, actor = system
    uploaded(system, metadata)
    job = guardian.repo.list("jobs")[0]
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    runner.claim(job["id"], 1)
    durable = guardian.repo.get(f"jobs/{job['id']}")
    durable.update(lease_until="2000-01-01T00:00:00Z", attempt_count=3)
    guardian.repo.set(f"jobs/{job['id']}", durable)
    assert runner.recover()["failed_jobs"] == 1
    failure = next(event for event in events() if event["event"] == "job_failed")
    assert failure["status"] == "failed" and failure["code"] == "RETRY_BUDGET_EXHAUSTED"
    assert failure["attempt_count"] == 3 and failure["outcome"] == "recovery_budget_exhausted"
    aggregate = next(event for event in events() if event["event"] == "recovery_completed")
    assert aggregate["failed_jobs"] == aggregate["expired_jobs"] == 1
    assert aggregate["latency_ms"] >= 0


def test_api_and_worker_request_logs_exclude_identity_tokens_and_request_bodies(system, events):
    guardian, _ = system

    class InvalidIdentity:
        def verify(self, token, privileged):
            raise AppError("UNAUTHENTICATED", "Invalid identity.", 401)

    with TestClient(create_app(guardian, InvalidIdentity())) as client:
        assert (
            client.get("/api/v1/projects", headers={"Authorization": "Bearer SECRET_USER_TOKEN"}).status_code
            == 401
        )
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())

    def verify(token, expected):
        assert token == "SECRET_SERVICE_TOKEN"

    with TestClient(create_worker(runner, verify)) as client:
        response = client.post(
            "/internal/recover", json={}, headers={"Authorization": "Bearer SECRET_SERVICE_TOKEN"}
        )
    assert response.status_code == 200
    assert any(event["event"] == "request_completed" and event["http_status"] == 401 for event in events())
    request = next(event for event in events() if event["event"] == "worker_request_completed")
    assert request["http_status"] == 200 and request["request_id"] == response.headers["X-Request-ID"]
    assert "SECRET_" not in json.dumps(events()) and "Bearer" not in json.dumps(events())
