import copy

import pytest
from conftest import approved

from handoff_guardian.contracts import validate
from handoff_guardian.errors import AppError


def created(system, metadata):
    guardian, actor = system
    action, _ = approved(system, metadata)
    task = guardian.execute(actor, "p", action["id"], action["binding"], "create-task-0001")["task"]
    return task, {"expected_task_version": 1, "expected_board_revision": 1}


def test_completion_is_atomic_audited_and_replay_safe(system, metadata):
    guardian, actor = system
    task, request = created(system, metadata)
    findings = copy.deepcopy(guardian.repo.list("projects/p/findings"))
    commitments = copy.deepcopy(guardian.repo.list("projects/p/commitments"))
    result = guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001")
    assert result["status"] == "done" and result["version"] == 2
    validate("Task", result)
    assert guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001") == result
    assert guardian.repo.get("projects/p")["board_revision"] == 2
    assert guardian.repo.get("projects/p")["evidence_revision"] == 2
    assert guardian.repo.list("projects/p/findings") == findings
    assert guardian.repo.list("projects/p/commitments") == commitments
    audits = [a for a in guardian.repo.list("projects/p/audit_events") if a["event_type"] == "task_completed"]
    assert len(audits) == 1 and audits[0]["actor_id"] == actor.user_id
    assert audits[0]["board_revision"] == 2
    # A subsequent already-done request records no second completion.
    guardian.complete_task(
        actor,
        "p",
        task["id"],
        {"expected_task_version": 2, "expected_board_revision": 2},
        "complete-task-0002",
    )
    assert guardian.repo.get("projects/p")["board_revision"] == 2


@pytest.mark.parametrize(
    "field,code", [("expected_task_version", "VERSION_CONFLICT"), ("expected_board_revision", "STALE_TARGET")]
)
def test_stale_completion_writes_nothing(system, metadata, field, code):
    guardian, actor = system
    task, request = created(system, metadata)
    request[field] = 99
    with pytest.raises(AppError, match=code):
        guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001")
    assert guardian.repo.get(f"projects/p/tasks/{task['id']}")["status"] == "open"
    assert guardian.repo.get("projects/p")["board_revision"] == 1


def test_completion_rechecks_permission_on_replay_and_rejects_changed_body(system, metadata):
    guardian, actor = system
    task, request = created(system, metadata)
    guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001")
    with pytest.raises(AppError, match="IDEMPOTENCY_CONFLICT"):
        guardian.complete_task(
            actor, "p", task["id"], {**request, "expected_task_version": 2}, "complete-task-0001"
        )
    guardian.repo.set("projects/p/memberships/reviewer", {"user_id": "reviewer", "role": "viewer"})
    with pytest.raises(AppError, match="REVIEWER_REQUIRED"):
        guardian.complete_task(actor, "p", task["id"], request, "complete-task-0001")


def test_foreign_task_and_extra_fields_rejected(system, metadata):
    guardian, actor = system
    _, request = created(system, metadata)
    with pytest.raises(AppError, match="NOT_FOUND"):
        guardian.complete_task(actor, "p", "foreign_task", request, "complete-task-0001")
    with pytest.raises(AppError, match="INVALID_REQUEST"):
        guardian.complete_task(
            actor, "p", "foreign_task", {**request, "status": "done"}, "complete-task-0001"
        )
