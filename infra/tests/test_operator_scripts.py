"""Offline operator/runner boundary tests. These doubles are never cloud evidence."""

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bootstrap_reviewer as bootstrap  # noqa: E402
import run_cloud_acceptance as acceptance  # noqa: E402
import seed_demo as seed  # noqa: E402


class Snapshot:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value.copy() if self.value else None


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return Ref(self.db, self.path + "/" + name)

    def document(self, name):
        return Ref(self.db, self.path + "/" + name)

    def get(self, transaction):
        assert not transaction.writes, "Firestore must read all documents before writes"
        return Snapshot(self.db.docs.get(self.path))


class Transaction:
    def __init__(self, db):
        self.db, self.writes = db, []

    def set(self, ref, value):
        self.writes.append((ref.path, value))

    def create(self, ref, value):
        assert ref.path not in self.db.docs
        self.writes.append((ref.path, value))


class DB:
    def __init__(self, project=None):
        self.docs = {"projects/p": project} if project else {}

    def collection(self, name):
        return Ref(self, name)

    def transaction(self, max_attempts):
        assert max_attempts == 5
        return Transaction(self)


@pytest.fixture
def fake_firestore(monkeypatch):
    from google.cloud import firestore

    def transactional(function):
        def apply(transaction):
            result = function(transaction)
            transaction.db.docs.update(transaction.writes)
            return result

        return apply

    monkeypatch.setattr(firestore, "transactional", transactional)


def project():
    return {
        "id": "p",
        "name": "Synthetic",
        "timezone": "UTC",
        "_synthetic": True,
        "business_clock": {"mode": "synthetic", "as_of": "2026-10-22T12:00:00Z"},
        "evidence_revision": 4,
        "board_revision": 2,
    }


def clock_args(**changes):
    values = dict(
        google_cloud_project="hg-offline-tests",
        database="(default)",
        project_id="p",
        as_of="2026-10-22T12:00:00.123Z",
        expected_evidence_revision=4,
        operator_id="test-operator",
    )
    return SimpleNamespace(**(values | changes))


def test_clock_advance_uses_real_audit_time_and_invalidates_evidence(monkeypatch, fake_firestore):
    db = DB(project())
    monkeypatch.setattr(seed, "cloud_client", lambda *args: db)
    result = seed.advance(clock_args())
    assert result["evidence_revision"] == 5
    assert db.docs["projects/p"]["board_revision"] == 2
    assert db.docs["projects/p"]["business_clock"]["as_of"] == "2026-10-22T12:00:00.123Z"
    events = [value for key, value in db.docs.items() if "/audit_events/" in key]
    assert len(events) == 1
    assert events[0]["event_type"] == "synthetic_business_clock_advanced"
    assert datetime.fromisoformat(events[0]["occurred_at"]) != datetime.fromisoformat(
        result["business_clock"]["as_of"]
    )


@pytest.mark.parametrize(
    "changes,args",
    [
        ({"_synthetic": False}, {}),
        ({"business_clock": {"mode": "real", "as_of": "2026-10-22T12:00:00Z"}}, {}),
        ({}, {"expected_evidence_revision": 3}),
        ({}, {"as_of": "2026-10-21T12:00:00Z"}),
    ],
)
def test_clock_rejects_unsafe_or_stale_changes(monkeypatch, fake_firestore, changes, args):
    db = DB(project() | changes)
    original = db.docs.copy()
    monkeypatch.setattr(seed, "cloud_client", lambda *unused: db)
    with pytest.raises(ValueError):
        seed.advance(clock_args(**args))
    assert db.docs == original


def test_synthetic_seed_is_repeatable_and_has_empty_capacity_counters(monkeypatch, fake_firestore):
    db = DB()
    monkeypatch.setattr(seed, "cloud_client", lambda *args: db)
    args = SimpleNamespace(
        scenario="scn_01",
        project_id="p",
        google_cloud_project="hg-offline-tests",
        database="(default)",
        operator_id="test-operator",
    )
    first = seed.seed(args)
    second = seed.seed(args)
    assert first["changed"] and not second["changed"]
    assert db.docs["projects/p"]["evidence_revision"] == 0
    for counter in (
        "_source_count",
        "_task_count",
        "_commitment_count",
        "_current_finding_count",
        "_open_finding_count",
        "_current_reviewed_finding_count",
        "_action_count",
    ):
        assert db.docs["projects/p"][counter] == 0
    assert not any("memberships" in key or "sources" in key for key in db.docs)


def test_operator_commands_reject_emulator_before_adc(monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:9999")
    import google.auth

    def forbidden_adc(**kwargs):
        pytest.fail("ADC must not be resolved with an emulator endpoint override")

    monkeypatch.setattr(google.auth, "default", forbidden_adc)
    with pytest.raises(ValueError, match="emulator"):
        bootstrap.cloud_client("hg-offline-tests", "(default)")


def test_generated_pdf_is_real_and_retains_full_selected_text():
    text = "Synthetic Harbour statement\nPhase one staff portal.\nSSO is phase two."
    reader = PdfReader(io.BytesIO(acceptance.pdf_bytes(text, pages=15)), strict=True)
    assert len(reader.pages) == 15
    extracted = reader.pages[0].extract_text()
    assert all(line in extracted for line in text.splitlines())


def test_cloud_runner_requires_live_flag_and_returns_machine_readable_not_run(monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["runner", "--all", "--report", str(report)])
    monkeypatch.setattr(acceptance.Runner, "run", lambda *args: pytest.fail("Live probes must not run"))
    assert acceptance.main() == 1
    result = json.loads(report.read_text())
    assert len(result["cases"]) == 24
    assert {case["status"] for case in result["cases"]} == {"not_run"}
    assert result["live_authorized"] is False


def test_cloud_runner_missing_live_inputs_is_blocked_without_request(monkeypatch, tmp_path):
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        sys, "argv", ["runner", "--case", "AGENT-01", "--allow-live", "--report", str(report)]
    )
    monkeypatch.setattr(
        acceptance.urllib.request.OpenerDirector,
        "open",
        lambda *args, **kwargs: pytest.fail("No endpoint/model may be called without inputs"),
    )
    assert acceptance.main() == 1
    result = json.loads(report.read_text())
    assert result["cases"][0]["status"] == "blocked"
    assert "--agent-resource" in result["cases"][0]["reason"]


@pytest.mark.parametrize("value", ["../escape", "_invalid", "source/id", "id\\escape"])
def test_operator_ids_cannot_escape_firestore_paths(value):
    with pytest.raises(argparse.ArgumentTypeError):
        bootstrap.identifier(value)
