"""Bounded validation diagnostics that cannot contain model or document values."""

from pydantic import ValidationError

from handoff_agent.schemas import EvidenceError, canonical_json

_EVIDENCE_REASONS = {
    "OUTPUT_TOO_LARGE",
    "DUPLICATE_COMMITMENT_ID",
    "DUPLICATE_FINDING_ID",
    "INVALID_OWNER",
    "INVALID_DATE",
    "UNKNOWN_COMMITMENT",
    "TWO_POSITIONS_REQUIRED",
    "FOREIGN_SOURCE",
    "QUOTE_MISSING_OR_AMBIGUOUS",
    "QUOTE_NOT_FOUND",
    "QUOTE_NOT_UNIQUE",
}
_FIELDS = {
    "proposed_commitments",
    "findings",
    "coverage_warnings",
    "id",
    "description",
    "acceptance_criteria",
    "owner_id",
    "due_date",
    "evidence",
    "source_version_id",
    "quote",
    "kind",
    "title",
    "explanation",
    "commitment_ids",
}
_ERROR_TYPES = {
    "json_invalid",
    "missing",
    "extra_forbidden",
    "string_type",
    "string_too_short",
    "string_too_long",
    "list_type",
    "too_short",
    "too_long",
    "literal_error",
    "model_type",
    "dict_type",
    "int_type",
    "none_required",
    "recursion_loop",
}
_TOKEN_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "total_token_count",
    "cached_content_token_count",
    "tool_use_prompt_token_count",
)
_HINTS = {
    "QUOTE_NOT_FOUND": "Copy a contiguous substring from canonical_text verbatim. Preserve spaces, "
    "line breaks, punctuation and case. Do not join text across omitted content.",
    "QUOTE_NOT_UNIQUE": "Choose a longer contiguous quote that occurs exactly once in its source.",
    "FOREIGN_SOURCE": "Use only source IDs listed in this packet.",
    "UNKNOWN_COMMITMENT": "Reference an existing commitment ID or an ID declared in "
    "proposed_commitments in this same response.",
    "DUPLICATE_COMMITMENT_ID": "Give each new candidate a unique ID distinct from existing IDs.",
    "DUPLICATE_FINDING_ID": "Give each finding a distinct local ID.",
    "INVALID_OWNER": "Use a supplied member_id or null for owner_id.",
    "INVALID_DATE": "Use a valid YYYY-MM-DD calendar date or null for due_date.",
    "TWO_POSITIONS_REQUIRED": "Scope mismatch and dependency conflict each require at least two "
    "exact evidence citations covering both positions.",
    "OUTPUT_TOO_LARGE": "Return fewer, more concise grounded observations within output bounds.",
}


def validation_failure(exc: ValueError) -> dict:
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        return {
            "reason": "OUTPUT_SCHEMA_INVALID",
            "error_count": exc.error_count(),
            "errors": [
                {
                    "type": item["type"] if item["type"] in _ERROR_TYPES else "validation_error",
                    "loc": [
                        value
                        if type(value) is int and 0 <= value <= 1000
                        else value
                        if type(value) is str and value in _FIELDS
                        else "unknown_field"
                        for value in item.get("loc", ())[:6]
                    ],
                }
                for item in errors[:5]
            ],
        }
    if isinstance(exc, EvidenceError):
        if type(exc.diagnostic_reason) is str and exc.diagnostic_reason in _EVIDENCE_REASONS:
            return {"reason": exc.diagnostic_reason}
        if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _EVIDENCE_REASONS:
            return {"reason": exc.args[0]}
    return {"reason": "OUTPUT_VALIDATION_FAILED"}


def log_rejection(attempt: int, failure: dict, usage: dict | None) -> None:
    counts = {
        key: value
        for key in _TOKEN_FIELDS
        if usage and type(value := usage.get(key)) is int and 0 <= value <= 2**53
    }
    # Direct JSON stdout is intentional: no SDK handlers, exception text or model input.
    print(
        canonical_json(
            {
                "severity": "WARNING",
                "event": "model_output_rejected",
                "attempt": attempt,
                **failure,
                "usage": counts,
            }
        ),
        flush=True,
    )


def repair_instruction(failure: dict) -> str:
    hint = _HINTS.get(
        failure["reason"],
        "Follow the JSON field types, required fields and bounds. "
        "Do not add fields, and use only grounded references.",
    )
    return (
        "\nThe previous result failed deterministic validation: "
        + canonical_json(failure)
        + ". "
        + hint
        + " Every finding needs nonempty commitment_ids referencing existing IDs or IDs declared "
        "in proposed_commitments in this same response. Every candidate and finding needs "
        "nonempty evidence. Scope/dependency conflicts need at least two cited positions. "
        "Copy unique contiguous source passages without whitespace, Markdown or Unicode "
        "normalization." + " Return the full corrected JSON response."
    )
