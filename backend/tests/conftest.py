"""Explicit test doubles; this module is excluded from runtime images."""

import copy
import threading

import pytest

from handoff_guardian.config import Settings
from handoff_guardian.contracts import digest
from handoff_guardian.errors import AppError
from handoff_guardian.ports import Actor
from handoff_guardian.service import Guardian


class MemoryRepository:
    def __init__(self):
        self.records = {}
        self.lock = threading.RLock()

    def get(self, path):
        return copy.deepcopy(self.records.get(path))

    def list(self, path, limit=1000):
        prefix = path + "/"
        return [
            copy.deepcopy(value)
            for key, value in sorted(self.records.items())
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ][:limit]

    def set(self, path, value):
        self.records[path] = copy.deepcopy(value)

    def create(self, path, value):
        if path in self.records:
            raise AppError("VERSION_CONFLICT", "Test store creation conflict.")
        self.set(path, value)

    def transact(self, callback):
        with self.lock:
            before = copy.deepcopy(self.records)
            try:
                return callback(self)
            except Exception:
                self.records = before
                raise

    def accessible_projects(self, user_id):
        return [key.split("/")[1] for key in self.records if key.endswith(f"/memberships/{user_id}")]

    def where(self, path, field, value, limit=100):
        return [record for record in self.list(path, 100000) if record.get(field) == value][:limit]

    def recent(self, path, field, limit):
        return sorted(self.list(path, 100000), key=lambda item: item[field], reverse=True)[:limit]


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.reads = 0

    def put(self, name, content, mime_type):
        if name in self.objects and self.objects[name] != content:
            raise AppError("VERSION_CONFLICT", "Test immutable object conflict.")
        self.objects[name] = content
        return {"name": name, "generation": 1, "sha256": digest(content)}

    def read(self, ref):
        self.reads += 1
        content = self.objects[ref["name"]]
        assert digest(content) == ref["sha256"]
        return content


class Dispatcher:
    def __init__(self):
        self.dispatched = []
        self.unavailable = False

    def enqueue(self, job_id, generation):
        if self.unavailable:
            raise TimeoutError("queue unavailable")
        self.dispatched.append((job_id, generation))


@pytest.fixture
def settings():
    return Settings(
        "test-project",
        "us-central1",
        "test-project",
        "test-bucket",
        "projects/test-project/locations/us/processors/ocr/processorVersions/1",
        "us",
        "projects/test-project/locations/us-central1/reasoningEngines/123",
        "gemini-test",
        "https://worker.example.run.app",
        "tasks@test-project.iam.gserviceaccount.com",
        "scheduler@test-project.iam.gserviceaccount.com",
        "us-central1",
        "work",
        ("https://web.example.run.app",),
    )


def populate(repo, project_id="p", user_id="reviewer", role="reviewer"):
    binding = digest(f"test-project:{user_id}".encode())
    repo.set(
        f"identity_bindings/{binding}",
        {
            "uid": user_id,
            "identity_project_id": "test-project",
            "user_id": user_id,
            "enabled": True,
            "version": 1,
        },
    )
    repo.set(f"users/{user_id}", {"id": user_id, "email": f"{user_id}@example.test", "display_name": user_id})
    repo.set(
        f"projects/{project_id}",
        {
            "id": project_id,
            "name": "Test handoff",
            "timezone": "UTC",
            "business_clock": {"mode": "synthetic", "as_of": "2026-10-18T12:00:00Z"},
            "evidence_revision": 0,
            "board_revision": 0,
        },
    )
    repo.set(f"projects/{project_id}/memberships/{user_id}", {"user_id": user_id, "role": role})
    return Actor(user_id, binding, 1)


@pytest.fixture
def system(settings):
    repo, store, dispatcher = MemoryRepository(), MemoryStore(), Dispatcher()
    actor = populate(repo)
    return Guardian(settings, repo, store, dispatcher), actor


@pytest.fixture
def metadata():
    return {
        "document_id": "sow",
        "version_number": 1,
        "title": "Agreed scope",
        "source_kind": "sow",
        "created_at": "2026-10-01T12:00:00Z",
        "effective_at": "2026-10-01T12:00:00Z",
        "supersedes_source_version_id": None,
        "expected_evidence_revision": 0,
    }


class TextOnlyExtractor:
    def extract(self, raw):
        raise AssertionError("Text ingestion must not call Document AI")


class TestProvider:
    __test__ = False

    def __init__(self, result=None, hook=None):
        self.calls = 0
        self.result = result
        self.hook = hook

    async def analyze(self, packet, attempt_id):
        self.calls += 1
        if self.hook:
            self.hook()
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return copy.deepcopy(self.result)
        source = packet["sources"][0]
        quote = source["canonical_text"][:10]
        span = {"source_version_id": source["id"], "start_char": 0, "end_char": len(quote), "quote": quote}
        return {
            "proposed_commitments": [
                {
                    "id": "candidate",
                    "description": "Deliver the agreed work",
                    "acceptance_criteria": ["Reviewed confirmation"],
                    "owner_id": None,
                    "due_date": None,
                    "evidence": [span],
                }
            ],
            "findings": [
                {
                    "id": "gap",
                    "kind": "acceptance_gap",
                    "title": "Acceptance evidence missing",
                    "explanation": "The searched packet does not contain acceptance confirmation.",
                    "commitment_ids": ["candidate"],
                    "evidence": [span],
                }
            ],
            "coverage_warnings": [],
        }


def uploaded(system, metadata, raw=None):
    guardian, actor = system
    if raw is None:
        raw = "Deliver café 🛠 with reviewed acceptance".encode()
    return guardian.upload(actor, "p", metadata, raw, "scope.md", "text/markdown", "upload-key-0001")


def ready_source(system, metadata):
    import asyncio

    from handoff_guardian.jobs import JobRunner

    guardian, actor = system
    source = uploaded(system, metadata)
    job_id = guardian.repo.get(f"projects/p/sources/{source['id']}")["_job_id"]
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    assert asyncio.run(runner.run(job_id, 1))["outcome"] == "completed"
    return guardian.source(actor, "p", source["id"]), runner


def analyzed(system, metadata, provider=None):
    import asyncio

    source, runner = ready_source(system, metadata)
    guardian, actor = system
    if provider is not None:
        runner.provider = provider
    run = guardian.start_analysis(
        actor,
        "p",
        {"expected_evidence_revision": 2, "evaluation_as_of": "2026-10-18T12:00:00Z"},
        "analysis-key-0001",
    )
    job_id = guardian.repo.get(f"projects/p/analyses/{run['id']}")["_job_id"]
    try:
        asyncio.run(runner.run(job_id, 1))
    except AppError as exc:
        if exc.status != 503:
            raise
    return guardian.analysis(actor, "p", run["id"]), runner


def approved(system, metadata):
    guardian, actor = system
    run, runner = analyzed(system, metadata)
    finding = run["findings"][0]
    action = guardian.propose(
        actor,
        "p",
        {
            "analysis_run_id": run["id"],
            "payload": {
                "finding_id": finding["id"],
                "title": "Obtain acceptance evidence",
                "description": "Confirm the explicit criteria with the reviewer.",
                "owner_id": actor.user_id,
                "due_date": None,
            },
            "expected_evidence_revision": 2,
            "expected_board_revision": 0,
            "expected_finding_version": 1,
            "replaces_action_id": None,
        },
    )
    return guardian.review_action(
        actor,
        "p",
        action["id"],
        {"decision": "approve", "note": "Reviewed all fields", "binding": action["binding"]},
    ), runner
