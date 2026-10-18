"""Real Firestore transaction checks; explicitly skipped if its emulator is absent.

Run with FIRESTORE_EMULATOR_HOST=127.0.0.1:8089 and pytest -q tests/test_firestore_emulator.py.
Deployed settings reject this variable; only this test constructs the adapter directly.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from conftest import Dispatcher, MemoryStore, TestProvider, TextOnlyExtractor, approved, populate, uploaded

from handoff_guardian.errors import AppError
from handoff_guardian.google_adapters import FirestoreRepository
from handoff_guardian.jobs import JobRunner
from handoff_guardian.service import Guardian

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="Firestore emulator unavailable; cloud transaction behavior is unverified",
)


def concurrent_then_replay(call):
    """Contention may return retryable 503; settle each original request with its same key."""

    def attempt(index):
        try:
            return call(index)
        except AppError as exc:
            assert exc.status == 503 and exc.code == "TEMPORARY_DEPENDENCY_FAILURE"
            return exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    return [call(index) if isinstance(result, AppError) else result for index, result in enumerate(results)]


@pytest.fixture
def emulator_system(settings):
    settings = replace(settings, project=f"hg-test-{uuid.uuid4().hex[:12]}")
    repo = FirestoreRepository(settings)
    # Bootstrap writes use an explicit atomic transaction, not the buffered adapter's direct set method.
    actor = repo.transact(lambda unit: populate(unit))
    guardian = Guardian(settings, repo, MemoryStore(), Dispatcher())
    return guardian, actor


def test_firestore_concurrent_project_creation(emulator_system):
    guardian, actor = emulator_system
    body = {"name": "New project", "timezone": "UTC", "reviewer_project_id": "p"}
    results = concurrent_then_replay(
        lambda _: guardian.create_project(actor, body, "create-project-concurrent")
    )
    assert len({item["id"] for item in results}) == 1
    assert len(guardian.repo.list("projects")) == 2
    workspace = guardian.workspace(actor, results[0]["id"])
    assert workspace["actor_role"] == "reviewer"
    assert len(workspace["audit_events"]) == 2
    assert not workspace["sources"] and not workspace["tasks"]


def test_firestore_transaction_concurrent_execution_and_replay(emulator_system, metadata):
    guardian, actor = emulator_system
    repo = guardian.repo
    action, _ = approved(emulator_system, metadata)

    def execute(i):
        return guardian.execute(actor, "p", action["id"], action["binding"], f"concurrency-key-{i:04}")

    results = concurrent_then_replay(execute)
    assert sum(not item["replayed"] for item in results) == 1
    assert len(repo.list("projects/p/tasks")) == 1
    assert repo.get("projects/p")["board_revision"] == 1
    assert (
        sum(item["event_type"] == "blocker_task_created" for item in repo.list("projects/p/audit_events"))
        == 1
    )


def test_firestore_task_completion_replay_is_single_effect(emulator_system, metadata):
    guardian, actor = emulator_system
    action, _ = approved(emulator_system, metadata)
    task = guardian.execute(actor, "p", action["id"], action["binding"], "create-task-0001")["task"]
    request = {"expected_task_version": 1, "expected_board_revision": 1}
    results = concurrent_then_replay(
        lambda _: guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001")
    )
    assert all(t["status"] == "done" and t["version"] == 2 for t in results)
    assert guardian.repo.get("projects/p")["board_revision"] == 2
    audits = guardian.repo.list("projects/p/audit_events")
    assert sum(a["event_type"] == "task_completed" for a in audits) == 1


def test_firestore_concurrent_upload_receipt_is_single_effect(emulator_system, metadata):
    guardian, actor = emulator_system
    sources = concurrent_then_replay(lambda _: uploaded(emulator_system, metadata))
    assert len({source["id"] for source in sources}) == 1
    assert len(guardian.repo.list("projects/p/sources")) == 1
    assert len(guardian.repo.list("jobs")) == 1
    assert guardian.repo.get("projects/p")["evidence_revision"] == 1


@pytest.mark.parametrize("changed", ["evidence_revision", "board_revision", "finding_version"])
def test_firestore_stale_execution_commits_marker_without_task(emulator_system, metadata, changed):
    guardian, actor = emulator_system
    action, _ = approved(emulator_system, metadata)

    def mutate(unit):
        target = (
            "projects/p"
            if changed != "finding_version"
            else (f"projects/p/findings/{action['payload']['finding_id']}")
        )
        value = unit.get(target)
        value[changed if changed != "finding_version" else "version"] += 1
        unit.set(target, value)

    guardian.repo.transact(mutate)
    with pytest.raises(AppError, match="STALE_"):
        guardian.execute(actor, "p", action["id"], action["binding"], "stale-key-0001")
    assert guardian.repo.list("projects/p/tasks") == []
    assert guardian.repo.get(f"projects/p/action_proposals/{action['id']}")["status"] == "stale"
    assert (
        sum(event["event_type"] == "action_stale" for event in guardian.repo.list("projects/p/audit_events"))
        == 1
    )


def test_firestore_transaction_failure_has_no_partial_task_effect(emulator_system):
    guardian, actor = emulator_system

    def interrupted(unit):
        project = unit.get("projects/p")
        project["board_revision"] += 1
        unit.create("projects/p/tasks/interrupted", {"id": "interrupted"})
        unit.set("projects/p", project)
        raise RuntimeError("pre-commit interruption")

    with pytest.raises(RuntimeError):
        guardian.repo.transact(interrupted)
    assert guardian.repo.get("projects/p/tasks/interrupted") is None
    assert guardian.repo.get("projects/p")["board_revision"] == 0


def test_firestore_postcommit_replay_after_later_revision(emulator_system, metadata):
    guardian, actor = emulator_system
    action, _ = approved(emulator_system, metadata)
    first = guardian.execute(actor, "p", action["id"], action["binding"], "first-key-0001")

    def newer(unit):
        project = unit.get("projects/p")
        project["evidence_revision"] += 1
        project["board_revision"] += 1
        unit.set("projects/p", project)

    guardian.repo.transact(newer)
    replay = guardian.execute(actor, "p", action["id"], action["binding"], "second-key-0002")
    assert replay["replayed"] and replay["task"] == first["task"]
    binding = dict(action["binding"], finding_version=99)
    with pytest.raises(AppError, match="IDEMPOTENCY_CONFLICT"):
        guardian.execute(actor, "p", action["id"], binding, "second-key-0002")
    assert len(guardian.repo.list("projects/p/tasks")) == 1


def test_firestore_lost_fence_cannot_publish_evidence(emulator_system, metadata):
    guardian, actor = emulator_system
    source = uploaded(emulator_system, metadata)
    job_id = guardian.repo.get(f"projects/p/sources/{source['id']}")["_job_id"]
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    first = runner.claim(job_id, 1)

    def steal(unit):
        job = unit.get(f"jobs/{job_id}")
        job["fencing_token"] += 1
        unit.set(f"jobs/{job_id}", job)

    guardian.repo.transact(steal)
    assert runner.ingest(first) == "stale"
    assert guardian.source(actor, "p", source["id"])["ingestion_status"] == "processing"
    assert guardian.repo.get("projects/p")["evidence_revision"] == 1
