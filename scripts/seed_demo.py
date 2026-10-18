#!/usr/bin/env python3
"""Trusted synthetic project bootstrap and monotonic business-clock advancement.

Project creation never imports sources, accepted authority, canned model results,
foreign-project material or fixture memberships. Upload/review evidence through
the real signed-in API. Optional commitment bootstrap imports only explicitly
proposed commitments with evidence already present in the deployed project.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bootstrap_reviewer import audit, cloud_client, identifier


def parse_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Supply an RFC3339 timestamp with UTC offset.") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Synthetic clock timestamps need a timezone.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def selected_fixture(args) -> dict:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "fixtures/inputs/manifest.json").read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True:
        raise ValueError("The trusted fixture inventory is not marked synthetic.")
    scenario = next((item for item in manifest["scenarios"] if item["id"] == args.scenario), None)
    if scenario is None:
        raise ValueError("Unknown explicitly selected fixture scenario.")
    project = next(item for item in manifest["projects"] if item["id"] == scenario["project_id"])
    return {
        "id": args.project_id or project["id"],
        "name": project["name"],
        "timezone": project["timezone"],
        "business_clock": {"mode": "synthetic", "as_of": parse_time(scenario["evaluation_as_of"])},
        "evidence_revision": 0,
        "board_revision": 0,
        "_synthetic": True,
        "_scenario_id": args.scenario,
        "_source_count": 0,
        "_task_count": 0,
        "_commitment_count": 0,
        "_current_finding_count": 0,
        "_open_finding_count": 0,
        "_current_reviewed_finding_count": 0,
        "_action_count": 0,
    }


def seed(args) -> dict:
    from google.cloud import firestore

    desired = selected_fixture(args)
    ZoneInfo(desired["timezone"])
    db = cloud_client(args.google_cloud_project, args.database)
    ref = db.collection("projects").document(desired["id"])
    event = audit(
        desired,
        args.operator_id,
        "synthetic_project_created",
        desired["id"],
        "Trusted synthetic project bootstrap; no source authority or login membership imported.",
    )

    @firestore.transactional
    def commit(transaction):
        existing = ref.get(transaction=transaction).to_dict()
        if existing:
            if existing.get("_synthetic") is not True or existing.get("_scenario_id") != args.scenario:
                raise ValueError("Refusing to overwrite a real project or another synthetic scenario.")
            return {
                "changed": False,
                "project_id": desired["id"],
                "business_clock": existing["business_clock"],
            }
        transaction.create(ref, desired)
        transaction.create(ref.collection("audit_events").document(event["id"]), event)
        return {
            "changed": True,
            "project_id": desired["id"],
            "business_clock": desired["business_clock"],
            "audit_id": event["id"],
        }

    return commit(db.transaction(max_attempts=5))


def advance(args) -> dict:
    from google.cloud import firestore

    db = cloud_client(args.google_cloud_project, args.database)
    ref = db.collection("projects").document(args.project_id)

    @firestore.transactional
    def commit(transaction):
        project = ref.get(transaction=transaction).to_dict()
        if (
            not project
            or project.get("_synthetic") is not True
            or project["business_clock"]["mode"] != "synthetic"
        ):
            raise ValueError(
                "Only an explicitly provisioned synthetic project can advance its business clock."
            )
        if project["evidence_revision"] != args.expected_evidence_revision:
            raise ValueError(
                "Evidence revision changed; inspect the project and supply its current revision."
            )
        if datetime.fromisoformat(project["business_clock"]["as_of"]) >= datetime.fromisoformat(args.as_of):
            raise ValueError("Business-clock advance must move forward, never repeat or rewind.")
        project["business_clock"] = {"mode": "synthetic", "as_of": args.as_of}
        project["evidence_revision"] += 1
        event = audit(
            project,
            args.operator_id,
            "synthetic_business_clock_advanced",
            args.project_id,
            "Trusted operator advanced synthetic time; prior proposal evidence bindings are stale.",
        )
        transaction.set(ref, project)
        transaction.create(ref.collection("audit_events").document(event["id"]), event)
        return {
            "project_id": args.project_id,
            "evidence_revision": project["evidence_revision"],
            "business_clock": project["business_clock"],
            "audit_id": event["id"],
        }

    return commit(db.transaction(max_attempts=5))


def commitments(args) -> dict:
    import hashlib

    from google.cloud import firestore, storage

    from handoff_guardian.contracts import validate

    if args.file.stat().st_size > 256 * 1024:
        raise ValueError("Commitment bootstrap exceeds 256 KiB.")
    items = json.loads(args.file.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("Supply 1–50 proposed Commitment records with current runtime evidence IDs.")
    db = cloud_client(args.google_cloud_project, args.database)
    ref = db.collection("projects").document(args.project_id)
    bucket = storage.Client(project=args.google_cloud_project).bucket(args.evidence_bucket)
    source_snapshots = {}
    for item in items:
        validate("Commitment", item)
        if item["project_id"] != args.project_id or item["status"] != "proposed" or item["version"] != 1:
            raise ValueError(
                "Bootstrap only permits version-one proposed commitments for the selected project."
            )
        for span in item["evidence"]:
            source_id = span["source_version_id"]
            identifier(source_id)
            source = ref.collection("sources").document(source_id).get().to_dict()
            if not source or source["ingestion_status"] != "ready" or source.get("withdrawn_at"):
                raise ValueError("Commitment evidence must reference ready evidence in this project.")
            canonical_ref = source.get("_canonical")
            if not canonical_ref:
                raise ValueError("Source has no immutable canonical object reference.")
            data = bucket.blob(
                canonical_ref["name"], generation=canonical_ref["generation"]
            ).download_as_bytes(if_generation_match=canonical_ref["generation"], timeout=60)
            if hashlib.sha256(data).hexdigest() != canonical_ref["sha256"]:
                raise ValueError("Canonical object hash mismatch.")
            text = data.decode("utf-8")
            if (
                span["start_char"] < 0
                or span["end_char"] <= span["start_char"]
                or text[span["start_char"] : span["end_char"]] != span["quote"]
            ):
                raise ValueError("Bootstrap evidence does not exactly anchor immutable canonical text.")
            source_snapshots[source_id] = source

    @firestore.transactional
    def commit(transaction):
        project = ref.get(transaction=transaction).to_dict()
        existing = [
            ref.collection("commitments").document(item["id"]).get(transaction=transaction).to_dict()
            for item in items
        ]
        fresh_sources = {
            sid: ref.collection("sources").document(sid).get(transaction=transaction).to_dict()
            for sid in source_snapshots
        }
        if not project or project.get("_synthetic") is not True:
            raise ValueError("Commitment bootstrap requires an explicitly synthetic project.")
        if (
            project["evidence_revision"] != args.expected_evidence_revision
            or fresh_sources != source_snapshots
        ):
            raise ValueError("Project evidence changed while verifying canonical anchors.")
        if all(old == item for old, item in zip(existing, items, strict=True)):
            return {"changed": False, "count": len(items)}
        if any(old is not None for old in existing):
            raise ValueError("Commitment IDs already exist; refusing overwrite.")
        if project.get("_commitment_count", 0) + len(items) > 100:
            raise ValueError("Project commitment capacity would exceed 100.")
        project["_commitment_count"] = project.get("_commitment_count", 0) + len(items)
        project["evidence_revision"] += 1
        event = audit(
            project,
            args.operator_id,
            "synthetic_commitments_proposed",
            args.project_id,
            "Trusted operator seeded proposed commitments with verified immutable runtime anchors.",
        )
        for item in items:
            transaction.create(ref.collection("commitments").document(item["id"]), item)
        transaction.set(ref, project)
        transaction.create(ref.collection("audit_events").document(event["id"]), event)
        return {
            "changed": True,
            "count": len(items),
            "evidence_revision": project["evidence_revision"],
            "audit_id": event["id"],
        }

    return commit(db.transaction(max_attempts=5))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--google-cloud-project", required=True, type=identifier)
    parser.add_argument("--database", default="(default)")
    parser.add_argument("--operator-id", required=True, type=identifier)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Idempotently create one selected synthetic scenario project.")
    init.add_argument("--scenario", default="scn_01", type=identifier)
    init.add_argument("--project-id", type=identifier)
    clock = commands.add_parser("advance-clock")
    clock.add_argument("--project-id", required=True, type=identifier)
    clock.add_argument("--as-of", required=True, type=parse_time)
    clock.add_argument("--expected-evidence-revision", required=True, type=int)
    proposed = commands.add_parser("propose-commitments")
    proposed.add_argument("--project-id", required=True, type=identifier)
    proposed.add_argument("--file", required=True, type=Path)
    proposed.add_argument("--evidence-bucket", required=True)
    proposed.add_argument("--expected-evidence-revision", required=True, type=int)
    args = parser.parse_args()
    try:
        result = {"init": seed, "advance-clock": advance, "propose-commitments": commitments}[args.command](
            args
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 -- sanitize remote errors at the trusted CLI boundary
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "Trusted operator command failed; check ADC, IAM, project and validated input.",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
