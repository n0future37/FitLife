"""Read one frozen campaign; never pool pilots or substitute missing usage with zero."""

import argparse
import hashlib
import json
import statistics
import zipfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path


def stats(values):
    return {
        "n": len(values),
        "mean": statistics.mean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "stdev_population": statistics.pstdev(values) if values else None,
    }


def tokens(result):
    events = result.get("budget_events", [])
    if not events:
        return None
    if not all(
        e.get("usage")
        and type(e["usage"].get("input_tokens")) is int
        and type(e["usage"].get("output_tokens")) is int
        for e in events
    ):
        return None
    return sum(e["usage"]["input_tokens"] + e["usage"]["output_tokens"] for e in events)


def summarize(rows):
    attempts = [e for r in rows for e in r.get("budget_events", [])]
    checks = sorted({k for r in rows for k in r["checks"]})
    overlap = []
    for r in rows:
        intervals = r.get("delegate_intervals", [])
        for i, a in enumerate(intervals):
            for b in intervals[i + 1 :]:
                if all(k in e for e in (a, b) for k in ("started_ns", "finished_ns")):
                    overlap.append(
                        max(
                            0,
                            min(a["finished_ns"], b["finished_ns"])
                            - max(a["started_ns"], b["started_ns"]),
                        )
                        / 1e9
                    )
    return {
        "n": len(rows),
        "strict_passed": sum(bool(r["passed"]) for r in rows),
        "outcome_passed": sum(bool(r["outcome_passed"]) for r in rows),
        "latency_seconds": stats([r["seconds"] for r in rows if "seconds" in r]),
        "model_attempts": len(attempts),
        "requested_models": dict(Counter(e["model"] for e in attempts)),
        "known_tokens": sum(
            e["usage"]["input_tokens"] + e["usage"]["output_tokens"]
            for e in attempts
            if e.get("usage")
            and type(e["usage"].get("input_tokens")) is int
            and type(e["usage"].get("output_tokens")) is int
        ),
        "fully_known_usage_episodes": sum(tokens(r) is not None for r in rows),
        "estimated_known_cny": str(
            sum((Decimal(e.get("estimated_cny", "0")) for e in attempts), Decimal(0))
        ),
        "unknown_usage_attempts": sum(e.get("usage") is None for e in attempts),
        "provider_errors": dict(
            Counter(
                str(e.get("http_status")) + ":" + e.get("error_category", "")
                for e in attempts
                if e.get("error_type")
            )
        ),
        "tools": stats([r["tool_calls"] for r in rows if "tool_calls" in r]),
        "steps": stats([r["steps"] for r in rows if type(r.get("steps")) is int]),
        "tool_contract": {
            "matched": sum(r.get("tool_contract_matches", 0) for r in rows),
            "total": sum(r.get("tool_contract_total", 0) for r in rows),
        },
        "checks": {
            k: {
                "passed": sum(r["checks"].get(k) is True for r in rows),
                "applicable": sum(k in r["checks"] for r in rows),
            }
            for k in checks
        },
        "delegate_overlap_seconds": stats(overlap),
        "failed_ids": [r["id"] for r in rows if not r["passed"]],
    }


def analyze(campaign, matched=False):
    protocol = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((campaign / "episodes").glob("*/result.json"))
    ]
    expected = {c["id"] for c in protocol["schedule"]}
    actual = {r["id"] for r in rows}
    if actual != expected or len(rows) != len(actual):
        raise ValueError("CAMPAIGN_INCOMPLETE_OR_DUPLICATED")
    original_rows = rows[:]
    audit_path = campaign / "operational-censoring.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    removed_ids = []
    if matched:
        censored = set(audit.get("excluded_resource_ids", []))
        if not censored:
            raise ValueError("MATCHED_COMPARISON_REQUIRES_EXPLICIT_CENSORING_AUDIT")
        bad_pairs = {(r["case"], r["repeat"]) for r in rows if r["id"] in censored}
        removed_ids = [r["id"] for r in rows if (r["case"], r["repeat"]) in bad_pairs]
        rows = [r for r in rows if r["id"] not in removed_ids]
    if (
        hashlib.sha256((campaign / "dataset.jsonl").read_bytes()).hexdigest()
        != protocol["dataset_sha256"]
    ):
        raise ValueError("DATASET_HASH_MISMATCH")
    with zipfile.ZipFile(campaign / "source.zip") as archive:
        if (
            hashlib.sha256(
                archive.read(protocol.get("dataset_path", "evals/experiments-v1.jsonl"))
            ).hexdigest()
            != protocol["dataset_sha256"]
        ):
            raise ValueError("ARCHIVED_DATASET_HASH_MISMATCH")
        for name, digest in protocol["source_sha256"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("SOURCE_ARCHIVE_HASH_MISMATCH")
    grouped, cases = defaultdict(list), defaultdict(list)
    for r in rows:
        for suffix in ("all", r["split"]):
            grouped[f"{r['experiment']}/{r['arm']}/{suffix}"].append(r)
        cases[f"{r['case']}/{r['arm']}"].append(r)
    paired = {}
    for experiment, baseline, alternatives in (
        ("N1", "serial", ["dag"]),
        ("N2", "fixed_strong", ["routed"]),
        ("N3", "recent", ["none", "focused"]),
    ):
        for alternative in alternatives:
            index = {
                (r["case"], r["repeat"], r["arm"]): r for r in rows if r["experiment"] == experiment
            }
            pairs = [
                (r, index[(r["case"], r["repeat"], alternative)])
                for r in rows
                if r["experiment"] == experiment and r["arm"] == baseline
            ]
            both = [(a, b) for a, b in pairs if a["passed"] and b["passed"]]
            known = [(a, b) for a, b in both if tokens(a) is not None and tokens(b) is not None]
            paired[f"{experiment}/{baseline}->{alternative}"] = {
                "n": len(pairs),
                "both_strict_success": len(both),
                "baseline_only_success": sum(a["passed"] and not b["passed"] for a, b in pairs),
                "alternative_only_success": sum(b["passed"] and not a["passed"] for a, b in pairs),
                "both_failed": sum(not a["passed"] and not b["passed"] for a, b in pairs),
                "both_success_latency_baseline_seconds": stats([a["seconds"] for a, b in both]),
                "both_success_latency_alternative_seconds": stats([b["seconds"] for a, b in both]),
                "both_success_latency_difference_seconds": stats(
                    [b["seconds"] - a["seconds"] for a, b in both]
                ),
                "both_success_complete_usage_pairs": len(known),
                "paired_baseline_estimated_cny": str(
                    sum(
                        (
                            Decimal(e.get("estimated_cny", "0"))
                            for a, b in known
                            for e in a.get("budget_events", [])
                        ),
                        Decimal(0),
                    )
                ),
                "paired_alternative_estimated_cny": str(
                    sum(
                        (
                            Decimal(e.get("estimated_cny", "0"))
                            for a, b in known
                            for e in b.get("budget_events", [])
                        ),
                        Decimal(0),
                    )
                ),
                "paired_baseline_tokens": sum(tokens(a) for a, b in known),
                "paired_alternative_tokens": sum(tokens(b) for a, b in known),
            }
    failure_evidence = {}
    for row in rows:
        if row["passed"]:
            continue
        folder = campaign / "episodes" / row["id"]
        run_reports = [
            json.loads((folder / (rid + ".json")).read_text(encoding="utf-8"))
            for rid in row.get("run_ids", [])
            if (folder / (rid + ".json")).exists()
        ]
        invalid_arguments = []
        for path in (folder / "model-replies").glob("*.json"):
            reply = json.loads(path.read_text(encoding="utf-8"))
            for call in reply.get("calls", []):
                try:
                    value = json.loads(call["arguments"])
                    valid = isinstance(value, dict)
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    invalid_arguments.append(
                        {"artifact": path.relative_to(campaign).as_posix(), "tool": call["name"]}
                    )
        failure_evidence[row["id"]] = {
            "termination_codes": [r.get("termination_code") for r in run_reports],
            "orchestration_validation": [
                e
                for r in run_reports
                for e in r["trace"]
                if e.get("kind") == "orchestration_validation"
            ],
            "provider_errors": [
                {
                    k: e.get(k)
                    for k in (
                        "model",
                        "error_type",
                        "http_status",
                        "provider_error_code",
                        "provider_error_message",
                    )
                }
                for e in row.get("budget_events", [])
                if e.get("error_type")
            ],
            "invalid_argument_payloads": invalid_arguments,
            "raw_reply_files": sorted(
                p.relative_to(campaign).as_posix()
                for p in (folder / "model-replies").glob("*.json")
            ),
        }
    result = {
        "original_n": len(original_rows),
        "included_ids": [r["id"] for r in rows],
        "removed_ids": removed_ids,
        "study_budget_censoring": audit,
        "comparison_requires_complete_case_repeat_arms": matched,
        "failure_evidence": failure_evidence,
        "campaign": campaign.name,
        "mode": protocol["mode"],
        "n": len(rows),
        "repeat": protocol["repeat"],
        "case_count": len(protocol["resolved_cases"]),
        "dataset_sha256": protocol["dataset_sha256"],
        "source_sha256": protocol["source_sha256"],
        "all_cleaned": all(r.get("cleanup_verified") is True for r in rows),
        "groups": {k: summarize(v) for k, v in grouped.items()},
        "cases": {k: summarize(v) for k, v in cases.items()},
        "paired": paired,
        "failures": [
            {
                "id": r["id"],
                "run_ids": r.get("run_ids", []),
                "failed_checks": [k for k, v in r["checks"].items() if not v],
                "error_type": r.get("error_type"),
                "end_reason": r.get("response", {}).get("end_reason"),
                "answer": r.get("response", {}).get("answer"),
                "planned_graphs": r.get("planned_graphs"),
                "tool_calls": [
                    {
                        "name": e["name"],
                        "arguments": e.get("arguments"),
                        "is_error": e.get("is_error"),
                    }
                    for e in r.get("tool_events", [])
                ],
            }
            for r in rows
            if not r["passed"]
        ],
        "actual_billed_cny": None,
        "limitations": [
            "Synthetic 12-case benchmark; repeats are not independent users.",
            "Executable state/process contracts, not full semantic answer grading.",
            "No failed runs removed; paired speed/token comparison requires both arms to pass.",
            "Public-price estimate, not verified account billing; unknown usage is not zero.",
        ],
    }
    (campaign / ("comparison.json" if matched else "analysis.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    columns = [
        "experiment/arm",
        "rule pass",
        "outcome pass",
        "latency mean±sd (s)",
        "known CNY",
        "unknown calls",
    ]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for k, v in sorted(result["groups"].items()):
        if not k.endswith("/all"):
            continue
        t = v["latency_seconds"]
        lines.append(
            f"| {k[:-4]} | {v['strict_passed']}/{v['n']} | {v['outcome_passed']}/{v['n']} | "
            f"{t['mean']:.3f} ± {t['stdev_population']:.3f} | "
            f"{v['estimated_known_cny']} | {v['unknown_usage_attempts']} |"
        )
    table = "\n".join(lines) + "\n"
    (campaign / ("comparison-table.md" if matched else "table.md")).write_text(
        table, encoding="utf-8"
    )
    print(table)
    print("CLEANUP", result["all_cleaned"], "N", len(rows))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--matched-study-budget", action="store_true")
    args = parser.parse_args()
    analyze(args.campaign.resolve(), matched=args.matched_study_budget)
