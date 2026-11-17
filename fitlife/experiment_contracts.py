"""Versioned tool contracts: strict v1 and predeclared semantic plan equivalence v2."""

from fitlife.task_eval import subset


def equal_field(key, actual, wanted):
    if key == "available_weekdays":
        return (
            isinstance(actual, list)
            and all(type(v) is int for v in actual)
            and len(actual) == len(set(actual))
            and set(actual) == set(wanted)
        )
    return actual == wanted


def profile_equivalent(call, wanted, events, checks):
    if wanted["tool"] != "propose_week_plan" or call["name"] != "propose_week_plan":
        return False
    args = call.get("arguments", {})
    if args.get("week_start") != wanted["arguments"].get("week_start"):
        return False
    supplied = args.get("overrides")
    supplied = {} if supplied is None else supplied
    expected = wanted["arguments"].get("overrides", {})
    if (
        not isinstance(supplied, dict)
        or set(expected)
        != {"activity_id", "available_weekdays", "weekly_sessions", "session_minutes"}
        or set(supplied) - set(expected)
        or any(not equal_field(k, value, expected[k]) for k, value in supplied.items())
    ):
        return False
    if (
        call.get("is_error")
        or call.get("result", {}).get("status") != "proposed"
        or checks.get("expected_final_state") is not True
        or checks.get("critical_order") is not True
    ):
        return False
    if set(supplied) == set(expected):
        return True
    for event in events:
        if (
            event["name"] != "get_user_profile"
            or event.get("is_error")
            or event.get("finished_ns", float("inf")) > call.get("started_ns", -1)
        ):
            continue
        result = event.get("result", {})
        values = result.get("values", {})
        inherited = {k: values.get(k) for k in expected if k != "activity_id"}
        preferred = values.get("preferred_activities")
        inherited["activity_id"] = (
            preferred[0] if isinstance(preferred, list) and len(preferred) == 1 else None
        )
        merged = {**inherited, **supplied}
        if not all(equal_field(k, merged[k], value) for k, value in expected.items()):
            continue
        payload = call.get("result", {}).get("proposal", {}).get("payload", {})
        if (
            result.get("version") is not None
            and payload.get("source_profile_version") == result["version"]
        ):
            return True
    return False


def tool_contract_matches(step, events, checks, policy="strict-v1"):
    names = {step["tool"]}
    if step["tool"] == "save_body_profile_tool":
        names.add("record_height_tool")
    if any(
        e["name"] in names
        and not e.get("is_error")
        and subset(e.get("arguments"), step["arguments"])
        for e in events
    ):
        return True
    return policy == "profile-equivalent-v2" and any(
        profile_equivalent(e, step, events, checks) for e in events
    )
