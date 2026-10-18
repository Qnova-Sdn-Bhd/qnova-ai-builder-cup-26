"""Score human-adjudicated findings without letting evaluator gold reach an agent.

Input: {provider_mode: LIVE_GEMINI|CANNED, scenarios: [{scenario_id, predictions:
[{id,kind,matched_gold_id:null|id,support_reviewed:true,citation_valid:true}]}]}.
Matching is explicitly recorded by a reviewer: IDs or category words alone are not semantic matches.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("scope_mismatch", "acceptance_gap", "dependency_conflict")


def metric(counts):
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return {**{key: counts[key] for key in ("tp", "fp", "fn", "duplicates")},
            "precision": precision, "recall": recall, "f1": f1}


def score(record):
    if record.get("provider_mode") not in {"LIVE_GEMINI", "CANNED"}:
        raise ValueError("Explicit LIVE_GEMINI or CANNED provenance required")
    by_kind = {kind: Counter() for kind in KINDS}
    seen_scenarios, clean_cases, clean_alarms, spans, valid = set(), 0, 0, 0, 0
    for case in record["scenarios"]:
        case_id = case["scenario_id"]
        if case_id not in {f"scn_{number:02d}" for number in range(1, 13)}:
            raise ValueError("Unknown scenario")
        if case_id in seen_scenarios:
            raise ValueError("Repeated scenarios cannot count as independent observations")
        seen_scenarios.add(case_id)
        gold = json.loads((ROOT / "fixtures" / "expected" / f"{case_id}.json").read_text("utf-8"))
        gold_by_id = {item["id"]: item for item in gold["expected_findings"]}
        matched = set()
        predictions = case["predictions"]
        if not gold_by_id and gold["expected_outcome"] == "no_findings":
            clean_cases += 1
            clean_alarms += bool(predictions)
        for prediction in predictions:
            kind = prediction["kind"]
            if kind not in KINDS or prediction.get("support_reviewed") is not True:
                raise ValueError("Every prediction needs a supported kind and human support review")
            span_count = prediction.get("citation_count", 0)
            valid_count = prediction.get("valid_citation_count", 0)
            if not 0 <= valid_count <= span_count:
                raise ValueError("Invalid citation counts")
            spans += span_count
            valid += valid_count
            match = prediction.get("matched_gold_id")
            if match is not None and (match not in gold_by_id or gold_by_id[match]["kind"] != kind):
                raise ValueError("Human match references an unknown or different-kind issue")
            if match in matched:
                by_kind[kind]["duplicates"] += 1
            elif match is not None and prediction.get("citation_valid") is True:
                by_kind[kind]["tp"] += 1
                matched.add(match)
            else:
                by_kind[kind]["fp"] += 1
        for issue in gold_by_id.values():
            if issue["id"] not in matched:
                by_kind[issue["kind"]]["fn"] += 1
    total = sum(by_kind.values(), Counter())
    per_kind = {kind: metric(counts) for kind, counts in by_kind.items()}
    macro = {}
    for key in ("precision", "recall", "f1"):
        values = [value[key] for value in per_kind.values() if value[key] is not None]
        macro[key] = sum(values) / len(values) if values else None
    return {"provider_mode": record["provider_mode"],
            "live_accuracy_evidence": record["provider_mode"] == "LIVE_GEMINI",
            "dataset": "open synthetic development set; scenarios 01/12 overlap",
            "scenario_count": len(seen_scenarios), "micro": metric(total), "macro": macro,
            "per_kind": per_kind, "clean_cases": clean_cases,
            "clean_false_alarm_rate": clean_alarms / clean_cases if clean_cases else None,
            "structural_citation_validity": valid / spans if spans else None,
            "impact": "not_measured", "cost": "not_measured"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjudication", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    result = score(json.loads(args.adjudication.read_text("utf-8")))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
