"""Narrow deterministic guard for unambiguous positive weekday restrictions.

It does not parse general language. Explicit restrictions cannot inherit broader defaults.
Historical, negative and conflicting weekday clauses fall back to normal validation.
"""

import json
import re

DAY = r"(?:星期|礼拜|周)[一二三四五六日天]"
NUMBERS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def explicit_weekdays(message):
    if not message or re.search(
        r"上次|原来|之前|曾经|不是|不要|不用|不在|改为|改成|改到|而是|除了|排除", message
    ):
        return None
    pattern = (
        rf"(?:只|仅)(?:能|可)?(?:安排|在|选择)?\s*({DAY}(?:\s*(?:和|及|、|或|,|，)\s*{DAY})*)"
        r"(?=步行|运动|活动|训练|有空|[一二三四五六七八九十两\d]+次|[\s，。；,;]|$)"
    )
    groups = {
        tuple(sorted({NUMBERS[d[-1]] for d in re.findall(DAY, m.group(1))}))
        for m in re.finditer(pattern, message)
    }
    if len(groups) != 1:
        return None
    result = next(iter(groups))
    if {NUMBERS[d[-1]] for d in re.findall(DAY, message)} != set(result):
        return None
    return result


def weekday_conflict(message, call):
    key = {"propose_week_plan": "overrides", "propose_plan_adjustment": "options"}.get(call["name"])
    if not key:
        return None
    expected = explicit_weekdays(message)
    if expected is None:
        return None
    try:
        values = json.loads(call["arguments"]).get(key)
    except (ValueError, TypeError, AttributeError):
        return None
    supplied = values.get("available_weekdays") if isinstance(values, dict) else None
    if (
        not isinstance(supplied, list)
        or not all(type(v) is int for v in supplied)
        or set(supplied) != set(expected)
    ):
        return {
            "status": "argument_conflict",
            "error_code": "EXPLICIT_WEEKDAY_CONFLICT",
            "expected_available_weekdays": list(expected),
            "note": "原始用户明确限制了这些星期；编号0周一到6周日。请更正参数，不自动保存。",
        }
    return None


ACTIVITY_LOG_TOOLS = frozenset(
    {"get_activity_catalog_tool", "get_user_profile", "query_activities", "propose_activity"}
)


def activity_logging_scope(message):
    """Narrow only clear, standalone minute-activity logging; never infer missing facts."""
    if not message or not re.search(r"记录|录入|登记|记下", message):
        return None
    if not re.search(r"步行|轻柔活动|关节活动|walking|gentle_mobility", message):
        return None
    if re.search(
        r"计划|查询|查看|进度|目标|修改|更改|更正|删除|忘记|清除|调整|设置|保存|更新|维护|建议|分析|根据|结合|参考|"
        r"体重|身体|餐|食|步数|训练|图片|视频|约束|规则|不要记录|别记录|并|同时|另外|以及|还要",
        message,
    ):
        return None
    return ACTIVITY_LOG_TOOLS
