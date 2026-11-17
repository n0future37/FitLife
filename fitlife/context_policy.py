"""实验用完整回合筛选；指代不清或跨域时保留完整历史，默认产品策略不变。"""

WORDS = {
    "body": ("体重", "体脂", "身高", "腰围", "睡眠", "疲劳", "身体", "公斤"),
    "exercise": ("运动", "步行", "步数", "训练", "计划", "活动", "分钟", "器械"),
    "diet": ("饮食", "餐食", "食品", "食物", "热量", "营养", "蛋白质", "早餐", "午餐", "晚餐"),
}


def domains(text):
    return {name for name, words in WORDS.items() if any(word in text for word in words)}


def select_history(history, message, policy):
    if policy == "none":
        return []
    if policy == "recent" or not history:
        return list(history)
    if policy != "focused":
        raise ValueError("UNKNOWN_HISTORY_POLICY")
    wanted = domains(message)
    if len(wanted) != 1 or len(history) % 2:
        return list(history)
    pairs = [history[i : i + 2] for i in range(0, len(history), 2)]
    if any(p[0].get("role") != "user" or p[1].get("role") != "assistant" for p in pairs):
        return list(history)
    return [
        item
        for i, pair in enumerate(pairs)
        if i == len(pairs) - 1 or domains(str(pair[0].get("content", ""))) & wanted
        for item in pair
    ]
