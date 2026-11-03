"""仅经已认证API启动的私有stdio子进程；身份和请求键不属于工具参数。"""

import logging
import os
from datetime import date
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from fitlife.business import ActivityInput, Actor, BusinessError, ForgetProfile, ProfilePatch
from fitlife.database import DatabaseSettings
from fitlife.plan_types import PlanOverrides, PlanRequest
from fitlife.planning import PlanningService
from fitlife.tools import Category, get_activity_catalog


def create_server():
    engine = DatabaseSettings().engine()
    actor = Actor(UUID(os.environ["FITLIFE_TRUSTED_USER_ID"]))
    request_key = str(UUID(os.environ["FITLIFE_TRUSTED_REQUEST_KEY"]))
    key = "agent:" + request_key
    service = PlanningService(
        engine, request_fence=(request_key, str(UUID(os.environ["FITLIFE_TRUSTED_REQUEST_NONCE"])))
    )
    service.collect_candidates = os.getenv("FITLIFE_COLLECT_CANDIDATES") == "1"
    request_day = date.fromisoformat(os.environ["FITLIFE_TRUSTED_REQUEST_DAY"])
    server = FastMCP("fitlife-business")
    read_plan_versions = {}
    read_adjustment_contexts = {}

    def call(function, *args):
        try:
            return function(*args)
        except BusinessError as exc:
            return {"status": "error", "error_code": exc.code, "retryable": False}
        except Exception:
            raise ToolError("BUSINESS_TOOL_FAILED") from None

    @server.tool()
    def get_activity_catalog_tool(category: Category = "all") -> dict:
        """查询应用支持的活动项目。只读，不说明个人是否适合。"""
        return get_activity_catalog(category)

    @server.tool()
    def get_user_profile() -> dict:
        """查询本人已确认目标和运动偏好，包含字段来源和版本；空值不能猜测。"""
        return call(service.get_profile, actor)

    @server.tool()
    def query_activities(
        start_date: date, end_date: date, limit: int = 100, offset: int = 0
    ) -> dict:
        """查询本人指定起止日期（含首尾）的活动和已记录分钟数，最多366天；无记录不等于零活动。"""
        return call(service.list_activities, actor, start_date, end_date, limit, offset)

    @server.tool()
    def propose_activity_deletion(record_id: UUID) -> dict:
        """提出删除一条本人活动的候选，必须先查询本人记录找准确ID；等待用户确认才删除。"""
        return call(service.propose_activity_deletion, actor, record_id, key)

    @server.tool()
    def propose_profile_forget(data: ForgetProfile) -> dict:
        """仅用户明确要求时，提出忘记已保存画像字段的候选；确认前不删除。"""
        return call(service.propose, actor, "profile_forget", data.model_dump(mode="json"), key)

    @server.tool()
    def propose_profile_update(patch: ProfilePatch) -> dict:
        """仅根据本轮用户明确提供的信息提出画像更新候选；不是保存，等待页面确认。"""
        if "temporary_constraints" in patch.model_fields_set:
            return {"status": "error", "error_code": "USE_DEDICATED_CONSTRAINT_TOOL"}
        return call(
            service.propose,
            actor,
            "profile_patch",
            patch.model_dump(mode="json", exclude_unset=True),
            key,
        )

    @server.tool()
    def propose_activity(data: ActivityInput) -> dict:
        """提出一条实际活动候选，项目walking/gentle_mobility，日期和分钟数需准确，不猜时长。
        未给日期时表示本轮请求的上海日期；等待用户确认，不保存活动、不确认其他候选。
        """
        payload = data.model_dump(mode="json")
        if "occurred_on" not in data.model_fields_set:
            payload["occurred_on"] = request_day.isoformat()
        return call(service.propose, actor, "record_activity", payload, key)

    @server.tool()
    def get_current_plan(week_start: date) -> dict:
        """查询本人指定周一开始的当前周计划及版本；生成/调整该周计划前必须先查。"""
        result = call(service.get_plan, actor, week_start)
        if "version" in result:
            read_plan_versions[week_start] = result["version"]
        return result

    @server.tool()
    def propose_week_plan(week_start: date, overrides: PlanOverrides | None = None) -> dict:
        """按已确认画像/现有计划及本轮明确修改生成草案候选，不保存。须先get_current_plan。
        overrides只填用户明确的新要求；缺信息或天数冲突返回结构化状态，不猜测。"""
        if week_start not in read_plan_versions:
            return {
                "status": "dependency_required",
                "required_tool": "get_current_plan",
                "week_start": week_start.isoformat(),
            }
        if service.collect_candidates and read_plan_versions[week_start] > 0:
            return {
                "status": "dependency_required",
                "required_tool": "get_plan_adjustment_context",
                "next_tool": "propose_plan_adjustment",
                "note": "主从模式修改已有计划必须核对活动证据。",
            }
        values = overrides.model_dump(exclude_none=True) if overrides else {}
        request = PlanRequest(
            week_start=week_start, expected_plan_version=read_plan_versions[week_start], **values
        )
        return call(service.propose_plan, actor, request, key)

    @server.tool()
    def get_plan_progress(week_start: date) -> dict:
        """查询本人周计划及按同日同项目匹配的进度；额外时长不冒充计划完成，未记录不等于实际未运动。"""
        return call(service.get_progress, actor, week_start)

    from fitlife.adjustments import AdjustmentOptions, AdjustmentRequest
    from fitlife.adjustments import context as adjustment_context
    from fitlife.adjustments import propose as adjustment_propose

    @server.tool()
    def get_plan_adjustment_context(
        week_start: date, reference_week_start: date | None = None
    ) -> dict:
        """调整前必查：本人目标周原计划、参考周实际记录与画像，含版本/证据摘要；未记录不等于未运动。"""
        result = call(
            adjustment_context, service, actor, week_start, reference_week_start, request_day
        )
        if "evidence_digest" in result:
            read_adjustment_contexts[(week_start, reference_week_start or week_start)] = result
        return result

    @server.tool()
    def propose_plan_adjustment(week_start: date, options: AdjustmentOptions) -> dict:
        """按本轮明确要求调整未来安排并展示差异/依据；保留过去及已有记录项目，等待确认。
        须先get_plan_adjustment_context，同一目标周和参考周；remaining_sessions不含保留的过去项目。
        """
        reference = options.reference_week_start or week_start
        evidence = read_adjustment_contexts.get((week_start, reference))
        if evidence is None:
            return {"status": "dependency_required", "required_tool": "get_plan_adjustment_context"}
        request = AdjustmentRequest(
            week_start=week_start,
            expected_plan_version=evidence["plans"][str(week_start)]["version"],
            expected_evidence_digest=evidence["evidence_digest"],
            **options.model_dump(exclude_none=True),
        )
        return call(adjustment_propose, service, actor, request, key, request_day)

    from fitlife.domains import register_domain_tools

    domain_service = register_domain_tools(
        server, engine, actor, key, request_day, call, service.collect_candidates
    )
    domain_service.request_fence = service.request_fence

    from fitlife.domain_types import BundleOperation

    @server.tool()
    def propose_action_bundle(operations: list[BundleOperation]) -> dict:
        """仅主编排器白名单可用：汇总最多3项已校验候选，确认后原子保存；不直接确认。"""
        return call(
            domain_service.collect_proposal,
            actor,
            [op.model_dump(mode="json") for op in operations],
            key,
        )

    return server, engine


if __name__ == "__main__":
    # 避免SDK/校验异常把个人工具参数或数据库错误正文写到stderr。
    logging.disable(logging.CRITICAL)
    server, engine = create_server()
    try:
        server.run(transport="stdio")
    finally:
        engine.dispose()
