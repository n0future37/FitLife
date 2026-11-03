"""比较冻结源码/数据上的请求模型策略，报告样本量与波动，不把估算当实扣。"""

import argparse
import json
import statistics
from collections import Counter
from decimal import Decimal
from pathlib import Path

from fitlife.evaluation_budget import RATES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in args.reports]
    first = reports[0]
    assert all(r["source_sha256"] == first["source_sha256"] for r in reports)
    assert all(
        r["dataset_sha256"] == first["dataset_sha256"] and r["mode"] == "live" for r in reports
    )

    def normalized(settings):
        result = {k: v for k, v in settings.items() if k != "routing_policy"}
        result["model_capabilities"] = sorted(result["model_capabilities"])
        return result

    common_settings = normalized(first["settings"])
    assert all(normalized(r["settings"]) == common_settings for r in reports)
    groups = {}
    for report, path in zip(reports, args.reports):
        project = next(p for p in path.parents if p.name == "artifacts").parent
        for case in report["results"]:
            key = case["id"] + "/" + report["routing"]
            bucket = groups.setdefault(key, [])
            models = []
            for run in case["run_ids"]:
                trace = json.loads(
                    (project / "artifacts/runs" / (run + ".json")).read_text(encoding="utf-8")
                )["trace"]
                models.extend(e for e in trace if e.get("kind") == "model")
            cost = Decimal(0)
            known = True
            for event in models:
                usage = event.get("usage") or {}
                name = event.get("requested_model")
                if (
                    name not in RATES
                    or not isinstance(usage.get("input_tokens"), int)
                    or not isinstance(usage.get("output_tokens"), int)
                ):
                    known = False
                    continue
                rate_in, rate_out = map(Decimal, RATES[name])
                cost += (
                    usage["input_tokens"] * rate_in + usage["output_tokens"] * rate_out
                ) / Decimal(1000000)
            bucket.append(
                dict(
                    passed=case["passed"],
                    seconds=case["seconds"],
                    model_attempts=case["model_attempts"],
                    tool_calls=case["tool_calls"],
                    estimated_max_group_usd=str(cost) if known else None,
                    requested_models=[m.get("requested_model") for m in models],
                    reported_models=[m.get("response_model") for m in models],
                    checks=case["checks"],
                    report=str(path),
                )
            )
    summary = dict(
        protocol="ABBA-query-2-per-policy-and-adjustment-1-per-policy",
        same_source=True,
        same_dataset=True,
        dataset_sha256=first["dataset_sha256"],
        source_sha256=first["source_sha256"],
        settings=common_settings,
        groups={},
        full_semantic_success_rate=None,
        actual_billed_usd=None,
        limitations=[
            "tiny developer-visible development sample",
            "cost estimated from requested-model max group price and reported usage; no task bill",
            "latency includes API/MCP and user controls/replay; not pure inference",
            "reported model names do not verify actual backend",
            "no statistical improvement claim",
        ],
    )
    for name, values in groups.items():
        times = [v["seconds"] for v in values]
        rule_totals = {}
        for item in values:
            for rule, passed in item["checks"].items():
                stat = rule_totals.setdefault(rule, dict(applicable=0, passed=0))
                stat["applicable"] += 1
                stat["passed"] += bool(passed)
        summary["groups"][name] = dict(
            n=len(values),
            rule_passed=sum(v["passed"] for v in values),
            seconds=dict(
                mean=statistics.mean(times),
                min=min(times),
                max=max(times),
                population_stdev=statistics.pstdev(times),
            ),
            model_attempts=sum(v["model_attempts"] for v in values),
            tool_calls=sum(v["tool_calls"] for v in values),
            requested_model_counts=dict(Counter(m for v in values for m in v["requested_models"])),
            estimated_max_group_usd_total=str(
                sum((Decimal(v["estimated_max_group_usd"]) for v in values), Decimal(0))
            )
            if all(v["estimated_max_group_usd"] is not None for v in values)
            else None,
            rules=rule_totals,
            samples=values,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Frozen model-routing comparison saved: " + str(args.output))


if __name__ == "__main__":
    main()
