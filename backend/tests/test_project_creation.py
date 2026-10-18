from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import populate
from fastapi.testclient import TestClient
from test_api_and_configuration import Identity

from handoff_guardian.api import create_app
from handoff_guardian.contracts import validate
from handoff_guardian.errors import AppError


def request_body(**overrides):
    return {
        "name": "  New delivery  ",
        "timezone": "Asia/Kuala_Lumpur",
        "reviewer_project_id": "p",
        **overrides,
    }


def test_creation_is_empty_audited_and_does_not_copy_synthetic_clock(system):
    guardian, actor = system
    before = guardian.repo.get("projects/p")
    project = guardian.create_project(actor, request_body(), "new-project-001")
    validate("Project", project)
    assert project["name"] == "New delivery"
    assert project["business_clock"]["mode"] == "real"
    assert project["evidence_revision"] == project["board_revision"] == 0
    workspace = guardian.workspace(actor, project["id"])
    assert workspace["actor_role"] == "reviewer"
    assert workspace["memberships"] == [{"user_id": actor.user_id, "role": "reviewer"}]
    for collection in ("sources", "commitments", "findings", "analysis_runs", "action_proposals", "tasks"):
        assert workspace[collection] == []
    assert {event["event_type"] for event in workspace["audit_events"]} == {
        "project_created",
        "membership_granted",
    }
    for event in workspace["audit_events"]:
        validate("AuditEvent", event)
        assert event["actor_id"] == actor.user_id
    assert guardian.repo.get("projects/p") == before
    assert len(guardian.list_projects(actor)["projects"]) == 2
    assert not guardian.store.objects and not guardian.dispatcher.dispatched


def test_concurrent_retries_create_one_project_and_changed_payload_conflicts(system):
    guardian, actor = system
    with ThreadPoolExecutor(max_workers=4) as pool:
        projects = list(
            pool.map(lambda _: guardian.create_project(actor, request_body(), "new-project-001"), range(4))
        )
    assert len({project["id"] for project in projects}) == 1
    assert len(guardian.repo.list("projects")) == 2
    assert len(guardian.repo.list(f"projects/{projects[0]['id']}/audit_events")) == 2
    with pytest.raises(AppError, match="IDEMPOTENCY_CONFLICT"):
        guardian.create_project(actor, request_body(name="Different"), "new-project-001")


@pytest.mark.parametrize("change", ["viewer", "nonmember", "disabled", "version"])
def test_permissions_rechecked_on_creation_and_replay(system, change):
    guardian, actor = system
    guardian.create_project(actor, request_body(), "new-project-001")
    if change == "viewer":
        guardian.repo.set("projects/p/memberships/reviewer", {"user_id": "reviewer", "role": "viewer"})
    elif change == "nonmember":
        del guardian.repo.records["projects/p/memberships/reviewer"]
    else:
        binding = guardian.repo.get(f"identity_bindings/{actor.binding_id}")
        binding["enabled" if change == "disabled" else "version"] = False if change == "disabled" else 2
        guardian.repo.set(f"identity_bindings/{actor.binding_id}", binding)
    for key in ("new-project-001", "new-project-002"):
        with pytest.raises(AppError):
            guardian.create_project(actor, request_body(), key)
    assert len(guardian.repo.list("projects")) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "   "},
        {"name": "x" * 201},
        {"timezone": "Invalid/Zone"},
        {"role": "reviewer"},
        {"business_clock": {"mode": "synthetic", "as_of": "2026-01-01T00:00:00Z"}},
        {"reviewer_project_id": "foreign"},
    ],
)
def test_invalid_or_unauthorized_request_writes_nothing(system, overrides):
    guardian, actor = system
    before = dict(guardian.repo.records)
    with pytest.raises(AppError):
        guardian.create_project(actor, request_body(**overrides), "new-project-001")
    assert guardian.repo.records == before


def test_viewer_cannot_access_new_project(system):
    guardian, actor = system
    viewer = populate(guardian.repo, user_id="viewer", role="viewer")
    project = guardian.create_project(actor, request_body(), "new-project-001")
    with pytest.raises(AppError, match="NOT_FOUND"):
        guardian.workspace(viewer, project["id"])


def test_api_authentication_idempotency_and_creation(system):
    guardian, _ = system
    identity = Identity()
    with TestClient(create_app(guardian, identity)) as client:
        assert client.post("/api/v1/projects", json=request_body()).status_code == 401
        headers = {"Authorization": "Bearer valid-test-token", "Idempotency-Key": "new-project-001"}
        first = client.post("/api/v1/projects", json=request_body(), headers=headers)
        assert first.status_code == 201
        second = client.post("/api/v1/projects", json=request_body(), headers=headers)
        assert second.json()["id"] == first.json()["id"]
        assert all(identity.privileged)
        assert (
            client.post(
                "/api/v1/projects", json=request_body(), headers={"Authorization": "Bearer valid-test-token"}
            ).status_code
            == 422
        )
