import hashlib
import json

import pytest

from handoff_agent.schemas import EvidenceError, resolve_result, validate_packet


@pytest.fixture
def packet():
    text = "📋 Review: customer acceptance must be recorded."
    return {
        "project": {"id": "p", "evidence_revision": 4},
        "sources": [
            {
                "id": "s",
                "project_id": "p",
                "canonical_text": text,
                "canonical_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "ingestion_status": "ready",
                "authority": "accepted_baseline",
                "page_spans": [{"page_number": 1, "start_char": 0, "end_char": len(text)}],
            }
        ],
        "commitments": [{"id": "c", "project_id": "p"}],
    }


def output(quote="customer acceptance must be recorded."):
    return {
        "proposed_commitments": [],
        "coverage_warnings": [],
        "findings": [
            {
                "id": "f",
                "kind": "acceptance_gap",
                "title": "Acceptance unproven",
                "explanation": "No acceptance evidence supplied in this packet.",
                "commitment_ids": ["c"],
                "evidence": [{"source_version_id": "s", "quote": quote}],
            }
        ],
    }


def test_unicode_offsets_are_codepoints(packet):
    result = resolve_result(json.dumps(output()), validate_packet(packet))
    span = result["findings"][0]["evidence"][0]
    assert span["start_char"] == 10
    assert span["page_number"] == 1
    assert (
        packet["sources"][0]["canonical_text"][span["start_char"] : span["end_char"]]
        == (span["quote"])
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"project_id": "foreign"},
        {"ingestion_status": "failed"},
        {"canonical_text_sha256": "0" * 64},
        {"withdrawn_at": "2026-01-01"},
    ],
)
def test_packet_rejects_foreign_failed_or_tampered_sources(packet, mutation):
    packet["sources"][0].update(mutation)
    with pytest.raises(EvidenceError):
        validate_packet(packet)


def test_labels_never_forwarded(packet):
    packet["expected_labels"] = ["sensitive evaluator label"]
    packet["sources"][0]["expected"] = "hidden label"
    assert "label" not in json.dumps(validate_packet(packet))


def test_memberships_are_bound_to_the_authorized_packet(packet):
    packet["memberships"] = [{"user_id": "reviewer", "role": "reviewer"}]
    assert validate_packet(packet)["member_ids"] == ["reviewer"]


@pytest.mark.parametrize("quote", ["not in source", "e"])
def test_missing_and_ambiguous_quotes_fail(packet, quote):
    with pytest.raises(EvidenceError, match="QUOTE_MISSING_OR_AMBIGUOUS"):
        resolve_result(json.dumps(output(quote)), validate_packet(packet))


def test_foreign_output_and_unknown_commitment(packet):
    raw = output()
    raw["findings"][0]["commitment_ids"] = ["foreign"]
    with pytest.raises(EvidenceError, match="UNKNOWN_COMMITMENT"):
        resolve_result(json.dumps(raw), validate_packet(packet))
    raw = output()
    raw["findings"][0]["evidence"][0]["source_version_id"] = "foreign"
    with pytest.raises(EvidenceError, match="FOREIGN_SOURCE"):
        resolve_result(json.dumps(raw), validate_packet(packet))


def test_conflict_requires_two_positions(packet):
    raw = output()
    raw["findings"][0]["kind"] = "scope_mismatch"
    with pytest.raises(EvidenceError, match="TWO_POSITIONS_REQUIRED"):
        resolve_result(json.dumps(raw), validate_packet(packet))


@pytest.mark.parametrize("raw", ["invalid JSON", "{}", '{"findings": [], "status": "accepted"}'])
def test_invalid_shape_never_publishes(packet, raw):
    with pytest.raises(ValueError):
        resolve_result(raw, validate_packet(packet))


@pytest.mark.parametrize("violation", ["extra", "long_title", "empty_evidence", "many_warnings"])
def test_strict_validation_remains_after_lean_generation_schema(packet, violation):
    raw = output()
    if violation == "extra":
        raw["findings"][0]["authority"] = "accepted_baseline"
    elif violation == "long_title":
        raw["findings"][0]["title"] = "x" * 201
    elif violation == "empty_evidence":
        raw["findings"][0]["evidence"] = []
    else:
        raw["coverage_warnings"] = ["warning"] * 51
    with pytest.raises(ValueError):
        resolve_result(json.dumps(raw), validate_packet(packet))


def test_size_caps(packet):
    with pytest.raises(EvidenceError, match="OUTPUT_TOO_LARGE"):
        resolve_result(" " * 524289, validate_packet(packet))
    packet["sources"] *= 31
    with pytest.raises(EvidenceError, match="SOURCE_LIMIT"):
        validate_packet(packet)
