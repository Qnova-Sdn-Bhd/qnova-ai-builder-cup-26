"""Prepare authorized initial Markdown development packets; performs no model calls.

Gold labels and staged sources never enter packets. These files are offline synthetic
inputs, not deployed source records or PDF annotations. Live analysis must use normal
authenticated cloud ingestion and snapshots instead.
"""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(scenario_id):
    manifest = json.loads((ROOT / "fixtures/inputs/manifest.json").read_text("utf-8"))
    scenario = next((item for item in manifest["scenarios"] if item["id"] == scenario_id), None)
    if scenario is None:
        raise ValueError("Unknown scenario")
    project = next(item for item in manifest["projects"] if item["id"] == scenario["project_id"])
    sources = []
    for source_id in scenario["source_version_ids"]:
        source = next(item for item in manifest["source_versions"] if item["id"] == source_id)
        if source["project_id"] != project["id"]:
            raise ValueError("Foreign project source")
        source_path = (ROOT / source["path"]).resolve()
        if not source_path.is_relative_to(ROOT / "fixtures/inputs/sources"):
            raise ValueError("Source path is outside eligible input tree")
        raw = source_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["content_sha256"]:
            raise ValueError("Fixture bytes changed")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if hashlib.sha256(text.encode()).hexdigest() != source["canonical_text_sha256"]:
            raise ValueError("Fixture canonical hash changed")
        sources.append({**{key: value for key, value in source.items() if key != "path"},
                        "canonical_text": text, "ingestion_status": "ready", "withdrawn_at": None,
                        "page_spans": []})
    board = scenario["initial_board"]
    return {"project": {key: project[key] for key in ("id", "name", "timezone")}
            | {"business_clock": {"mode": "synthetic", "as_of": scenario["evaluation_as_of"]},
               "evidence_revision": board["evidence_revision"],
               "board_revision": board["board_revision"]},
            "evidence_revision": board["evidence_revision"],
            "evaluation_as_of": scenario["evaluation_as_of"],
            "sources": sources, "commitments": [],
            "memberships": project["memberships"], "coverage_warnings": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", choices=[f"scn_{i:02d}" for i in range(1, 13)])
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = [f"scn_{i:02d}" for i in range(1, 13)] if args.all else [args.case]
    for case_id in selected:
        packet = prepare(case_id)
        (args.output_dir / f"{case_id}.packet.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps({"status": "prepared", "provider_mode": "NOT_RUN",
                      "scenario_count": len(selected), "live_accuracy_evidence": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
