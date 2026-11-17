"""Analyze a frozen, complete equal-capability architecture campaign."""

import argparse
import hashlib
import json
import zipfile
from decimal import Decimal
from pathlib import Path

from scripts.analyze_experiments_v2 import stats, summarize, tokens


def paired(rows):
    index = {(r["case"], r["repeat"], r["arm"]): r for r in rows}
    pairs = [(a, index[(a["case"], a["repeat"], "multi")]) for a in rows if a["arm"] == "unified"]
    both = [(a, b) for a, b in pairs if a["passed"] and b["passed"]]
    known = [(a, b) for a, b in both if tokens(a) is not None and tokens(b) is not None]

    def costs(side):
        return str(
            sum(
                (
                    Decimal(e.get("estimated_cny", "0"))
                    for pair in known
                    for e in pair[side].get("budget_events", [])
                ),
                Decimal(0),
            )
        )

    return {
        "n_pairs": len(pairs),
        "both_contract_pass": len(both),
        "unified_only_pass": sum(a["passed"] and not b["passed"] for a, b in pairs),
        "multi_only_pass": sum(b["passed"] and not a["passed"] for a, b in pairs),
        "both_fail": sum(not a["passed"] and not b["passed"] for a, b in pairs),
        "co_success_unified_seconds": stats([a["seconds"] for a, b in both]),
        "co_success_multi_seconds": stats([b["seconds"] for a, b in both]),
        "paired_multi_minus_unified_seconds": stats([b["seconds"] - a["seconds"] for a, b in both]),
        "complete_usage_co_success_pairs": len(known),
        "complete_usage_scenarios": len({a["case"] for a, b in known}),
        "unified_tokens": sum(tokens(a) for a, b in known),
        "multi_tokens": sum(tokens(b) for a, b in known),
        "unified_estimated_cny": costs(0),
        "multi_estimated_cny": costs(1),
        "paired_ids": [[a["id"], b["id"]] for a, b in both],
        "token_pair_ids": [[a["id"], b["id"]] for a, b in known],
    }


def analyze(campaign):
    protocol = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((campaign / "episodes").glob("*/result.json"))
    ]
    expected = {c["id"] for c in protocol["schedule"]}
    if {r["id"] for r in rows} != expected or len(rows) != len(expected):
        raise ValueError("CAMPAIGN_INCOMPLETE_OR_DUPLICATED")
    if (
        hashlib.sha256((campaign / "dataset.jsonl").read_bytes()).hexdigest()
        != protocol["dataset_sha256"]
    ):
        raise ValueError("DATASET_HASH_MISMATCH")
    with zipfile.ZipFile(campaign / "source.zip") as archive:
        for name, digest in protocol["source_sha256"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("SOURCE_ARCHIVE_HASH_MISMATCH")
    cases = {c["id"]: c for c in protocol["resolved_cases"]}
    groups = {}
    for label, selected in [
        ("all", rows),
        ("single_simple", [r for r in rows if cases[r["case"]]["stratum"] == "single_simple"]),
        ("cross_domain", [r for r in rows if cases[r["case"]]["stratum"] == "cross_domain"]),
        ("single_complex", [r for r in rows if cases[r["case"]]["stratum"] == "single_complex"]),
    ]:
        groups[label] = {
            arm: summarize([r for r in selected if r["arm"] == arm]) for arm in ["unified", "multi"]
        }
        groups[label]["paired"] = paired(selected)
    per_case = {
        name: {
            arm: summarize([r for r in rows if r["case"] == name and r["arm"] == arm])
            for arm in ["unified", "multi"]
        }
        for name in cases
    }
    failure_details = []
    for row in rows:
        if row["passed"]:
            continue
        folder = campaign / "episodes" / row["id"]
        reports = [
            json.loads((folder / (rid + ".json")).read_text(encoding="utf-8"))
            for rid in row.get("run_ids", [])
            if (folder / (rid + ".json")).exists()
        ]
        failure_details.append(
            {
                "id": row["id"],
                "failed_checks": [k for k, v in row["checks"].items() if not v],
                "error_type": row.get("error_type"),
                "response": row.get("response"),
                "termination_codes": [r.get("termination_code") for r in reports],
                "validation": [
                    e
                    for r in reports
                    for e in r["trace"]
                    if e.get("kind")
                    in {
                        "orchestration_validation",
                        "tool_argument_blocked",
                        "tool_repetition_blocked",
                    }
                ],
            }
        )
    output = {
        "version": "architecture-analysis-v1",
        "mode": protocol["mode"],
        "n": len(rows),
        "scenario_count": len(cases),
        "repeats": protocol["repeat"],
        "groups": groups,
        "per_case": per_case,
        "failures": failure_details,
        "all_cleaned": all(r.get("cleanup_verified") for r in rows),
        "actual_billed_cny": None,
        "limitations": [
            f"{len(cases)} exposed templates; repeated trials are not independent users",
            "state/process contracts and numeric anchors, not complete open-answer semantics",
            "whole architecture comparison bundles role prompts, tool views and coordination",
            "all failures retained; performance pairs require both arms to pass",
            "unknown usage is not zero; estimates do not apply cache discounts or verify billing",
        ],
    }
    (campaign / "architecture-analysis.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "n": len(rows),
                "groups": {
                    arm: groups["all"][arm]["strict_passed"] for arm in ["unified", "multi"]
                },
                "paired": groups["all"]["paired"],
            },
            ensure_ascii=False,
        )
    )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    analyze(parser.parse_args().campaign.resolve())
