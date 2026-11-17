"""Post-hoc v1.1 contract audit. Preserve all v1 outputs; no new model calls."""

import argparse
import copy
import hashlib
import json
from collections import defaultdict
from pathlib import Path

VERSION = "contract-equivalence-v1.1-posthoc"


def subset(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and subset(actual[k], v) for k, v in expected.items()
        )
    return actual == expected


def profile_equivalent(call, wanted, events, checks):
    if wanted["tool"] != "propose_week_plan" or call["name"] != "propose_week_plan":
        return False
    args = call.get("arguments", {})
    if (
        args.get("week_start") != wanted["arguments"].get("week_start")
        or args.get("overrides") is not None
    ):
        return False
    if call.get("is_error") or call.get("result", {}).get("status") != "proposed":
        return False
    if checks.get("expected_final_state") is not True or checks.get("critical_order") is not True:
        return False
    expected = wanted["arguments"].get("overrides", {})
    if set(expected) != {"activity_id", "available_weekdays", "weekly_sessions", "session_minutes"}:
        return False
    for event in events:
        if (
            event["name"] != "get_user_profile"
            or event.get("is_error")
            or event.get("finished_ns", float("inf")) > call.get("started_ns", -1)
        ):
            continue
        result = event.get("result", {})
        values = result.get("values", {})
        if values.get("preferred_activities") != [expected["activity_id"]] or any(
            values.get(k) != expected[k]
            for k in ("available_weekdays", "weekly_sessions", "session_minutes")
        ):
            continue
        proposal = call.get("result", {}).get("proposal", {})
        if (
            proposal.get("payload", {}).get("source_profile_version") == result.get("version")
            and result.get("version") is not None
        ):
            return True
    return False


def recompute(row, case):
    checks = dict(row["checks"])
    events = row.get("tool_events", [])
    matches, accepted = [], []
    for script in case["script"].values():
        for wanted in script:
            names = {wanted["tool"]}
            if wanted["tool"] == "save_body_profile_tool":
                names.add("record_height_tool")
            direct = any(
                e["name"] in names
                and not e.get("is_error")
                and subset(e.get("arguments"), wanted["arguments"])
                for e in events
            )
            equivalent = not direct and any(
                profile_equivalent(e, wanted, events, checks) for e in events
            )
            matches.append(direct or equivalent)
            if equivalent:
                accepted.append(
                    {
                        "tool": wanted["tool"],
                        "reason": "exact profile, prior read, plan/version and order verified",
                    }
                )
    # Do not erase exceptional worker failures or synthesize absent checks.
    if "tool_parameter_contract" in checks:
        checks["tool_parameter_contract"] = all(matches)
    return {
        "id": row["id"],
        "experiment": row["experiment"],
        "arm": row["arm"],
        "split": row["split"],
        "original_passed": row["passed"],
        "rescored_passed": row["passed"]
        or (
            bool(accepted)
            and not row.get("error_type")
            and bool(checks)
            and all(checks.values())
            and row.get("cleanup_verified") is True
        ),
        "accepted_equivalences": accepted,
        "checks": checks,
    }


def self_check():
    wanted = {
        "tool": "propose_week_plan",
        "arguments": {
            "week_start": "2026-11-09",
            "overrides": {
                "activity_id": "walking",
                "available_weekdays": [1, 5],
                "weekly_sessions": 2,
                "session_minutes": 15,
            },
        },
    }
    read = {
        "name": "get_user_profile",
        "finished_ns": 10,
        "result": {
            "version": 1,
            "values": {
                "preferred_activities": ["walking"],
                "available_weekdays": [1, 5],
                "weekly_sessions": 2,
                "session_minutes": 15,
            },
        },
    }
    call = {
        "name": "propose_week_plan",
        "started_ns": 20,
        "arguments": {"week_start": "2026-11-09", "overrides": None},
        "result": {"status": "proposed", "proposal": {"payload": {"source_profile_version": 1}}},
    }
    checks = {"expected_final_state": True, "critical_order": True}
    assert profile_equivalent(call, wanted, [read], checks)
    wrong = copy.deepcopy(read)
    wrong["result"]["values"]["session_minutes"] = 20
    assert not profile_equivalent(call, wanted, [wrong], checks)
    assert not profile_equivalent(call, wanted, [], checks)
    assert not profile_equivalent(call, wanted, [{**read, "finished_ns": 30}], checks)
    assert not profile_equivalent(call, wanted, [read], {**checks, "expected_final_state": False})
    wrong_call = copy.deepcopy(call)
    wrong_call["result"]["proposal"]["payload"]["source_profile_version"] = 2
    assert not profile_equivalent(wrong_call, wanted, [read], checks)
    case = {"script": {"exercise": [wanted]}}
    row = {
        "id": "fixture",
        "experiment": "N2",
        "arm": "fixed_strong",
        "split": "holdout",
        "checks": {**checks, "tool_parameter_contract": False, "no_unconfirmed_write": True},
        "tool_events": [read, call],
        "passed": False,
        "cleanup_verified": True,
    }
    assert recompute(row, case)["rescored_passed"]
    wrong_row = copy.deepcopy(row)
    wrong_row["checks"]["no_unconfirmed_write"] = False
    assert not recompute(wrong_row, case)["rescored_passed"]
    wrong_row = copy.deepcopy(row)
    wrong_row["error_type"] = "FixtureFailure"
    assert not recompute(wrong_row, case)["rescored_passed"]
    print("9 contract equivalence checks passed")


def main(campaign):
    protocol = json.loads((campaign / "protocol.json").read_text(encoding="utf-8"))
    summary = json.loads((campaign / "summary.json").read_text(encoding="utf-8"))
    if {r["id"] for r in summary["results"]} != {c["id"] for c in protocol["schedule"]}:
        raise ValueError("INCOMPLETE_CAMPAIGN")
    cases = {c["id"]: c for c in protocol["resolved_cases"]}
    rows = [recompute(r, cases[r["case"]]) for r in summary["results"]]
    groups = defaultdict(lambda: {"n": 0, "original_passed": 0, "rescored_passed": 0})
    for r in rows:
        for split in ("all", r["split"]):
            g = groups[f"{r['experiment']}/{r['arm']}/{split}"]
            g["n"] += 1
            g["original_passed"] += r["original_passed"]
            g["rescored_passed"] += r["rescored_passed"]
    comparison_path = campaign / "comparison.json"
    included = (
        set(json.loads(comparison_path.read_text(encoding="utf-8"))["included_ids"])
        if comparison_path.exists()
        else {r["id"] for r in rows}
    )
    comparable = defaultdict(lambda: {"n": 0, "original_passed": 0, "rescored_passed": 0})
    for r in rows:
        if r["id"] not in included:
            continue
        for split in ("all", r["split"]):
            g = comparable[f"{r['experiment']}/{r['arm']}/{split}"]
            g["n"] += 1
            g["original_passed"] += r["original_passed"]
            g["rescored_passed"] += r["rescored_passed"]
    output = {
        "comparison_groups": dict(comparable),
        "version": VERSION,
        "posthoc": True,
        "reason": (
            "v1 rejected exact-profile reuse instead of redundant overrides; "
            "observed during holdout, not a pre-registered blind score"
        ),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "original_summary_sha256": hashlib.sha256(
            (campaign / "summary.json").read_bytes()
        ).hexdigest(),
        "groups": dict(groups),
        "rows": rows,
    }
    (campaign / "rescored-v1.1.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(dict(groups), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, nargs="?")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
    elif args.campaign:
        main(args.campaign.resolve())
    else:
        parser.error("provide campaign or --self-check")
