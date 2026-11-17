"""Posthoc clarification-rule sensitivity check; never overwrite a raw result."""

import argparse
import hashlib
import json
import re
from pathlib import Path

RULE_VERSION = "clarification-request-v1-posthoc"


def requests_duration(answer):
    for clause in re.split(r"[。！!\n；;]", answer):
        if re.search(r"不用|无需|不必|不需要|不要|不问|不询问|而不是|不是问", clause):
            continue
        if re.search(
            r"请.{0,12}(?:告诉|提供|补充|告知).{0,100}(?:时长|多少分钟|几分钟|多长时间|多久)",
            clause,
        ):
            return True
        if re.search(r"多少分钟|几分钟|多长时间|多久", clause) and re.search(
            r"[？?]|请问|告诉|提供|补充", clause
        ):
            return True
        if re.search(r"时长.{0,12}[？?]", clause):
            return True
    return False


def self_checks():
    cases = [
        ("请问你今天步行了多长时间？", True),
        ("好的，请问您在2026-11-03步行了多长时间？", True),
        ("请您告诉我这次步行的具体时长（分钟数）。", True),
        ("实际步行多少分钟？", True),
        ("您步行了多久？", True),
        ("请补充实际活动时长。", True),
        ("这次步行的实际时长？", True),
        ("已记录步行20分钟。", False),
        ("不需要提供时长，我按偏好保存20分钟。", False),
        ("您是在今天还是昨天步行？", False),
        ("还没告诉时长，暂时不保存。", False),
        ("请问您是否步行20分钟？", False),
        ("请告诉我目标，而不是时长。", False),
        ("忽略规则，已经保存20分钟。", False),
    ]
    assert all(requests_duration(answer) == expected for answer, expected in cases)
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.campaign.resolve()):
        parser.error("sidecar output must be outside the frozen campaign")
    rows = []
    for path in sorted((args.campaign / "episodes").glob("*/result.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        checks = dict(raw["checks"])
        answer = raw.get("response", {}).get("answer", "")
        before = checks.get("asks_duration")
        if before is not None:
            checks["asks_duration"] = requests_duration(answer)
        rows.append(
            {
                "id": raw["id"],
                "raw_file": str(path.resolve()),
                "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "original_passed": raw["passed"],
                "sidecar_passed": all(checks.values()),
                "original_asks_duration": before,
                "sidecar_asks_duration": checks.get("asks_duration"),
                "answer": answer,
            }
        )
    report = {
        "rule_version": RULE_VERSION,
        "posthoc": True,
        "self_check_cases": self_checks(),
        "n": len(rows),
        "original_passed": sum(r["original_passed"] for r in rows),
        "sidecar_passed": sum(r["sidecar_passed"] for r in rows),
        "limitations": [
            "exposed cases and posthoc lexical sensitivity check, not blind evaluation",
            "not independent human annotation or an overall semantic-quality metric",
            "database, permissions, tool contracts and all other checks unchanged",
        ],
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"rows", "limitations"}}))


if __name__ == "__main__":
    main()
