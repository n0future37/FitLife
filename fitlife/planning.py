"""确定性周计划基线与进度口径；模型不负责计算完成率或直接保存。"""

import hashlib
import json
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from fitlife.business import BusinessError, BusinessService, snapshot, utcnow
from fitlife.database import Activity, PendingAction, Plan, PlanRevision, Profile
from fitlife.plan_types import PlanPayload, PlanRequest, monday


def week_date(value):
    try:
        return monday(date.fromisoformat(str(value)))
    except ValueError:
        raise BusinessError("INVALID_WEEK_START") from None


def current_plan(session, owner, week_start):
    plan = session.scalar(select(Plan).where(Plan.owner_id == owner, Plan.week_start == week_start))
    if plan is None:
        return {"week_start": week_start.isoformat(), "version": 0, "plan": None}
    revision = session.get(PlanRevision, (plan.id, plan.version))
    if revision is None:
        raise BusinessError("PLAN_HISTORY_MISSING")
    return {
        "plan_id": plan.id,
        "week_start": week_start.isoformat(),
        "version": plan.version,
        "plan": revision.payload,
    }


def save_plan(session, owner, action, payload):
    if action.kind == "adjust_plan":
        from fitlife.adjustments import valid

        if not valid(session, owner, payload):
            return None
    profile = session.get(Profile, owner)
    if (profile.version if profile else 0) != action.base_profile_version:
        return None
    from fitlife.constraints import plan_rules

    violations, _ = plan_rules(
        profile.values if profile else {},
        payload["sessions"],
        payload.get("temporary_constraint_exceptions", []),
    )
    if violations:
        return None
    week_start = date.fromisoformat(payload["week_start"])
    plan = session.scalar(select(Plan).where(Plan.owner_id == owner, Plan.week_start == week_start))
    if (plan.version if plan else 0) != action.base_plan_version:
        return None
    if plan is None:
        plan = Plan(
            id=str(uuid4()), owner_id=owner, week_start=week_start, version=1, updated_at=utcnow()
        )
        session.add(plan)
        session.flush()
    else:
        plan.version += 1
        plan.updated_at = utcnow()
    session.add(
        PlanRevision(
            plan_id=plan.id,
            version=plan.version,
            source_action_id=action.id,
            payload=payload,
            created_at=utcnow(),
        )
    )
    return {"plan_id": plan.id, "plan_version": plan.version, "week_start": payload["week_start"]}


class PlanningService(BusinessService):
    def get_plan(self, actor, week_start):
        week_start = week_date(week_start)
        with self.sessions() as session:
            owner = self._user(session, actor)
            return current_plan(session, owner, week_start)

    def propose_plan(self, actor, request: PlanRequest, request_key):
        request_values = request.model_dump(mode="json")
        if request_values.get("temporary_constraint_exceptions") is None:
            request_values.pop("temporary_constraint_exceptions", None)
        request_hash = hashlib.sha256(
            json.dumps(request_values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.sessions() as session:
            owner = self._user(session, actor)
            previous = session.scalar(
                select(PendingAction).where(
                    PendingAction.owner_id == owner,
                    PendingAction.request_key == request_key,
                    PendingAction.kind == "save_plan",
                )
            )
            if previous:
                if previous.request_fingerprint != request_hash:
                    raise BusinessError("IDEMPOTENCY_CONFLICT")
                return snapshot(previous)
            profile = session.get(Profile, owner)
            preferences = profile.values if profile else {}
            profile_version = profile.version if profile else 0
            existing = current_plan(session, owner, request.week_start)
        if existing["version"] != request.expected_plan_version:
            raise BusinessError("PLAN_VERSION_CONFLICT")
        old_sessions = existing["plan"]["sessions"] if existing["plan"] else []
        old_by_day = {date.fromisoformat(s["day"]).weekday(): s for s in old_sessions}
        days = request.available_weekdays
        if days is None:
            days = tuple(sorted(set(old_by_day) | set(preferences.get("available_weekdays", []))))
        count = request.weekly_sessions or len(old_sessions) or preferences.get("weekly_sessions")
        missing = []
        if not days:
            missing.append("available_weekdays")
        if not count:
            missing.append("weekly_sessions")
        default_minutes = request.session_minutes or preferences.get("session_minutes")
        default_activity = request.activity_id or next(
            iter(preferences.get("preferred_activities", [])), None
        )
        if not old_sessions and not default_minutes:
            missing.append("session_minutes")
        if not old_sessions and not default_activity:
            missing.append("preferred_activities")
        if missing:
            return {"status": "needs_information", "missing_fields": missing}
        if count > len(days):
            return {
                "status": "schedule_conflict",
                "required_sessions": count,
                "available_days": len(days),
            }
        preferred = sorted(set(old_by_day) & set(days))
        chosen = sorted((preferred + sorted(set(days) - set(preferred)))[:count])
        sessions = []
        for weekday in chosen:
            old = old_by_day.get(weekday, {})
            minutes = request.session_minutes or old.get("minutes") or default_minutes
            activity = request.activity_id or old.get("activity_id") or default_activity
            if minutes is None or activity is None:
                return {
                    "status": "needs_information",
                    "missing_fields": ["session_minutes", "preferred_activities"],
                }
            sessions.append(
                {
                    "day": (request.week_start + timedelta(days=weekday)).isoformat(),
                    "activity_id": activity,
                    "minutes": minutes,
                }
            )
        from fitlife.constraints import plan_rules

        violations, applied = plan_rules(
            preferences, sessions, request.temporary_constraint_exceptions or ()
        )
        if violations:
            return {"status": "constraint_conflict", "violations": violations}
        if old_sessions == sessions and not request.temporary_constraint_exceptions:
            return {"status": "unchanged", **existing}
        overrides = tuple(
            name
            for name in ("activity_id", "available_weekdays", "weekly_sessions", "session_minutes")
            if getattr(request, name) is not None
        )
        payload = PlanPayload(
            week_start=request.week_start,
            sessions=sessions,
            source_profile_version=profile_version,
            source_plan_version=existing["version"],
            explicit_overrides=overrides,
            temporary_constraint_exceptions=request.temporary_constraint_exceptions or (),
            temporary_constraints_applied=applied,
        ).model_dump(mode="json")
        return self.propose(
            actor,
            "save_plan",
            payload,
            request_key,
            expected_plan_version=request.expected_plan_version,
            request_fingerprint=request_hash,
        )

    def get_progress(self, actor, week_start):
        week_start = week_date(week_start)
        # 与所有本用户业务写入采用相同短锁，计划版本和活动聚合来自一致状态。
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            plan = current_plan(session, owner, week_start)
            if plan["plan"] is None:
                return {"status": "no_plan", **plan}
            rows = session.execute(
                select(
                    Activity.occurred_on,
                    Activity.activity_id,
                    func.sum(Activity.minutes),
                    func.count(Activity.id),
                )
                .where(
                    Activity.owner_id == owner,
                    Activity.occurred_on.between(week_start, week_start + timedelta(days=6)),
                )
                .group_by(Activity.occurred_on, Activity.activity_id)
            ).all()
            actual = {
                (day.isoformat(), activity): int(minutes) for day, activity, minutes, _ in rows
            }
            sessions = []
            for target in plan["plan"]["sessions"]:
                minutes = actual.get((target["day"], target["activity_id"]), 0)
                sessions.append(
                    {
                        **target,
                        "recorded_minutes": minutes,
                        "matched_minutes": min(minutes, target["minutes"]),
                        "status": "completed"
                        if minutes >= target["minutes"]
                        else "partial"
                        if minutes
                        else "unrecorded",
                    }
                )
            planned = sum(s["minutes"] for s in sessions)
            matched = sum(s["matched_minutes"] for s in sessions)
            recorded = sum(int(row[2]) for row in rows)
            return {
                "status": "ok",
                "week_start": week_start.isoformat(),
                "plan_version": plan["version"],
                "sessions": sessions,
                "planned_minutes": planned,
                "matched_minutes": matched,
                "recorded_minutes": recorded,
                "activity_record_count": sum(int(row[3]) for row in rows),
                "extra_recorded_minutes": recorded - matched,
                "completed_sessions": sum(s["status"] == "completed" for s in sessions),
                "partial_sessions": sum(s["status"] == "partial" for s in sessions),
                "completion_percent": round(100 * matched / planned, 2) if planned else None,
                "note": (
                    "按同日同项目匹配并封顶计入计划完成；额外时长单列。"
                    "未记录不等于实际未运动，部分完成不计完整一次。"
                ),
            }
