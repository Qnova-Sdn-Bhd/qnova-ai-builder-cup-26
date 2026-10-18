import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TestProvider, TextOnlyExtractor, analyzed, approved, ready_source, uploaded

from handoff_guardian.contracts import digest, validate
from handoff_guardian.errors import AppError
from handoff_guardian.evidence import check_span
from handoff_guardian.jobs import JobRunner


def test_upload_is_unreviewed_and_duplicate_has_no_second_effect(system, metadata):
    guardian, actor = system
    source = uploaded(system, metadata)
    assert source["authority"] == "unreviewed" and source["canonical_text"] is None
    assert uploaded(system, metadata) == source
    assert len(guardian.repo.list("jobs")) == 1
    assert guardian.repo.get("projects/p")["evidence_revision"] == 1
    validate("SourceVersion", source)


def test_changed_upload_idempotency_body_conflicts(system, metadata):
    uploaded(system, metadata)
    with pytest.raises(AppError, match="IDEMPOTENCY_CONFLICT"):
        uploaded(system, metadata, b"different")


def test_wrong_project_rejected_before_gcs_read(system, metadata):
    guardian, actor = system
    source = uploaded(system, metadata)
    with pytest.raises(AppError, match="NOT_FOUND"):
        guardian.source(actor, "foreign", source["id"])
    assert guardian.store.reads == 0


def test_viewer_cannot_upload_even_replay(system, metadata):
    guardian, actor = system
    uploaded(system, metadata)
    guardian.repo.set("projects/p/memberships/reviewer", {"user_id": "reviewer", "role": "viewer"})
    with pytest.raises(AppError, match="REVIEWER_REQUIRED"):
        uploaded(system, metadata)


def test_ingestion_unicode_offsets_and_terminal_replay(system, metadata):
    guardian, actor = system
    source, runner = ready_source(system, metadata)
    assert source["canonical_text"] == "Deliver café 🛠 with reviewed acceptance"
    text = source["canonical_text"]
    index = text.index("🛠")
    check_span(
        {"source_version_id": source["id"], "start_char": index, "end_char": index + 1, "quote": "🛠"},
        {source["id"]: source},
    )
    job = guardian.repo.list("jobs")[0]
    assert asyncio.run(runner.run(job["id"], 1))["outcome"] == "already_terminal"
    assert guardian.repo.get("projects/p")["evidence_revision"] == 2
    validate("SourceVersion", source)


def test_broken_text_is_visible_failed_original_preserved_and_retryable(system, metadata):
    guardian, actor = system
    source = uploaded(system, metadata, b"\xff\xfe")
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    job = guardian.repo.list("jobs")[0]
    assert asyncio.run(runner.run(job["id"], 1))["outcome"] == "failed"
    failed = guardian.source(actor, "p", source["id"])
    assert failed["ingestion_status"] == "failed"
    assert guardian.original(actor, "p", source["id"])[0] == b"\xff\xfe"
    result = guardian.remedy_source(
        actor, "p", source["id"], {"expected_evidence_revision": 2}, "retry-key-0001"
    )
    assert result["source"]["ingestion_status"] == "queued"
    assert len(guardian.repo.list("jobs")) == 2
    assert guardian.repo.get(f"jobs/{job['id']}")["attempt_count"] == 1


def test_queue_failure_keeps_outbox_for_recovery(system, metadata):
    guardian, actor = system
    guardian.dispatcher.unavailable = True
    uploaded(system, metadata)
    assert guardian.repo.list("outbox")[0]["status"] == "pending"
    guardian.dispatcher.unavailable = False
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    assert runner.recover()["enqueued"] == 1


def test_recovery_not_starved_by_terminal_records(system, metadata):
    guardian, actor = system
    for i in range(150):
        guardian.repo.set(
            f"outbox/aaa-{i:04}", {"job_id": f"aaa-{i:04}", "generation": 1, "status": "dispatched"}
        )
    guardian.dispatcher.unavailable = True
    uploaded(system, metadata)
    guardian.dispatcher.unavailable = False
    assert JobRunner(guardian, TextOnlyExtractor(), TestProvider()).recover()["enqueued"] == 1


def test_active_lease_and_old_generation_do_not_invoke_provider(system, metadata):
    guardian, actor = system
    uploaded(system, metadata)
    job = guardian.repo.list("jobs")[0]
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    assert runner.claim(job["id"], 2) == "ignored_outdated"
    runner.claim(job["id"], 1)
    with pytest.raises(AppError, match="JOB_LEASE_ACTIVE"):
        runner.claim(job["id"], 1)
    assert guardian.repo.get(f"jobs/{job['id']}")["attempt_count"] == 1


def test_analysis_validates_and_publishes_only_proposed_candidates(system, metadata):
    guardian, actor = system
    run, _ = analyzed(system, metadata)
    assert run["status"] == "succeeded"
    assert run["candidate_commitments"][0]["status"] == "proposed"
    assert run["findings"][0]["kind"] == "acceptance_gap"
    validate("AnalysisRun", run)
    validate("Workspace", guardian.workspace(actor, "p"))


@pytest.mark.parametrize(
    "mutation", ["foreign_source", "bad_quote", "bad_kind", "foreign_owner", "extra_action"]
)
def test_invalid_provider_output_fails_closed(system, metadata, mutation):
    class InvalidProvider(TestProvider):
        async def analyze(self, packet, attempt_id):
            result = await super().analyze(packet, attempt_id)
            if mutation == "foreign_source":
                result["findings"][0]["evidence"][0]["source_version_id"] = "foreign"
            elif mutation == "bad_quote":
                result["findings"][0]["evidence"][0]["quote"] = "fabricated"
            elif mutation == "bad_kind":
                result["findings"][0]["kind"] = "approve_scope"
            elif mutation == "foreign_owner":
                result["proposed_commitments"][0]["owner_id"] = "foreign"
            else:
                result["action"] = {"approve": True}
            return result

    guardian, actor = system
    run, _ = analyzed(system, metadata, InvalidProvider())
    assert run["status"] == "failed"
    assert not guardian.workspace(actor, "p")["findings"]


def test_evidence_changes_during_inference_make_result_historical(system, metadata):
    guardian, actor = system

    def change():
        project = guardian.repo.get("projects/p")
        project["evidence_revision"] += 1
        guardian.repo.set("projects/p", project)

    run, _ = analyzed(system, metadata, TestProvider(hook=change))
    assert run["status"] == "stale" and run["findings"]
    assert not guardian.workspace(actor, "p")["findings"]


def test_provider_timeout_never_falls_back_and_respects_attempt_budget(system, metadata):
    guardian, actor = system
    provider = TestProvider(result=TimeoutError())
    run, runner = analyzed(system, metadata, provider)
    job_id = guardian.repo.get(f"projects/p/analyses/{run['id']}")["_job_id"]
    with pytest.raises(AppError, match="TEMPORARY_DEPENDENCY_FAILURE"):
        asyncio.run(runner.run(job_id, 2))
    asyncio.run(runner.run(job_id, 3))
    final = guardian.analysis(actor, "p", run["id"])
    assert final["status"] == "failed" and not final["findings"]
    assert guardian.repo.get(f"jobs/{job_id}")["attempt_count"] == provider.calls == 3


def test_approval_does_not_create_task_and_execution_does_not_close_finding(system, metadata):
    guardian, actor = system
    action, _ = approved(system, metadata)
    assert not guardian.repo.list("projects/p/tasks")
    result = guardian.execute(actor, "p", action["id"], action["binding"], "execute-key-0001")
    assert result["task"]["status"] == "open" and not result["replayed"]
    assert guardian.repo.list("projects/p/findings")[0]["status"] == "open"
    assert guardian.repo.list("projects/p/commitments")[0]["status"] == "proposed"
    validate("ExecutionResult", result)


def test_concurrent_test_double_execution_returns_one_task_and_audit(system, metadata):
    guardian, actor = system
    action, _ = approved(system, metadata)

    def execute(i):
        return guardian.execute(actor, "p", action["id"], action["binding"], f"execute-key-{i:04}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, range(4)))
    assert sum(not result["replayed"] for result in results) == 1
    assert len(guardian.repo.list("projects/p/tasks")) == 1
    assert guardian.repo.get("projects/p")["board_revision"] == 1
    assert (
        sum(
            event["event_type"] == "blocker_task_created"
            for event in guardian.repo.list("projects/p/audit_events")
        )
        == 1
    )


@pytest.mark.parametrize("change", ["evidence", "board", "finding", "approver", "owner", "business_date"])
def test_changed_binding_or_authority_commits_stale_marker_without_task(system, metadata, change):
    guardian, actor = system
    action, _ = approved(system, metadata)
    if change in {"evidence", "board", "business_date"}:
        project = guardian.repo.get("projects/p")
        if change == "business_date":
            project["business_clock"]["as_of"] = "2026-10-19T12:00:00Z"
        else:
            project[f"{change}_revision"] += 1
        guardian.repo.set("projects/p", project)
    elif change == "finding":
        finding_path = f"projects/p/findings/{action['payload']['finding_id']}"
        finding = guardian.repo.get(finding_path)
        finding["version"] += 1
        guardian.repo.set(finding_path, finding)
    elif change == "approver":
        # A separate currently authorized executor tests a revoked original approver.
        from conftest import populate

        original_project = guardian.repo.get("projects/p")
        executor = populate(guardian.repo, user_id="executor")
        guardian.repo.set("projects/p", original_project)
        binding = guardian.repo.get(f"identity_bindings/{actor.binding_id}")
        binding["enabled"] = False
        guardian.repo.set(f"identity_bindings/{actor.binding_id}", binding)
        actor = executor
    else:
        # Keep executor/approver reviewer, move owner to a removed membership.
        stored = guardian.repo.get(f"projects/p/action_proposals/{action['id']}")
        stored["payload"]["owner_id"] = "removed"
        stored["binding"]["payload_hash"] = digest(stored["payload"])
        from handoff_guardian.contracts import canonical

        stored["_payload_bytes"] = canonical(stored["payload"]).decode()
        stored["approval"]["binding"] = stored["binding"]
        guardian.repo.set(f"projects/p/action_proposals/{action['id']}", stored)
        action = stored
    with pytest.raises(AppError, match="STALE_"):
        guardian.execute(actor, "p", action["id"], action["binding"], "execute-key-0001")
    assert not guardian.repo.list("projects/p/tasks")
    assert guardian.repo.get(f"projects/p/action_proposals/{action['id']}")["status"] == "stale"


def test_executed_replay_survives_new_board_revision(system, metadata):
    guardian, actor = system
    action, _ = approved(system, metadata)
    first = guardian.execute(actor, "p", action["id"], action["binding"], "execute-key-0001")
    project = guardian.repo.get("projects/p")
    project["board_revision"] += 1
    project["evidence_revision"] += 1
    guardian.repo.set("projects/p", project)
    replay = guardian.execute(actor, "p", action["id"], action["binding"], "execute-key-0002")
    assert replay["replayed"] and replay["task"] == first["task"]


def test_changed_execution_key_body_conflicts(system, metadata):
    guardian, actor = system
    action, _ = approved(system, metadata)
    guardian.execute(actor, "p", action["id"], action["binding"], "execute-key-0001")
    binding = dict(action["binding"], base_board_revision=99)
    with pytest.raises(AppError, match="IDEMPOTENCY_CONFLICT"):
        guardian.execute(actor, "p", action["id"], binding, "execute-key-0001")


def test_ready_source_cannot_be_withdrawn(system, metadata):
    guardian, actor = system
    source, _ = ready_source(system, metadata)
    with pytest.raises(AppError, match="INVALID_STATE_TRANSITION"):
        guardian.remedy_source(
            actor,
            "p",
            source["id"],
            {"expected_evidence_revision": 2, "reason": "remove"},
            "withdraw-key-0001",
            True,
        )


def test_closure_requires_reviewed_acceptance_evidence(system, metadata):
    guardian, actor = system
    run, _ = analyzed(system, metadata)
    commitment = run["candidate_commitments"][0]
    request = {
        "decision": "close",
        "reason": "Done",
        "evidence": commitment["evidence"],
        "expected_commitment_version": 1,
        "expected_evidence_revision": 2,
        "supersedes_commitment_id": None,
    }
    with pytest.raises(AppError, match="INVALID_EVIDENCE"):
        guardian.review_entity(actor, "p", commitment["id"], request, True)


def test_cyclic_source_lineage_is_rejected(system, metadata):
    guardian, actor = system
    source, _ = ready_source(system, metadata)
    with pytest.raises(AppError, match="INVALID_STATE_TRANSITION"):
        guardian.review_source(
            actor,
            "p",
            source["id"],
            {
                "authority": "accepted_baseline",
                "effective_at": "2026-10-01T00:00:00Z",
                "supersedes_source_version_id": source["id"],
                "reason": "Reviewed",
                "expected_evidence_revision": 2,
            },
        )


def next_analysis(system, runner, key="analysis-next-0001"):
    guardian, actor = system
    revision = guardian.repo.get("projects/p")["evidence_revision"]
    run = guardian.start_analysis(
        actor, "p", {"expected_evidence_revision": revision, "evaluation_as_of": "2026-10-18T12:00:00Z"}, key
    )
    job_id = guardian.repo.get(f"projects/p/analyses/{run['id']}")["_job_id"]
    asyncio.run(runner.run(job_id, 1))
    return guardian.analysis(actor, "p", run["id"])


def test_clean_reanalysis_preserves_historical_open_finding(system, metadata):
    guardian, actor = system
    original, runner = analyzed(system, metadata)
    runner.provider = TestProvider({"proposed_commitments": [], "findings": [], "coverage_warnings": []})
    clean = next_analysis(system, runner)
    assert clean["status"] == "succeeded" and clean["findings"] == []
    assert guardian.workspace(actor, "p")["findings"][0]["id"] == original["findings"][0]["id"]
    assert guardian.repo.get("projects/p")["_open_finding_count"] == 1


def test_exact_grounding_reanalysis_preserves_reviewed_disposition_and_observation_history(system, metadata):
    guardian, actor = system
    original, runner = analyzed(system, metadata)
    finding = original["findings"][0]
    guardian.review_entity(
        actor,
        "p",
        finding["id"],
        {
            "decision": "dismiss",
            "reason": "Reviewed exception",
            "evidence": [],
            "expected_finding_version": 1,
            "expected_evidence_revision": 2,
        },
    )
    new = next_analysis(system, runner)
    observed = new["findings"][0]
    assert observed["id"] == finding["id"] and observed["status"] == "dismissed" and observed["version"] == 3
    assert guardian.analysis(actor, "p", original["id"])["findings"][0]["status"] == "open"
    assert guardian.repo.get("projects/p")["_open_finding_count"] == 0
    assert guardian.repo.get("projects/p")["_current_reviewed_finding_count"] == 1
    assert len(guardian.workspace(actor, "p")["findings"]) == 1
    assert any(
        event["event_type"] == "finding_observed_again"
        for event in guardian.repo.list("projects/p/audit_events")
    )


def test_withdrawn_failed_source_is_preserved_in_coverage_warnings(system, metadata):
    guardian, actor = system
    _, runner = ready_source(system, metadata)
    metadata = dict(metadata, document_id="broken", expected_evidence_revision=2)
    failed = guardian.upload(actor, "p", metadata, b"\xff", "broken.txt", "text/plain", "broken-upload-0001")
    job_id = guardian.repo.get(f"projects/p/sources/{failed['id']}")["_job_id"]
    asyncio.run(runner.run(job_id, 1))
    request = {"reason": "Unusable export; preserve and replace later", "expected_evidence_revision": 4}
    result = guardian.remedy_source(actor, "p", failed["id"], request, "withdraw-key-0001", True)
    assert result["source"]["ingestion_status"] == "failed" and result["source"]["withdrawn_at"]
    assert guardian.remedy_source(actor, "p", failed["id"], request, "withdraw-key-0001", True) == result
    run = next_analysis(system, runner)
    assert run["status"] == "succeeded" and failed["id"] in " ".join(run["warnings"])
    assert guardian.original(actor, "p", failed["id"])[0] == b"\xff"


@pytest.mark.parametrize("attempts", [1, 3])
def test_expired_lease_recovery_preserves_attempt_budget(system, metadata, attempts):
    guardian, actor = system
    uploaded(system, metadata)
    job = guardian.repo.list("jobs")[0]
    runner = JobRunner(guardian, TextOnlyExtractor(), TestProvider())
    runner.claim(job["id"], 1)
    current = guardian.repo.get(f"jobs/{job['id']}")
    current.update(lease_until="2000-01-01T00:00:00Z", attempt_count=attempts)
    guardian.repo.set(f"jobs/{job['id']}", current)
    counts = runner.recover()
    recovered = guardian.repo.get(f"jobs/{job['id']}")
    assert recovered["attempt_count"] == attempts and counts["expired_jobs"] == 1
    assert recovered["status"] == ("pending" if attempts == 1 else "failed")


def test_reviewer_accepts_then_closes_commitment_with_separate_reviewed_evidence(system, metadata):
    guardian, actor = system
    run, runner = analyzed(system, metadata)
    commitment = run["candidate_commitments"][0]
    source = guardian.repo.list("projects/p/sources")[0]
    guardian.review_source(
        actor,
        "p",
        source["id"],
        {
            "authority": "accepted_baseline",
            "effective_at": "2026-10-01T00:00:00Z",
            "supersedes_source_version_id": None,
            "reason": "Scope owner confirmed baseline",
            "expected_evidence_revision": 2,
        },
    )
    accepted = guardian.review_entity(
        actor,
        "p",
        commitment["id"],
        {
            "decision": "accept",
            "reason": "Confirmed scope",
            "evidence": commitment["evidence"],
            "expected_commitment_version": 1,
            "expected_evidence_revision": 3,
            "supersedes_commitment_id": None,
        },
        True,
    )
    assert accepted["commitment"]["status"] == "accepted"
    confirmation = b"Acceptance reviewer confirmed all delivery criteria satisfied."
    uploaded_source = guardian.upload(
        actor,
        "p",
        dict(
            metadata,
            document_id="acceptance",
            source_kind="acceptance_checklist",
            expected_evidence_revision=4,
        ),
        confirmation,
        "acceptance.txt",
        "text/plain",
        "acceptance-upload-0001",
    )
    job_id = guardian.repo.get(f"projects/p/sources/{uploaded_source['id']}")["_job_id"]
    asyncio.run(runner.run(job_id, 1))
    guardian.review_source(
        actor,
        "p",
        uploaded_source["id"],
        {
            "authority": "acceptance_evidence",
            "effective_at": "2026-10-18T00:00:00Z",
            "supersedes_source_version_id": None,
            "reason": "Accountable reviewer confirmed criteria",
            "expected_evidence_revision": 6,
        },
    )
    closed = guardian.review_entity(
        actor,
        "p",
        commitment["id"],
        {
            "decision": "close",
            "reason": "Reviewer confirmation establishes all listed criteria were satisfied",
            "evidence": [
                {
                    "source_version_id": uploaded_source["id"],
                    "start_char": 0,
                    "end_char": len(confirmation),
                    "quote": confirmation.decode(),
                }
            ],
            "expected_commitment_version": 2,
            "expected_evidence_revision": 7,
            "supersedes_commitment_id": None,
        },
        True,
    )
    assert closed["commitment"]["status"] == "closed" and closed["project"]["evidence_revision"] == 8
    assert guardian.repo.list("projects/p/findings")[0]["status"] == "open"
