"""Deterministic evidence checks; all offsets are Python Unicode code points."""

from datetime import UTC, datetime

from .contracts import canonical, digest, validate
from .errors import require


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def business_now(project: dict) -> str:
    return project["business_clock"]["as_of"] if project["business_clock"]["mode"] == "synthetic" else now()


def active_baseline(sources: list[dict], as_of: str) -> list[str]:
    eligible = {
        source["id"]: source
        for source in sources
        if (
            source["authority"] == "accepted_baseline"
            and source.get("authority_confirmed_by")
            and source["ingestion_status"] == "ready"
            and source.get("effective_at")
            and timestamp(source["effective_at"]) <= timestamp(as_of)
        )
    }
    superseded = {
        source["supersedes_source_version_id"]
        for source in eligible.values()
        if source.get("supersedes_source_version_id")
    }
    return sorted(set(eligible) - superseded)


def check_span(span: dict, sources: dict[str, dict]) -> None:
    validate("EvidenceSpan", span)
    source = sources.get(span["source_version_id"])
    require(source is not None, "INVALID_EVIDENCE", "Evidence is outside the authorized snapshot.", 422)
    text = source["canonical_text"]
    start, end = span["start_char"], span["end_char"]
    require(
        0 <= start < end <= len(text) and text[start:end] == span["quote"],
        "INVALID_EVIDENCE",
        "Evidence does not match immutable canonical text.",
        422,
    )
    if "page_number" in span:
        require(
            any(
                page["page_number"] == span["page_number"]
                and page["start_char"] <= start
                and end <= page["end_char"]
                for page in source.get("page_spans", [])
            ),
            "INVALID_EVIDENCE",
            "Evidence does not fit the declared page.",
            422,
        )


def validate_provider(result: dict, packet: dict) -> dict:
    require(
        len(canonical(result)) <= 512 * 1024, "PROVIDER_OUTPUT_INVALID", "Analysis output is too large.", 422
    )
    validate("ProviderAnalysisResult", result)
    sources = {source["id"]: source for source in packet["sources"]}
    for source in sources.values():
        require(
            digest(source["canonical_text"].encode()) == source["canonical_text_sha256"],
            "INVALID_EVIDENCE",
            "Snapshot canonical hash mismatch.",
            422,
        )
    candidates = result["proposed_commitments"]
    candidate_ids = [item["id"] for item in candidates]
    finding_ids = [item["id"] for item in result["findings"]]
    require(
        len(set(candidate_ids)) == len(candidate_ids) and len(set(finding_ids)) == len(finding_ids),
        "PROVIDER_OUTPUT_INVALID",
        "Response aliases must be unique.",
        422,
    )
    known = {item["id"] for item in packet["commitments"]}
    require(
        not known.intersection(candidate_ids),
        "PROVIDER_OUTPUT_INVALID",
        "Candidate aliases collide with existing commitments.",
        422,
    )
    owners = {item["user_id"] for item in packet["memberships"]}
    for item in [*candidates, *result["findings"]]:
        for span in item["evidence"]:
            check_span(span, sources)
        if "owner_id" in item:
            require(
                item["owner_id"] is None or item["owner_id"] in owners,
                "PROVIDER_OUTPUT_INVALID",
                "Unknown commitment owner.",
                422,
            )
        else:
            require(
                item["kind"] == "acceptance_gap" or len(item["evidence"]) >= 2,
                "PROVIDER_OUTPUT_INVALID",
                "Scope and dependency comparisons require both evidence sides.",
                422,
            )
            require(
                set(item["commitment_ids"]) <= known | set(candidate_ids),
                "PROVIDER_OUTPUT_INVALID",
                "Unknown commitment reference.",
                422,
            )
    return result


def canonicalize_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        require(False, "EXTRACTION_FAILED", "Source must contain valid UTF-8 text.", 422)
    require(
        0 < len(text) <= 200000 and bool(text.strip()),
        "EMPTY_EXTRACTION",
        "Extraction is empty or exceeds the canonical text limit.",
        422,
    )
    return text
