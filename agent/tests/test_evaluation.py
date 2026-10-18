import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from prepare_evaluation import prepare  # noqa: E402
from score_evaluation import score  # noqa: E402

from handoff_agent.schemas import validate_packet


@pytest.mark.parametrize("scenario_id", [f"scn_{i:02d}" for i in range(1, 13)])
def test_initial_development_packets_are_scoped_and_valid(scenario_id):
    safe = validate_packet(prepare(scenario_id))
    assert safe["sources"]
    if scenario_id in {"scn_01", "scn_12"}:
        ids = {item["id"] for item in safe["sources"]}
        assert "sv_hp_sow_002" not in ids
        assert "sv_hp_acceptance_001" not in ids


def test_metrics_require_review_and_preserve_canned_provenance():
    record = {
        "provider_mode": "CANNED",
        "scenarios": [
            {
                "scenario_id": "scn_01",
                "predictions": [
                    {
                        "id": "f",
                        "kind": "scope_mismatch",
                        "matched_gold_id": "fnd_hp_scope",
                        "support_reviewed": True,
                        "citation_valid": True,
                        "citation_count": 2,
                        "valid_citation_count": 2,
                    }
                ],
            }
        ],
    }
    result = score(record)
    assert result["micro"]["tp"] == 1
    assert result["micro"]["f1"] == 1
    assert not result["live_accuracy_evidence"]
    record["scenarios"][0]["predictions"][0]["support_reviewed"] = False
    with pytest.raises(ValueError):
        score(record)


def test_zero_denominators_remain_unknown():
    result = score({"provider_mode": "CANNED", "scenarios": []})
    assert result["micro"]["precision"] is None
    assert result["macro"]["recall"] is None
