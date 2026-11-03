"""带有效期的已确认临时约束；日期参与上下文隔离，结构化限制由计划服务执行。"""

import hashlib
import json
from datetime import date

from fitlife.business import TemporaryConstraint


def applicable(values, start, end, domain=None):
    return [
        c.model_dump(mode="json", exclude_none=True)
        for raw in values.get("temporary_constraints", [])
        if (c := TemporaryConstraint.model_validate(raw)).start_date <= end
        and c.end_date >= start
        and (domain is None or domain in c.domains)
    ]


def memory_scope(version, values, day):
    return hashlib.sha256(
        json.dumps(
            [version, applicable(values, day, day)], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()


def plan_rules(values, sessions, exceptions=()):
    if not sessions:
        return [], []
    start = min(date.fromisoformat(s["day"]) for s in sessions)
    end = max(date.fromisoformat(s["day"]) for s in sessions)
    rules = applicable(values, start, end, "exercise")
    names = {c["name"] for c in rules}
    violations = [
        {"constraint": name, "rule": "unknown_exception"}
        for name in exceptions
        if name not in names
    ]
    applied = []
    for c in rules:
        if c["name"] in exceptions:
            continue
        applied.append(c["name"])
        for session in sessions:
            day = date.fromisoformat(session["day"])
            if not c["start_date"] <= str(day) <= c["end_date"]:
                continue
            if (
                c.get("available_weekdays") is not None
                and day.weekday() not in c["available_weekdays"]
            ):
                violations.append(
                    {"constraint": c["name"], "day": str(day), "rule": "available_weekdays"}
                )
            if (
                c.get("max_session_minutes") is not None
                and session["minutes"] > c["max_session_minutes"]
            ):
                violations.append(
                    {"constraint": c["name"], "day": str(day), "rule": "max_session_minutes"}
                )
            if (
                c.get("allowed_activities") is not None
                and session["activity_id"] not in c["allowed_activities"]
            ):
                violations.append(
                    {"constraint": c["name"], "day": str(day), "rule": "allowed_activities"}
                )
    return violations, applied
