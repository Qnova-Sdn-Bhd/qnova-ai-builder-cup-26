"""Strict model-facing quotes and deterministic provider-boundary validation."""

import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[str, Field(min_length=1, max_length=200)]
Quote = Annotated[str, Field(min_length=1, max_length=10000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuoteLocator(StrictModel):
    source_version_id: Id
    quote: Quote


class QuotedCommitment(StrictModel):
    id: Id
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    acceptance_criteria: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=30)
    ]
    owner_id: Id | None
    due_date: str | None
    evidence: Annotated[list[QuoteLocator], Field(min_length=1, max_length=20)]


class QuotedFinding(StrictModel):
    id: Id
    kind: Literal["scope_mismatch", "acceptance_gap", "dependency_conflict"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    explanation: Annotated[str, Field(min_length=1, max_length=3000)]
    commitment_ids: Annotated[list[Id], Field(min_length=1, max_length=20)]
    evidence: Annotated[list[QuoteLocator], Field(min_length=1, max_length=20)]


class QuotedAnalysis(StrictModel):
    proposed_commitments: Annotated[list[QuotedCommitment], Field(max_length=100)]
    findings: Annotated[list[QuotedFinding], Field(max_length=100)]
    coverage_warnings: Annotated[list[Annotated[str, Field(max_length=1000)]], Field(max_length=50)]


def generation_schema() -> dict:
    """Send Gemini the output shape; enforce bounds and extras after generation.

    Vertex structured output can reject complex, constrained schemas with a generic
    INVALID_ARGUMENT. Keep the transport schema small and inline references, while
    QuotedAnalysis remains the independent strict validation boundary.
    """
    schema = QuotedAnalysis.model_json_schema()
    definitions = schema.get("$defs", {})

    def shape(node: dict) -> dict:
        if "$ref" in node:
            return shape(definitions[node["$ref"].removeprefix("#/$defs/")])
        if "anyOf" in node:
            variants = node["anyOf"]
            nonnull = [item for item in variants if item.get("type") != "null"]
            if len(nonnull) == 1 and len(variants) == 2:
                return {**shape(nonnull[0]), "nullable": True}
            return {"anyOf": [shape(item) for item in variants]}
        result = {key: node[key] for key in ("type", "enum", "required") if key in node}
        if "properties" in node:
            result["properties"] = {
                name: shape(value) for name, value in node["properties"].items()
            }
        if "items" in node:
            result["items"] = shape(node["items"])
        return result

    return shape(schema)


class EvidenceError(ValueError):
    """Safe failure category; never include a source body in the exception."""

    def __init__(self, code: str, *, diagnostic_reason: str | None = None):
        super().__init__(code)
        self.diagnostic_reason = diagnostic_reason


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_packet(packet: dict) -> dict:
    """Whitelist packet fields; never forward fixture labels or operational records."""
    project = packet.get("project", {})
    project_id = project.get("id") or packet.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise EvidenceError("INVALID_PROJECT")
    sources = packet.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 30:
        raise EvidenceError("SOURCE_LIMIT")
    safe_sources, seen = [], set()
    total = 0
    fields = (
        "id",
        "document_id",
        "version_number",
        "title",
        "source_kind",
        "authority",
        "authority_confirmed_by",
        "created_at",
        "effective_at",
        "supersedes_source_version_id",
        "canonical_text_sha256",
        "page_spans",
    )
    for source in sources:
        source_id = source.get("id")
        text = source.get("canonical_text")
        if (
            source.get("project_id") != project_id
            or not isinstance(source_id, str)
            or not source_id
            or source_id in seen
        ):
            raise EvidenceError("INVALID_SOURCE_REFERENCE")
        if source.get("ingestion_status") != "ready" or source.get("withdrawn_at"):
            raise EvidenceError("INCOMPLETE_PACKET")
        if not isinstance(text, str) or not text or len(text) > 200000:
            raise EvidenceError("INVALID_CANONICAL_TEXT")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != source.get("canonical_text_sha256"):
            raise EvidenceError("CANONICAL_HASH_MISMATCH")
        seen.add(source_id)
        total += len(text)
        safe_sources.append({**{key: source.get(key) for key in fields}, "canonical_text": text})
    if total > 120000:
        raise EvidenceError("PACKET_TOO_LARGE")
    commitments = packet.get("commitments", [])
    if not isinstance(commitments, list) or len(commitments) > 500:
        raise EvidenceError("COMMITMENT_LIMIT")
    safe_commitments = []
    for item in commitments:
        if item.get("project_id") != project_id:
            raise EvidenceError("INVALID_COMMITMENT_REFERENCE")
        safe_commitments.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    "description",
                    "status",
                    "acceptance_criteria",
                    "owner_id",
                    "due_date",
                    "evidence",
                    "supersedes_commitment_id",
                    "version",
                )
            }
        )
    return {
        "project_id": project_id,
        "evidence_revision": packet.get("evidence_revision", project.get("evidence_revision")),
        "evaluation_as_of": packet.get("evaluation_as_of"),
        "business_clock": project.get("business_clock"),
        "sources": safe_sources,
        "commitments": safe_commitments,
        "member_ids": [
            item["user_id"]
            for item in packet.get("memberships", [])
            if item.get("project_id", project_id) == project_id and item.get("user_id")
        ],
    }


def resolve_quote(locator: dict, sources: dict[str, dict]) -> dict:
    source = sources.get(locator["source_version_id"])
    if source is None:
        raise EvidenceError("FOREIGN_SOURCE")
    text, quote = source["canonical_text"], locator["quote"]
    start = text.find(quote)
    if start < 0:
        raise EvidenceError("QUOTE_MISSING_OR_AMBIGUOUS", diagnostic_reason="QUOTE_NOT_FOUND")
    if text.find(quote, start + 1) >= 0:
        raise EvidenceError("QUOTE_MISSING_OR_AMBIGUOUS", diagnostic_reason="QUOTE_NOT_UNIQUE")
    result = {**locator, "start_char": start, "end_char": start + len(quote)}
    for page in source.get("page_spans") or []:
        if page["start_char"] <= start and result["end_char"] <= page["end_char"]:
            result["page_number"] = page["page_number"]
            break
    return result


def resolve_result(raw: str, safe_packet: dict) -> dict:
    if len(raw.encode("utf-8")) > 524288:
        raise EvidenceError("OUTPUT_TOO_LARGE")
    result = QuotedAnalysis.model_validate_json(raw).model_dump()
    sources = {item["id"]: item for item in safe_packet["sources"]}
    known = {item["id"] for item in safe_packet["commitments"]}
    candidates = [item["id"] for item in result["proposed_commitments"]]
    finding_ids = [item["id"] for item in result["findings"]]
    if len(candidates) != len(set(candidates)) or known.intersection(candidates):
        raise EvidenceError("DUPLICATE_COMMITMENT_ID")
    if len(finding_ids) != len(set(finding_ids)):
        raise EvidenceError("DUPLICATE_FINDING_ID")
    known.update(candidates)
    for candidate in result["proposed_commitments"]:
        if candidate["owner_id"] and candidate["owner_id"] not in safe_packet["member_ids"]:
            raise EvidenceError("INVALID_OWNER")
        if candidate["due_date"] is not None:
            try:
                parsed = date.fromisoformat(candidate["due_date"])
                if parsed.isoformat() != candidate["due_date"]:
                    raise ValueError
            except ValueError as exc:
                raise EvidenceError("INVALID_DATE") from exc
    for finding in result["findings"]:
        if not set(finding["commitment_ids"]).issubset(known):
            raise EvidenceError("UNKNOWN_COMMITMENT")
        if finding["kind"] in {"scope_mismatch", "dependency_conflict"}:
            if len(finding["evidence"]) < 2:
                raise EvidenceError("TWO_POSITIONS_REQUIRED")
    for item in result["proposed_commitments"] + result["findings"]:
        item["evidence"] = [resolve_quote(span, sources) for span in item["evidence"]]
    if len(canonical_json(result).encode("utf-8")) > 524288:
        raise EvidenceError("OUTPUT_TOO_LARGE")
    return result
