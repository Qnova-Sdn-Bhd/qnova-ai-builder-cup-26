import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_evidence import output

from handoff_agent.diagnostics import log_rejection, repair_instruction, validation_failure
from handoff_agent.schemas import EvidenceError, QuotedAnalysis, resolve_quote


def test_pydantic_diagnostics_exclude_inputs_unknown_keys_context_and_messages():
    secret = "PRIVATE_SOURCE_OR_MODEL_CONTENT"
    raw = output()
    raw["findings"][0][secret] = secret
    raw["findings"][0]["kind"] = secret
    with pytest.raises(ValidationError) as caught:
        QuotedAnalysis.model_validate_json(json.dumps(raw))
    failure = validation_failure(caught.value)
    assert failure["reason"] == "OUTPUT_SCHEMA_INVALID"
    assert failure["error_count"] == 2
    assert sorted(failure["errors"], key=lambda item: item["type"]) == [
        {"type": "extra_forbidden", "loc": ["findings", 0, "unknown_field"]},
        {"type": "literal_error", "loc": ["findings", 0, "kind"]},
    ]
    assert secret not in json.dumps(failure)
    assert secret not in repair_instruction(failure)


def test_pydantic_diagnostics_are_bounded():
    raw = output()
    raw["findings"][0].update({f"unknown{i}": "private" for i in range(20)})
    with pytest.raises(ValidationError) as caught:
        QuotedAnalysis.model_validate_json(json.dumps(raw))
    failure = validation_failure(caught.value)
    assert failure["error_count"] == 20
    assert len(failure["errors"]) == 5
    assert "private" not in json.dumps(failure)


@pytest.mark.parametrize(
    ("quote", "reason"), [("not in text", "QUOTE_NOT_FOUND"), ("a", "QUOTE_NOT_UNIQUE")]
)
def test_quote_diagnostics_distinguish_cause_preserving_public_error(quote, reason):
    with pytest.raises(EvidenceError, match="QUOTE_MISSING_OR_AMBIGUOUS") as caught:
        resolve_quote({"source_version_id": "s", "quote": quote}, {"s": {"canonical_text": "aa"}})
    assert validation_failure(caught.value) == {"reason": reason}


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("PRIVATE_SOURCE_OR_MODEL_CONTENT"),
        EvidenceError("PRIVATE_SOURCE_OR_MODEL_CONTENT"),
        EvidenceError("PRIVATE_SOURCE_OR_MODEL_CONTENT", diagnostic_reason="PRIVATE_DETAIL"),
    ],
)
def test_unknown_exceptions_cannot_escape_taxonomy(exc):
    assert validation_failure(exc) == {"reason": "OUTPUT_VALIDATION_FAILED"}


def test_json_stdout_rejection_logs_only_numeric_usage(capsys):
    secret = "PRIVATE_SOURCE_OR_MODEL_CONTENT"
    usage = {
        "prompt_token_count": 10,
        "candidates_token_count": 5,
        "total_token_count": 15,
        "thoughts_token_count": True,
        "cached_content_token_count": secret,
        "tool_use_prompt_token_count": -1,
        "hidden_reasoning": secret,
    }
    log_rejection(1, {"reason": "QUOTE_NOT_FOUND"}, usage)
    captured = capsys.readouterr()
    assert not captured.err
    assert secret not in captured.out
    assert json.loads(captured.out) == {
        "severity": "WARNING",
        "event": "model_output_rejected",
        "attempt": 1,
        "reason": "QUOTE_NOT_FOUND",
        "usage": {"prompt_token_count": 10, "candidates_token_count": 5, "total_token_count": 15},
    }


async def test_repair_uses_safe_failure_and_keeps_rejected_response_out(packet, capsys):
    from unittest.mock import AsyncMock

    from handoff_agent.workflow import analyze

    secret = "PRIVATE_REJECTED_MODEL_CONTENT"
    rejected = output(secret)
    generate = AsyncMock(
        side_effect=[
            SimpleNamespace(text=json.dumps(rejected), usage_metadata=None),
            SimpleNamespace(text=json.dumps(output()), usage_metadata=None),
        ]
    )
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    _, provenance = await analyze(packet, client, "gemini-unit-test")
    assert provenance["model_calls"] == 2
    assert provenance["prompt_version"].endswith("-v2")
    repair = generate.await_args_list[1].kwargs["contents"]
    assert "QUOTE_NOT_FOUND" in repair
    assert "contiguous substring" in repair
    assert "nonempty commitment_ids" in repair
    assert secret not in repair
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert json.loads(captured.out)["reason"] == "QUOTE_NOT_FOUND"
