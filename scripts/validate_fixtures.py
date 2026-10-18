#!/usr/bin/env python3
"""Validate the synthetic starter pack using Python's standard library only.

This checks artifact structure and internal consistency. It does not run the
application, call a model, or measure model accuracy or runtime security.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo


class InvalidFixture(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidFixture(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidFixture(f"Cannot read JSON {path}: {exc}") from exc


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0, f"Not UTC: {value}")
        return parsed
    except (ValueError, AttributeError) as exc:
        raise InvalidFixture(f"Invalid timestamp: {value}") from exc


def validate_schema(value, rule, schema, location="$", depth=0):
    """Implement precisely the JSON Schema keywords used by fixtures.schema.json.

    This is not a general-purpose JSON Schema implementation. Unknown validation
    keywords fail closed so a schema change cannot silently weaken this check.
    """
    require(depth < 80, f"Schema recursion too deep at {location}")
    allowed = {"$schema", "$id", "$defs", "$ref", "title", "description", "type", "required", "properties", "additionalProperties", "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "enum", "const", "minimum", "maximum", "anyOf"}
    require(not (set(rule) - allowed), f"Unsupported schema keywords at {location}: {set(rule) - allowed}")
    if "$ref" in rule:
        ref = rule["$ref"]
        require(ref.startswith("#/$defs/"), f"External schema reference forbidden: {ref}")
        return validate_schema(value, schema["$defs"][ref.removeprefix("#/$defs/")], schema, location, depth + 1)
    if "anyOf" in rule:
        errors = []
        for alternative in rule["anyOf"]:
            try:
                validate_schema(value, alternative, schema, location, depth + 1)
                return
            except InvalidFixture as exc:
                errors.append(str(exc))
        raise InvalidFixture(f"No allowed schema matches {location}: {'; '.join(errors)}")
    if "const" in rule:
        require(type(value) is type(rule["const"]) and value == rule["const"], f"Wrong fixed value at {location}")
    if "enum" in rule:
        require(any(type(value) is type(item) and value == item for item in rule["enum"]), f"Unsupported enum value at {location}: {value!r}")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if "type" in rule:
        require(rule["type"] in types, f"Unsupported type in schema: {rule['type']}")
        require(type(value) is types[rule["type"]], f"Expected {rule['type']} at {location}")
    if isinstance(value, dict):
        require(set(rule.get("required", [])) <= set(value), f"Missing required keys at {location}: {set(rule.get('required', [])) - set(value)}")
        properties = rule.get("properties", {})
        additional = rule.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], schema, f"{location}.{key}", depth + 1)
            elif additional is False:
                raise InvalidFixture(f"Unknown key at {location}.{key}")
            elif isinstance(additional, dict):
                validate_schema(item, additional, schema, f"{location}.{key}", depth + 1)
    if isinstance(value, list):
        require(len(value) >= rule.get("minItems", 0), f"Too few items at {location}")
        require(len(value) <= rule.get("maxItems", len(value)), f"Too many items at {location}")
        if rule.get("uniqueItems"):
            require(len({canonical_json(item) for item in value}) == len(value), f"Duplicate items at {location}")
        for index, item in enumerate(value):
            if "items" in rule:
                validate_schema(item, rule["items"], schema, f"{location}[{index}]", depth + 1)
    if isinstance(value, str):
        require(len(value) >= rule.get("minLength", 0), f"String too short at {location}")
        require(len(value) <= rule.get("maxLength", len(value)), f"String too long at {location}")
        if "pattern" in rule:
            require(re.search(rule["pattern"], value) is not None, f"Pattern mismatch at {location}: {value!r}")
    if type(value) is int:
        require(value >= rule.get("minimum", value), f"Below minimum at {location}")
        require(value <= rule.get("maximum", value), f"Above maximum at {location}")


def index_unique(rows, key, label):
    result = {}
    for row in rows:
        require(row[key] not in result, f"Duplicate {label}: {row[key]}")
        result[row[key]] = row
    return result


def safe_path(root, relative, allowed_prefixes):
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, f"Unsafe fixture path: {relative}")
    require(any(relative.startswith(prefix) for prefix in allowed_prefixes), f"Disallowed fixture subtree: {relative}")
    resolved = (root / path).resolve()
    require(resolved.is_relative_to(root.resolve()), f"Path escapes kit root: {relative}")
    require(any(resolved.is_relative_to((root / prefix).resolve()) for prefix in allowed_prefixes), f"Symlink escapes allowed subtree: {relative}")
    require(resolved.is_file(), f"Missing fixture file: {relative}")
    return resolved


def check(root):
    schema = load_json(root / "contracts/fixtures.schema.json")
    manifest = load_json(root / "fixtures/inputs/manifest.json")
    validate_schema(manifest, schema, schema)
    users = index_unique(manifest["users"], "id", "user ID")
    projects = index_unique(manifest["projects"], "id", "project ID")
    sources = index_unique(manifest["source_versions"], "id", "source version ID")
    scenarios = index_unique(manifest["scenarios"], "id", "scenario ID")
    texts, memberships, seen_versions, source_paths = {}, {}, set(), set()
    for project in projects.values():
        try:
            # UTC is intrinsic; some Python installations lack the optional
            # system IANA timezone database, particularly on Windows.
            if project["timezone"] != "UTC":
                ZoneInfo(project["timezone"])
        except Exception as exc:
            raise InvalidFixture(f"Invalid project timezone: {project['timezone']}") from exc
        members = index_unique(project["memberships"], "user_id", "membership")
        require(set(members) <= set(users), f"Unknown project member in {project['id']}")
        require(any(m["role"] == "reviewer" for m in members.values()), f"No reviewer for {project['id']}")
        memberships[project["id"]] = {uid: row["role"] for uid, row in members.items()}
    for source in sources.values():
        sid, pid = source["id"], source["project_id"]
        require(pid in projects, f"Unknown source project: {sid}")
        version_key = (pid, source["document_id"], source["version_number"])
        require(version_key not in seen_versions, f"Duplicate logical version: {version_key}")
        seen_versions.add(version_key)
        require(source["path"] not in source_paths, f"Two source IDs use one file: {source['path']}")
        source_paths.add(source["path"])
        path = safe_path(root, source["path"], ("fixtures/inputs/sources/", "fixtures/security_boundary/sources/"))
        raw = path.read_bytes()
        require(b"\r" not in raw, f"Noncanonical newline in {sid}")
        text = raw.decode("utf-8")
        require(text.startswith("SYNTHETIC FIXTURE") and text.endswith("\n"), f"Missing synthetic marker or final newline: {sid}")
        require(f"Project: {pid}\n" in text and f"Source version ID: {sid}\n" in text, f"Source metadata disagrees with file: {sid}")
        require(f"Document ID: {source['document_id']}\n" in text, f"Logical document ID mismatch: {sid}")
        require(sha256(raw) == source["content_sha256"], f"Original-byte hash mismatch: {sid}")
        require(sha256(text.encode("utf-8")) == source["canonical_text_sha256"], f"Canonical hash mismatch: {sid}")
        for address in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", text):
            require(address.rstrip(".").endswith(".test"), f"Non-synthetic email in {sid}: {address}")
        timestamp(source["created_at"])
        if source["effective_at"] is not None:
            timestamp(source["effective_at"])
        if source["authority"] == "unreviewed":
            require(source["authority_confirmed_by"] is None, f"Unreviewed source names a confirming actor: {sid}")
        else:
            require(memberships[pid].get(source["authority_confirmed_by"]) == "reviewer", f"Source lacks authorized authority reviewer: {sid}")
        if source["authority"] == "accepted_baseline":
            require(source["effective_at"] is not None, f"Accepted baseline lacks effective time: {sid}")
        texts[sid] = text
    for source in sources.values():
        target_id = source["supersedes_source_version_id"]
        if target_id is not None:
            require(target_id in sources, f"Missing superseded source: {target_id}")
            target = sources[target_id]
            require(source["project_id"] == target["project_id"] and source["document_id"] == target["document_id"], f"Cross-project/document source supersession: {source['id']}")
            require(source["version_number"] > target["version_number"], f"Non-forward source supersession: {source['id']}")
        visited, current = set(), source["id"]
        while current:
            require(current not in visited, f"Supersession cycle at {current}")
            visited.add(current)
            current = sources[current]["supersedes_source_version_id"]

    def active_baselines(ids, as_of, authority=None, effective=None, supersedes=None):
        authority, effective, supersedes = authority or {}, effective or {}, supersedes or {}
        active = {sid for sid in ids if authority.get(sid, sources[sid]["authority"]) == "accepted_baseline"
                  and effective.get(sid, sources[sid]["effective_at"]) is not None
                  and timestamp(effective.get(sid, sources[sid]["effective_at"])) <= timestamp(as_of)}
        # Walk the entire explicit chain: an active replacement replaces its ancestors.
        for sid in list(active):
            previous = supersedes.get(sid, sources[sid]["supersedes_source_version_id"])
            while previous:
                active.discard(previous)
                previous = supersedes.get(previous, sources[previous]["supersedes_source_version_id"])
        return active

    span_count = 0
    def check_span(evidence, allowed_ids, pid):
        nonlocal span_count
        validate_schema(evidence, schema["$defs"]["evidenceSpan"], schema)
        sid = evidence["source_version_id"]
        require(sid in allowed_ids and sid in sources, f"Evidence outside authorized snapshot: {sid}")
        require(sources[sid]["project_id"] == pid, f"Cross-project evidence span: {sid}")
        start, end = evidence["start_char"], evidence["end_char"]
        require(0 <= start < end <= len(texts[sid]), f"Invalid Unicode code-point range: {sid}")
        require(texts[sid][start:end] == evidence["quote"], f"Quote does not match canonical source slice: {sid}")
        span_count += 1

    mocks, mock_points = {}, {}
    for path in sorted((root / "fixtures/mock").glob("*.canned.json")):
        mock = load_json(path)
        validate_schema(mock, schema["$defs"]["mockFile"], schema, path.name)
        snapshot = mock["supported_snapshot"]
        require(sha256(canonical_json(snapshot)) == mock["snapshot_fingerprint_sha256"], f"Wrong mock fingerprint: {path.name}")
        sid = mock["scenario_id"]
        require(sid in scenarios, f"Unknown mock scenario: {sid}")
        scenario = scenarios[sid]
        require(snapshot["project_id"] == scenario["project_id"], f"Wrong mock project: {path.name}")
        source_rows = index_unique(snapshot["source_versions"], "id", "mock source ID")
        allowed = set(scenario["source_version_ids"] + scenario["staged_source_version_ids"])
        require(set(source_rows) <= allowed, f"Unauthorized source in mock snapshot: {path.name}")
        for source_id, row in source_rows.items():
            require(row["canonical_text_sha256"] == sources[source_id]["canonical_text_sha256"], f"Stale mock source hash: {source_id}")
        aliases = index_unique(mock["response"]["proposed_commitments"], "id", "mock commitment alias")
        for commitment in aliases.values():
            for evidence in commitment["evidence"]:
                check_span(evidence, source_rows, scenario["project_id"])
        for finding in mock["response"]["findings"]:
            require(set(finding["commitment_ids"]) <= set(aliases) | set(snapshot["commitment_statuses"]), f"Unknown commitment alias in mock finding: {finding['id']}")
            for evidence in finding["evidence"]:
                check_span(evidence, source_rows, scenario["project_id"])
        key = (sid, snapshot["evaluation_as_of"], snapshot["evidence_revision"])
        require(key not in mocks, f"Duplicate mock snapshot: {key}")
        mocks[key] = mock
        mock_points[key] = False

    kinds, event_count, outcomes = Counter(), 0, Counter()
    label_files = set((root / "fixtures/expected").glob("*.json"))
    require(label_files == {root / "fixtures/expected" / (sid + ".json") for sid in scenarios}, "Expected-label files do not match scenario inventory")
    for sid, scenario in scenarios.items():
        pid = scenario["project_id"]
        require(pid in projects and scenario["actor_id"] in memberships[pid], f"Unauthorized scenario actor: {sid}")
        as_of = scenario["evaluation_as_of"]
        timestamp(as_of)
        initial = set(scenario["source_version_ids"])
        staged = set(scenario["staged_source_version_ids"])
        require(not initial & staged, f"Initial/staged source leakage: {sid}")
        for source_id in initial | staged:
            require(source_id in sources and sources[source_id]["project_id"] == pid, f"Cross-project scenario source: {sid}/{source_id}")
            require(sources[source_id]["path"].startswith("fixtures/inputs/sources/"), f"Security boundary file included in provider sources: {sid}")
        labels = load_json(root / "fixtures/expected" / (sid + ".json"))
        validate_schema(labels, schema["$defs"]["expectedCase"], schema, f"expected.{sid}")
        require(labels["scenario_id"] == sid and labels["project_id"] == pid, f"Label identity mismatch: {sid}")
        require(set(labels["expected_active_baseline_source_version_ids"]) == active_baselines(initial, as_of), f"Initial baseline expectation is inconsistent: {sid}")
        require(set(labels["forbidden_source_version_ids"]) <= set(sources), f"Unknown forbidden source: {sid}")
        require(not set(labels["forbidden_source_version_ids"]) & (initial | staged), f"Forbidden source leaks into provider input: {sid}")
        for forbidden in labels["forbidden_source_version_ids"]:
            require(sources[forbidden]["project_id"] != pid, f"Cross-project probe is not cross-project: {sid}")
            require(scenario["actor_id"] not in memberships[sources[forbidden]["project_id"]], f"Probe actor can access the supposedly forbidden project: {sid}")
        outcomes[labels["expected_outcome"]] += 1
        require(bool(labels["expected_findings"]) == (labels["expected_outcome"] == "findings"), f"Outcome/findings disagree: {sid}")
        findings = index_unique(labels["expected_findings"], "id", "expected finding ID")
        for finding in findings.values():
            kinds[finding["kind"]] += 1
            require(len(finding["required_evidence"]) == len(finding["fixture_locators"]), f"Locator count mismatch: {finding['id']}")
            for evidence, locator in zip(finding["required_evidence"], finding["fixture_locators"]):
                check_span(evidence, initial, pid)
                source_id = evidence["source_version_id"]
                require(locator["source_version_id"] == source_id and locator["path"] == sources[source_id]["path"], f"Wrong locator source: {finding['id']}")
                text = texts[source_id]
                require(locator["line_start"] == text[:evidence["start_char"]].count("\n") + 1 and locator["line_end"] == text[:evidence["end_char"] - 1].count("\n") + 1, f"Wrong line locator: {finding['id']}")
        if scenario["events_path"] is None:
            require(not labels["expected_events"], f"Event labels without inputs: {sid}")
            continue
        events_doc = load_json(safe_path(root, scenario["events_path"], ("fixtures/inputs/events/",)))
        validate_schema(events_doc, schema["$defs"]["eventsFile"], schema, f"events.{sid}")
        require(events_doc["scenario_id"] == sid, f"Wrong event scenario: {sid}")
        require([e["id"] for e in events_doc["events"]] == [e["event_id"] for e in labels["expected_events"]], f"Event/label order mismatch: {sid}")
        index_unique(events_doc["events"], "id", "event ID")
        board = scenario["initial_board"]["board_revision"]
        revision = scenario["initial_board"]["evidence_revision"]
        tasks = len(scenario["initial_board"]["tasks"])
        current_sources, authority, effective, supersedes = set(initial), {}, {}, {}
        statuses, proposals, executed, resolved = {}, {}, set(), set()
        for event, result in zip(events_doc["events"], labels["expected_events"]):
            event_count += 1
            operation, request = event["operation"], event.get("request", {})
            actor = event.get("actor_id", scenario["actor_id"])
            require(actor in users, f"Unknown event actor: {event['id']}")
            if "evaluation_as_of" in event:
                require(timestamp(event["evaluation_as_of"]) >= timestamp(as_of), f"Time moves backwards in {event['id']}")
                as_of = event["evaluation_as_of"]
            for field, lookup in event.get("capture", {}).items():
                require((field, lookup) in {("request.expected_commitment_version", "current_commitment.version"), ("request.expected_finding_version", "current_finding.version"), ("request.binding", "current_action.binding"), ("request.binding", "approved_action.binding")}, f"Unsafe/unknown capture lookup: {event['id']}")
            request_def = {"create_action_proposal": "actionCreateRequest", "review_action": "actionReviewRequest", "execute_action": "actionExecuteRequest", "review_source": "sourceReviewRequest", "review_commitment": "commitmentReviewRequest", "review_finding": "findingReviewRequest"}.get(operation)
            if request_def:
                validate_schema(request, schema["$defs"][request_def], schema, f"{event['id']}.request")
            for evidence in request.get("evidence", []):
                check_span(evidence, current_sources, pid)
            status = result.get("http_status")
            if operation in ("attempt_source_read", "attempt_cross_project_source_review"):
                foreign = event["source_version_id"]
                require(foreign in labels["forbidden_source_version_ids"] and status == 404, f"Cross-project probe must expect nondisclosing 404: {event['id']}")
            elif status == 403:
                require(memberships[pid].get(actor) == "viewer", f"403 reviewer test does not use a viewer: {event['id']}")
            elif status == 409:
                if operation == "create_action_proposal":
                    require(request["expected_evidence_revision"] != revision or request["expected_board_revision"] != board, f"Stale proposal test is not stale: {event['id']}")
                elif operation == "attempt_source_id_reuse_with_changed_content":
                    require(event["source_version_id"] in current_sources and event["replacement_text"] != texts[event["source_version_id"]], f"Source mutation probe is ineffective: {event['id']}")
            elif operation == "analyze":
                key = (sid, as_of, revision)
                require(key in mocks, f"Lifecycle analysis has no explicit canned snapshot: {key}")
                mock = mocks[key]
                snapshot = mock["supported_snapshot"]
                require({s["id"] for s in snapshot["source_versions"]} == current_sources, f"Mock snapshot has early/missing sources: {event['id']}")
                require(snapshot["commitment_statuses"] == statuses, f"Mock commitment snapshot mismatch: {event['id']}")
                for row in snapshot["source_versions"]:
                    require(row["authority"] == authority.get(row["id"], sources[row["id"]]["authority"]), f"Mock authority mismatch: {event['id']}")
                    require(row["effective_at"] == effective.get(row["id"], sources[row["id"]]["effective_at"]), f"Mock effective time mismatch: {event['id']}")
                mock_points[key] = True
                for candidate in mock["response"]["proposed_commitments"]:
                    statuses.setdefault(candidate["id"], "proposed")
            elif operation == "import_existing_source":
                require(event["source_version_id"] in current_sources, f"Duplicate-import test references absent source: {event['id']}")
            elif operation == "upload_source":
                source_id = event["source_version_id"]
                require(source_id in staged and source_id not in current_sources, f"Invalid staged upload: {event['id']}")
                require(sources[source_id]["authority"] == "unreviewed", f"Upload starts with authority: {event['id']}")
                current_sources.add(source_id)
                revision += 1
            elif operation == "review_source":
                require(memberships[pid].get(actor) == "reviewer", f"Unauthorized successful review: {event['id']}")
                require(request["expected_evidence_revision"] == revision, f"Source-review revision mismatch: {event['id']}")
                source_id = event["source_version_id"]
                require(source_id in current_sources, f"Review precedes upload: {event['id']}")
                authority[source_id] = request["authority"]
                effective[source_id] = request["effective_at"]
                supersedes[source_id] = request["supersedes_source_version_id"]
                if request["supersedes_source_version_id"]:
                    target = sources[request["supersedes_source_version_id"]]
                    require(target["project_id"] == pid and target["document_id"] == sources[source_id]["document_id"], f"Invalid reviewed source lineage: {event['id']}")
                revision += 1
            elif operation == "review_commitment":
                require(memberships[pid].get(actor) == "reviewer", f"Unauthorized successful commitment review: {event['id']}")
                require(request["expected_evidence_revision"] == revision, f"Commitment-review revision mismatch: {event['id']}")
                cid = event["commitment_id"]
                required_authority = "accepted_baseline" if request["decision"] == "accept" else "acceptance_evidence"
                require(all(authority.get(e["source_version_id"], sources[e["source_version_id"]]["authority"]) == required_authority for e in request["evidence"]), f"Wrong evidence authority for commitment decision: {event['id']}")
                if request["decision"] == "accept":
                    require(statuses.get(cid) == "proposed", f"Accepting a non-proposed commitment: {event['id']}")
                    statuses[cid] = "accepted"
                    previous = request["supersedes_commitment_id"]
                    if previous:
                        require(statuses.get(previous) == "accepted", f"Replacement has no accepted predecessor: {event['id']}")
                        statuses[previous] = "superseded"
                else:
                    require(statuses.get(cid) == "accepted", f"Closing a non-accepted commitment: {event['id']}")
                    statuses[cid] = "closed"
                revision += 1
            elif operation == "create_action_proposal":
                payload = request["payload"]
                require(payload["owner_id"] in memberships[pid], f"Unknown task owner: {event['id']}")
                require(payload["finding_id"] in findings, f"Unknown originating finding: {event['id']}")
                require(request["expected_evidence_revision"] == revision and request["expected_board_revision"] == board, f"Successful proposal is stale: {event['id']}")
                proposals[event["action_id"]] = {"hash": sha256(canonical_json(payload)), "revision": revision, "board": board, "approved": False}
            elif operation in ("review_action", "execute_action"):
                require(memberships[pid].get(actor) == "reviewer", f"Unauthorized successful action: {event['id']}")
                proposal = proposals[event["action_id"]]
                binding = request["binding"]
                require(binding["payload_hash"] == proposal["hash"] and binding["evidence_revision"] == proposal["revision"] and binding["base_board_revision"] == proposal["board"], f"Action binding mismatch: {event['id']}")
                if operation == "review_action":
                    require(revision == proposal["revision"] and board == proposal["board"], f"Stale approval marked successful: {event['id']}")
                    proposal["approved"] = request["decision"] == "approve"
                elif event["action_id"] not in executed:
                    require(proposal["approved"] and revision == proposal["revision"] and board == proposal["board"], f"Unapproved/stale execution marked successful: {event['id']}")
                    executed.add(event["action_id"])
                    tasks += 1
                    board += 1
            elif operation == "review_finding":
                require(memberships[pid].get(actor) == "reviewer" and request["expected_evidence_revision"] == revision, f"Invalid finding review: {event['id']}")
                require(event["finding_id"] in findings and request["decision"] == "resolve" and request["evidence"], f"Unsupported finding resolution: {event['id']}")
                resolved.add(event["finding_id"])
            else:
                raise InvalidFixture(f"Unsupported successful event operation: {operation}")
            require((result["expected_board_revision"], result["expected_evidence_revision"], result["expected_task_count"]) == (board, revision, tasks), f"Lifecycle expectation arithmetic mismatch: {event['id']}")
            if "expected_active_baseline_source_version_ids" in result:
                require(set(result["expected_active_baseline_source_version_ids"]) == active_baselines(current_sources, as_of, authority, effective, supersedes), f"Event baseline expectation mismatch: {event['id']}")
            if "expected_commitment_statuses" in result:
                require(all(statuses.get(key) == value for key, value in result["expected_commitment_statuses"].items()), f"Commitment status expectation mismatch: {event['id']}")
            if "expected_resolved_finding_ids" in result:
                require(set(result["expected_resolved_finding_ids"]) == resolved, f"Finding resolution expectation mismatch: {event['id']}")
            if "expected_open_finding_ids" in result:
                require(set(result["expected_open_finding_ids"]) == set(findings) - resolved, f"Finding open-state expectation mismatch: {event['id']}")
            for evidence in result.get("required_resolution_evidence", []):
                check_span(evidence, current_sources, pid)
            if "expected_task_status" in result:
                require(result["expected_task_status"] == "open" and result.get("expected_task_version", 1) == 1, f"Fixture assumes an out-of-scope task completion: {event['id']}")
    require(all(mock_points.values()), "A canned response has no matching lifecycle analysis point")
    require(set(kinds) == {"scope_mismatch", "acceptance_gap", "dependency_conflict"}, "Finding-kind coverage is incomplete")
    return {
        "status": "passed", "validation_scope": "starter_pack_integrity_only",
        "scenarios": len(scenarios), "projects_including_security_decoy": len(projects),
        "source_versions": len(sources), "expected_label_files": len(label_files),
        "lifecycle_events": event_count, "canned_mock_snapshots": len(mocks),
        "validated_evidence_span_occurrences": span_count,
        "expected_findings_by_kind": dict(sorted(kinds.items())), "scenario_outcomes": dict(sorted(outcomes.items())),
        "application_tests": "not_run", "live_model_calls": 0,
        "live_accuracy_metrics": "not_measured", "runtime_security_tests": "not_run",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Print machine-readable pack validation results")
    args = parser.parse_args()
    try:
        result = check(args.root.resolve())
    except (InvalidFixture, OSError, UnicodeError, KeyError, TypeError) as exc:
        if args.json:
            print(json.dumps({"status": "failed", "validation_scope": "starter_pack_integrity_only", "error": str(exc)}, indent=2))
        else:
            print(f"Fixture pack validation FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Fixture pack validation PASSED: {result['scenarios']} scenarios, {result['source_versions']} sources, {result['lifecycle_events']} lifecycle events, {result['canned_mock_snapshots']} canned snapshots.")
        print("Checked schemas, source hashes, exact Unicode citations, project boundaries, source supersession and lifecycle fixture consistency.")
        print("This is pack validation only. Application tests, runtime security tests and live-model accuracy have NOT been run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
