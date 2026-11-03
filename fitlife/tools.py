"""不依赖模型或 MCP 的只读业务函数。项目目录尚非运动处方。"""

from typing import Literal

Category = Literal["all", "aerobic", "mobility"]
CATALOG_VERSION = "catalog-v1"
CATALOG = (
    {"id": "walking", "name": "步行", "category": "aerobic"},
    {"id": "gentle_mobility", "name": "轻柔关节活动", "category": "mobility"},
)


def get_activity_catalog(category: Category = "all") -> dict:
    """查询应用当前支持的运动项目；不推荐强度或时长，不保存计划或活动。"""
    if category not in {"all", "aerobic", "mobility"}:
        raise ValueError("INVALID_CATEGORY")
    return {
        "catalog_version": CATALOG_VERSION,
        "activities": [
            dict(item) for item in CATALOG if category == "all" or item["category"] == category
        ],
        "notice": "此目录仅表示应用支持的项目，不代表适合特定用户的医疗或运动建议。",
    }
