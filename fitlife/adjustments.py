"""依据真实记录调整未来安排；事实快照与差异由服务端生成并在确认时重验。"""

import hashlib
import json
from datetime import date, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select

from fitlife.business import ActivityId, BusinessError, Input, Weekday, today
from fitlife.database import Activity, PendingAction, Profile
from fitlife.plan_types import PlanPayload, PlanSession, monday
from fitlife.planning import current_plan, week_date


class AdjustmentOptions(Input):
    reference_week_start: date | None = None
    available_weekdays: tuple[Weekday, ...] | None = Field(default=None, max_length=7)
    remaining_sessions: int | None = Field(default=None, strict=True, ge=0, le=7)
    session_minutes: int | None = Field(default=None, strict=True, ge=1, le=180)
    activity_id: ActivityId | None = None

    @model_validator(mode="after")
    def valid_options(self):
        if self.reference_week_start:
            monday(self.reference_week_start)
        if self.available_weekdays and len(set(self.available_weekdays)) != len(
            self.available_weekdays
        ):
            raise ValueError("DUPLICATE_WEEKDAY")
        return self


class AdjustmentRequest(AdjustmentOptions):
    week_start: date
    expected_plan_version: int = Field(strict=True, ge=0)
    expected_evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    _monday = field_validator("week_start")(monday)


class PlanChange(Input):
    day: date
    before: PlanSession | None
    after: PlanSession | None


class AdjustmentEvidence(Input):
    as_of: date
    reference_week_start: date
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    activity_record_ids: list[str] = Field(max_length=5000)
    reference_summary: dict
    before_sessions: list[PlanSession] = Field(max_length=7)
    locked_sessions: list[PlanSession] = Field(max_length=7)
    changes: list[PlanChange] = Field(min_length=1, max_length=14)
    explicit_requirements: dict
    explanation: str = Field(max_length=500)


class AdjustmentPayload(PlanPayload):
    sessions: tuple[PlanSession, ...] = Field(max_length=7)
    source: Literal["evidence_adjusted"] = "evidence_adjusted"
    rule_version: Literal["adjustment-v1"] = "adjustment-v1"
    adjustment: AdjustmentEvidence


def capture(session, owner, week_start, reference_week_start, as_of):
    profile = session.get(Profile, owner)
    weeks = sorted({week_start, reference_week_start})
    ranges = [(week, week + timedelta(days=6)) for week in weeks]
    from sqlalchemy import or_

    rows = session.scalars(
        select(Activity)
        .where(
            Activity.owner_id == owner,
            or_(*(Activity.occurred_on.between(a, b) for a, b in ranges)),
        )
        .order_by(Activity.occurred_on, Activity.id)
        .limit(5001)
    ).all()
    if len(rows) > 5000:
        raise BusinessError("ADJUSTMENT_EVIDENCE_TOO_LARGE")
    records = [
        dict(id=r.id, day=str(r.occurred_on), activity_id=r.activity_id, minutes=r.minutes)
        for r in rows
    ]
    plans = {str(w): current_plan(session, owner, w) for w in weeks}
    facts = dict(
        as_of=str(as_of),
        week_start=str(week_start),
        reference_week_start=str(reference_week_start),
        profile_version=profile.version if profile else 0,
        profile=profile.values if profile else {},
        plans=plans,
        activity_records=records,
    )
    digest = hashlib.sha256(
        json.dumps(facts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    reference = plans[str(reference_week_start)]["plan"]
    targets = reference["sessions"] if reference else []
    actual = {}
    for row in records:
        key = (row["day"], row["activity_id"])
        actual[key] = actual.get(key, 0) + row["minutes"]
    matched = sum(min(s["minutes"], actual.get((s["day"], s["activity_id"]), 0)) for s in targets)
    facts.update(
        evidence_digest=digest,
        reference_summary=dict(
            planned_sessions=len(targets),
            matched_minutes=matched,
            completed_sessions=sum(
                actual.get((s["day"], s["activity_id"]), 0) >= s["minutes"] for s in targets
            ),
            note="未记录不代表实际未运动；安排仅按明确时间与偏好调整，不自动推断健康负荷。",
        ),
    )
    return facts


def context(service, actor, week_start, reference_week_start=None, as_of=None):
    week_start = week_date(week_start)
    reference_week_start = week_date(reference_week_start or week_start)
    with service.sessions.begin() as session:
        owner = service._user(session, actor, lock=True)
        return capture(session, owner, week_start, reference_week_start, as_of or today())


def difference(before, after):
    old, new = ({s["day"]: s for s in items} for items in (before, after))
    return [
        dict(day=day, before=old.get(day), after=new.get(day))
        for day in sorted(old.keys() | new.keys())
        if old.get(day) != new.get(day)
    ]


def propose(service, actor, request, key, as_of=None):
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with service.sessions() as session:
        owner = service._user(session, actor)
        previous = session.scalar(
            select(PendingAction).where(
                PendingAction.owner_id == owner,
                PendingAction.request_key == key,
                PendingAction.kind == "adjust_plan",
            )
        )
        if previous:
            from fitlife.business import snapshot

            if previous.request_fingerprint != fingerprint:
                raise BusinessError("IDEMPOTENCY_CONFLICT")
            return snapshot(previous)
    facts = context(service, actor, request.week_start, request.reference_week_start, as_of)
    plan = facts["plans"][str(request.week_start)]
    if plan["version"] != request.expected_plan_version:
        raise BusinessError("PLAN_VERSION_CONFLICT")
    if facts["evidence_digest"] != request.expected_evidence_digest:
        raise BusinessError("ADJUSTMENT_CONTEXT_STALE")
    old = plan["plan"]["sessions"] if plan["plan"] else []
    recorded_days = {r["day"] for r in facts["activity_records"]}
    locked = [s for s in old if s["day"] <= facts["as_of"] or s["day"] in recorded_days]
    future = [s for s in old if s not in locked]
    values = facts["profile"]
    days = request.available_weekdays
    if days is None:
        days = tuple(sorted({date.fromisoformat(s["day"]).weekday() for s in future})) or tuple(
            values.get("available_weekdays", [])
        )
    count = request.remaining_sessions
    if count is None:
        count = len(future) if old else values.get("weekly_sessions")
    minutes = request.session_minutes or (
        future[0]["minutes"] if future else values.get("session_minutes")
    )
    activity = request.activity_id or (
        future[0]["activity_id"]
        if future
        else next(iter(values.get("preferred_activities", [])), None)
    )
    missing = [
        k
        for k, v in {
            "remaining_sessions": count,
            "session_minutes": minutes,
            "activity_id": activity,
        }.items()
        if v is None
    ]
    if count != 0 and not days:
        missing.append("available_weekdays")
    if missing and count != 0:
        return dict(status="needs_information", missing_fields=missing)
    eligible = [
        request.week_start + timedelta(days=d)
        for d in sorted(days or ())
        if str(request.week_start + timedelta(days=d)) > facts["as_of"]
        and str(request.week_start + timedelta(days=d)) not in recorded_days
    ]
    if count is None or count > len(eligible):
        return dict(
            status="schedule_conflict",
            requested_remaining_sessions=count,
            available_future_days=len(eligible),
        )
    preferred = {s["day"] for s in future}
    selected = sorted(sorted(eligible, key=lambda d: (str(d) not in preferred, d))[:count])
    by_day = {s["day"]: s for s in future}
    sessions = locked + [
        dict(
            day=str(d),
            activity_id=request.activity_id or by_day.get(str(d), {}).get("activity_id", activity),
            minutes=request.session_minutes or by_day.get(str(d), {}).get("minutes", minutes),
        )
        for d in selected
    ]
    sessions.sort(key=lambda s: s["day"])
    changes = difference(old, sessions)
    if not changes:
        return dict(status="unchanged", **plan)
    adjustment = dict(
        as_of=facts["as_of"],
        reference_week_start=facts["reference_week_start"],
        evidence_digest=facts["evidence_digest"],
        activity_record_ids=[r["id"] for r in facts["activity_records"]],
        reference_summary=facts["reference_summary"],
        before_sessions=old,
        locked_sessions=locked,
        changes=changes,
        explicit_requirements=request.model_dump(mode="json", exclude_none=True),
        explanation="依据本人原计划、已记录活动、确认画像与本轮明确安排调整；保留过去和已有记录的安排，未记录不等于未完成。",
    )
    payload = AdjustmentPayload(
        week_start=request.week_start,
        sessions=sessions,
        source_profile_version=facts["profile_version"],
        source_plan_version=plan["version"],
        adjustment=adjustment,
    ).model_dump(mode="json")
    return service.propose(
        actor,
        "adjust_plan",
        payload,
        key,
        expected_plan_version=plan["version"],
        expected_profile_version=facts["profile_version"],
        request_fingerprint=fingerprint,
    )


def valid(session, owner, payload):
    evidence = payload["adjustment"]
    if evidence.get("as_of") != str(today()):
        return False
    facts = capture(
        session,
        owner,
        date.fromisoformat(payload["week_start"]),
        date.fromisoformat(evidence["reference_week_start"]),
        today(),
    )
    if facts["evidence_digest"] != evidence.get("evidence_digest"):
        return False
    before = facts["plans"][payload["week_start"]]["plan"]
    before = before["sessions"] if before else []
    days = {r["day"] for r in facts["activity_records"]}
    locked = [s for s in before if s["day"] <= str(today()) or s["day"] in days]
    after_by_day = {s["day"]: s for s in payload["sessions"]}
    return (
        evidence.get("before_sessions") == before
        and evidence.get("reference_summary") == facts["reference_summary"]
        and evidence.get("activity_record_ids") == [r["id"] for r in facts["activity_records"]]
        and evidence.get("locked_sessions") == locked
        and all(after_by_day.get(s["day"]) == s for s in locked)
        and all(
            s in before for s in payload["sessions"] if s["day"] <= str(today()) or s["day"] in days
        )
        and evidence.get("changes") == difference(before, payload["sessions"])
    )
