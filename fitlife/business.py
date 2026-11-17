"""画像与活动业务服务；身份由受信任调用方传入，确认方法不暴露给模型。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from fitlife.database import Activity, AgentRequest, PendingAction, Plan, Profile, User

ActivityId = Literal["walking", "gentle_mobility"]
Weekday = Annotated[
    int,
    Field(
        strict=True,
        ge=0,
        le=6,
        description="星期编号：0周一、1周二、2周三、3周四、4周五、5周六、6周日；不是ISO的1到7。",
    ),
]


def utcnow():
    # MySQL DATETIME保存无时区的UTC，接口输出显式补Z。
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def today():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemporaryConstraint(Input):
    name: str = Field(min_length=1, max_length=80)
    domains: tuple[Literal["body", "diet", "exercise"], ...] = Field(min_length=1, max_length=3)
    start_date: date
    end_date: date
    note: str = Field(min_length=1, max_length=500)
    available_weekdays: tuple[Weekday, ...] | None = Field(default=None, min_length=1, max_length=7)
    max_session_minutes: int | None = Field(default=None, strict=True, ge=1, le=180)
    allowed_activities: tuple[ActivityId, ...] | None = Field(
        default=None, min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def valid_range(self):
        if (
            not 0 <= (self.end_date - self.start_date).days <= 365
            or not self.name.strip()
            or not self.note.strip()
        ):
            raise ValueError("INVALID_TEMPORARY_CONSTRAINT")
        for values in (self.domains, self.available_weekdays, self.allowed_activities):
            if values and len(values) != len(set(values)):
                raise ValueError("DUPLICATE_CONSTRAINT_VALUE")
        return self


class ProfilePatch(Input):
    temporary_constraints: tuple[TemporaryConstraint, ...] | None = Field(
        default=None, max_length=10
    )
    goal: str | None = Field(default=None, min_length=1, max_length=500)
    space: str | None = Field(default=None, min_length=1, max_length=500)
    equipment: str | None = Field(default=None, min_length=1, max_length=500)
    limitations: str | None = Field(default=None, min_length=1, max_length=500)
    height_cm: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    age: int | None = Field(default=None, ge=0, le=130, strict=True)
    sex: str | None = Field(default=None, min_length=1, max_length=40)
    diet_preferences: str | None = Field(default=None, min_length=1, max_length=500)
    preferred_activities: tuple[ActivityId, ...] | None = Field(default=None, min_length=1)
    available_weekdays: tuple[Weekday, ...] | None = Field(default=None, min_length=1)
    weekly_sessions: int | None = Field(default=None, strict=True, ge=1, le=7)
    session_minutes: int | None = Field(default=None, strict=True, ge=1, le=180)

    @model_validator(mode="after")
    def explicit_values(self):
        if not self.model_fields_set:
            raise ValueError("EMPTY_PROFILE_PATCH")
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is None:
                raise ValueError("NULL_PROFILE_VALUE_NOT_SUPPORTED")
            if isinstance(value, str) and not value.strip():
                raise ValueError("BLANK_PROFILE_VALUE")
            if isinstance(value, tuple) and len(value) != len(set(value)):
                raise ValueError("DUPLICATE_PROFILE_VALUE")
            if name == "temporary_constraints" and len({c.name for c in value}) != len(value):
                raise ValueError("DUPLICATE_CONSTRAINT_NAME")
        return self


class ActivityInput(Input):
    activity_id: ActivityId
    occurred_on: date = Field(default_factory=today)
    minutes: int = Field(strict=True, ge=1, le=1440)

    @model_validator(mode="after")
    def not_in_future(self):
        if self.occurred_on > today():
            raise ValueError("ACTIVITY_DATE_IN_FUTURE")
        return self


ProfileField = Literal[
    "goal",
    "preferred_activities",
    "available_weekdays",
    "weekly_sessions",
    "session_minutes",
    "space",
    "equipment",
    "limitations",
    "height_cm",
    "age",
    "sex",
    "diet_preferences",
    "temporary_constraints",
]


class ForgetProfile(Input):
    fields: tuple[ProfileField, ...] = Field(min_length=1, max_length=13)

    @model_validator(mode="after")
    def unique(self):
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("DUPLICATE_PROFILE_FIELD")
        return self


class DeleteActivity(Input):
    record_id: UUID
    activity_id: ActivityId
    occurred_on: date
    minutes: int = Field(strict=True, ge=1, le=1440)


@dataclass(frozen=True)
class Actor:
    """仅由服务端认证层/内部验收创建，不接收模型指定的用户身份。"""

    user_id: UUID


class BusinessError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fingerprint(action):
    content = {
        "owner": action.owner_id,
        "kind": action.kind,
        "payload": action.payload,
        "revision": action.revision,
        "base_version": action.base_profile_version,
        "expires_at": action.expires_at.isoformat(),
    }
    if action.kind in {"save_plan", "adjust_plan"}:
        content["base_plan_version"] = action.base_plan_version
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(action):
    return {
        "action_id": action.id,
        "kind": action.kind,
        "payload": action.payload,
        "revision": action.revision,
        "digest": action.digest,
        "status": action.status,
        "expires_at": action.expires_at.isoformat() + "Z",
        "result": action.result,
        **(
            {"base_plan_version": action.base_plan_version}
            if action.kind in {"save_plan", "adjust_plan"}
            else {}
        ),
    }


def validate_payload(kind, payload):
    from fitlife.adjustments import AdjustmentPayload
    from fitlife.domain_types import ActionBundle, DomainPayload
    from fitlife.plan_types import PlanPayload
    from fitlife.record_management import DeleteRecord

    schema = {
        "domain_record": DomainPayload,
        "delete_domain_record": DeleteRecord,
        "action_bundle": ActionBundle,
        "profile_patch": ProfilePatch,
        "record_activity": ActivityInput,
        "save_plan": PlanPayload,
        "adjust_plan": AdjustmentPayload,
        "profile_forget": ForgetProfile,
        "delete_activity": DeleteActivity,
    }.get(kind)
    if schema is None:
        raise BusinessError("UNKNOWN_OPERATION")
    request = schema.model_validate(payload)
    return request.model_dump(mode="json", exclude_unset=kind == "profile_patch")


class BusinessService:
    collect_candidates = False

    def __init__(self, engine, *, request_fence=None):
        if engine.dialect.name != "mysql":
            raise ValueError("MYSQL_REQUIRED")
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.request_fence = request_fence

    def _user(self, session, actor, lock=False):
        user = session.get(User, str(actor.user_id), with_for_update=lock)
        if user is None:
            raise BusinessError("USER_NOT_FOUND")
        return user.id

    def _action(self, session, owner, action_id):
        action = session.scalar(
            select(PendingAction)
            .where(
                PendingAction.id == str(action_id),
                PendingAction.owner_id == owner,
            )
            .with_for_update()
        )
        if action is None:
            raise BusinessError("ACTION_NOT_FOUND")
        return action

    def _confirmation(self, action, revision, digest):
        if action.revision != revision or action.digest != digest:
            raise BusinessError("STALE_CONFIRMATION")

    def get_profile(self, actor):
        with self.sessions() as session:
            owner = self._user(session, actor)
            profile = session.get(Profile, owner)
            from fitlife.constraints import applicable

            return {
                "active_temporary_constraints": applicable(
                    profile.values if profile else {}, today(), today()
                ),
                "version": profile.version if profile else 0,
                "values": profile.values if profile else {},
                "provenance": profile.provenance if profile else {},
            }

    def propose(
        self,
        actor,
        kind,
        payload,
        request_key,
        *,
        expected_plan_version=None,
        expected_profile_version=None,
        request_fingerprint=None,
    ):
        """request_key由请求入口生成并跨重试复用；相同key不同内容报冲突。"""
        payload = validate_payload(kind, payload)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise BusinessError("INVALID_REQUEST_KEY")
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            if self.request_fence:
                key, nonce = self.request_fence
                running = session.scalar(
                    select(AgentRequest).where(
                        AgentRequest.owner_id == owner,
                        AgentRequest.request_key == key,
                    )
                )
                if (
                    running is None
                    or running.nonce != nonce
                    or running.status != "running"
                    or running.lease_until <= utcnow()
                ):
                    raise BusinessError("REQUEST_SUPERSEDED")
            if request_key.startswith("agent:"):
                other = session.scalar(
                    select(PendingAction).where(
                        PendingAction.owner_id == owner,
                        PendingAction.request_key == request_key,
                        PendingAction.kind != kind,
                    )
                )
                if other:
                    raise BusinessError("REQUEST_OPERATION_CONFLICT")
            previous = session.scalar(
                select(PendingAction).where(
                    PendingAction.owner_id == owner,
                    PendingAction.request_key == request_key,
                    PendingAction.kind == kind,
                )
            )
            if previous:
                if (
                    previous.request_fingerprint != request_fingerprint
                    if request_fingerprint
                    else previous.payload != payload
                ):
                    raise BusinessError("IDEMPOTENCY_CONFLICT")
                return snapshot(previous)
            profile = session.get(Profile, owner)
            if (
                expected_profile_version is not None
                and (profile.version if profile else 0) != expected_profile_version
            ):
                raise BusinessError("PROFILE_VERSION_CONFLICT")
            plan_version = None
            if kind in {"save_plan", "adjust_plan"}:
                plan = session.scalar(
                    select(Plan).where(
                        Plan.owner_id == owner,
                        Plan.week_start == date.fromisoformat(payload["week_start"]),
                    )
                )
                plan_version = plan.version if plan else 0
                if expected_plan_version is None:
                    raise BusinessError("PLAN_READ_REQUIRED")
                if (
                    plan_version != expected_plan_version
                    or payload["source_plan_version"] != plan_version
                ):
                    raise BusinessError("PLAN_VERSION_CONFLICT")
                if payload["source_profile_version"] != (profile.version if profile else 0):
                    raise BusinessError("PROFILE_VERSION_CONFLICT")
                from fitlife.constraints import plan_rules

                violations, applied = plan_rules(
                    profile.values if profile else {},
                    payload["sessions"],
                    payload.get("temporary_constraint_exceptions", []),
                )
                if violations:
                    raise BusinessError("TEMPORARY_CONSTRAINT_CONFLICT")
                payload["temporary_constraints_applied"] = applied
                if kind == "adjust_plan":
                    from fitlife.adjustments import valid

                    if not valid(session, owner, payload):
                        raise BusinessError("ADJUSTMENT_CONTEXT_STALE")

            if kind == "action_bundle":
                from fitlife.domains import validate_bundle_constraints

                if not validate_bundle_constraints(session, owner, payload):
                    raise BusinessError("BUNDLE_CONSTRAINT_CONFLICT")
            if self.collect_candidates:
                return {
                    "proposal": {
                        "kind": kind,
                        "payload": payload,
                        "base_profile_version": profile.version if profile else 0,
                        "base_plan_version": plan_version,
                    },
                    "status": "proposed",
                }
            action = PendingAction(
                id=str(uuid4()),
                owner_id=owner,
                request_key=request_key,
                kind=kind,
                payload=payload,
                revision=1,
                base_profile_version=profile.version if profile else 0,
                base_plan_version=plan_version,
                request_fingerprint=request_fingerprint,
                status="pending",
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(minutes=15),
            )
            action.digest = fingerprint(action)
            session.add(action)
            session.flush()
            return snapshot(action)

    def get_action(self, actor, action_id):
        with self.sessions() as session:
            owner = self._user(session, actor)
            return snapshot(self._action(session, owner, action_id))

    def revise(self, actor, action_id, revision, digest, payload):
        """修改候选递增版本，旧确认失效；request_key保持原请求身份。"""
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            action = self._action(session, owner, action_id)
            self._confirmation(action, revision, digest)
            if action.status != "pending":
                raise BusinessError("ACTION_NOT_PENDING")
            if action.expires_at <= utcnow():
                action.status = "expired"
                return snapshot(action)
            new_payload = validate_payload(action.kind, payload)
            if action.kind in {
                "domain_record",
                "action_bundle",
                "delete_domain_record",
                "adjust_plan",
            }:
                raise BusinessError("OPERATION_SCOPE_CHANGE")
            if action.kind == "delete_activity" and new_payload != action.payload:
                raise BusinessError("OPERATION_SCOPE_CHANGE")
            profile = session.get(Profile, owner)
            if action.kind == "save_plan":
                if new_payload["week_start"] != action.payload["week_start"]:
                    raise BusinessError("OPERATION_SCOPE_CHANGE")
                plan = session.scalar(
                    select(Plan).where(
                        Plan.owner_id == owner,
                        Plan.week_start == date.fromisoformat(new_payload["week_start"]),
                    )
                )
                if (plan.version if plan else 0) != action.base_plan_version:
                    action.status = "superseded"
                    return snapshot(action)
                new_payload["source_profile_version"] = profile.version if profile else 0
                new_payload["source_plan_version"] = action.base_plan_version
                new_payload["source"] = "user_modified"
                from fitlife.constraints import plan_rules

                violations, applied = plan_rules(
                    profile.values if profile else {},
                    new_payload["sessions"],
                    new_payload.get("temporary_constraint_exceptions", []),
                )
                if violations:
                    raise BusinessError("TEMPORARY_CONSTRAINT_CONFLICT")
                new_payload["temporary_constraints_applied"] = applied

            action.payload = new_payload
            action.base_profile_version = profile.version if profile else 0
            action.revision += 1
            action.digest = fingerprint(action)
            return snapshot(action)

    def reject(self, actor, action_id, revision, digest):
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            action = self._action(session, owner, action_id)
            self._confirmation(action, revision, digest)
            if action.status == "rejected":
                return snapshot(action)
            if action.status != "pending":
                raise BusinessError("ACTION_NOT_PENDING")
            action.status = "rejected"
            return snapshot(action)

    def confirm(self, actor, action_id, revision, digest):
        """所有本用户写入先锁用户行；候选状态和业务写入在同一事务提交。"""
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            action = self._action(session, owner, action_id)
            self._confirmation(action, revision, digest)
            if action.status == "applied":
                return snapshot(action)
            if action.status != "pending":
                raise BusinessError("ACTION_NOT_PENDING")
            if action.expires_at <= utcnow():
                action.status = "expired"
                return snapshot(action)
            # 校验持久化内容未发生变化，摘要不作为身份认证凭据。
            if action.digest != fingerprint(action):
                raise BusinessError("ACTION_INTEGRITY_ERROR")
            payload = validate_payload(action.kind, action.payload)
            self._apply_operation(session, owner, action, payload)
            if action.status == "superseded":
                return snapshot(action)
            action.status = "applied"
            session.flush()
            return snapshot(action)

    def _apply_operation(self, session, owner, action, payload):
        if action.kind in {"profile_patch", "profile_forget"}:
            profile = session.get(Profile, owner)
            current_version = profile.version if profile else 0
            if current_version != action.base_profile_version:
                action.status = "superseded"
                return
            deleting = action.kind == "profile_forget"
            changed_fields = payload["fields"] if deleting else list(payload)
            merged = dict(profile.values if profile else {})
            if deleting:
                for name in changed_fields:
                    merged.pop(name, None)
            else:
                merged.update(payload)
            if (
                merged.get("weekly_sessions")
                and merged.get("available_weekdays")
                and merged["weekly_sessions"] > len(merged["available_weekdays"])
            ):
                raise BusinessError("PROFILE_SCHEDULE_CONFLICT")
            provenance = dict(profile.provenance if profile else {})
            for name in changed_fields:
                provenance[name] = {
                    "source": "user_deleted" if deleting else "user_explicit",
                    "action_id": action.id,
                    "updated_at": utcnow().isoformat() + "Z",
                }
            if profile is None:
                profile = Profile(owner_id=owner)
                session.add(profile)
            profile.values, profile.provenance = merged, provenance
            profile.version, profile.updated_at = current_version + 1, utcnow()
            action.result = {"profile_version": profile.version}
        elif action.kind in {"save_plan", "adjust_plan"}:
            from fitlife.planning import save_plan

            result = save_plan(session, owner, action, payload)
            if result is None:
                action.status = "superseded"
                return
            action.result = result
        elif action.kind == "record_activity":
            activity = Activity(
                id=str(uuid4()),
                owner_id=owner,
                source_action_id=action.id,
                activity_id=payload["activity_id"],
                occurred_on=date.fromisoformat(payload["occurred_on"]),
                minutes=payload["minutes"],
                created_at=utcnow(),
            )
            session.add(activity)
            action.result = {"activity_id": activity.id}
        elif action.kind == "delete_activity":
            activity = session.scalar(
                select(Activity).where(
                    Activity.id == payload["record_id"],
                    Activity.owner_id == owner,
                )
            )
            if activity is None:
                action.status = "superseded"
                return
            actual = {
                "activity_id": activity.activity_id,
                "occurred_on": activity.occurred_on.isoformat(),
                "minutes": activity.minutes,
            }
            if any(payload[key] != value for key, value in actual.items()):
                action.status = "superseded"
                return
            session.delete(activity)
            action.result = {"deleted_record_id": activity.id}
        elif action.kind == "delete_domain_record":
            from fitlife.record_management import apply

            result = apply(session, owner, payload)
            if result is None:
                action.status = "superseded"
                return
            action.result = result
        elif action.kind == "domain_record":
            from fitlife.domains import apply_domain

            result = apply_domain(session, owner, action, payload)
            if result is None:
                action.status = "superseded"
                return
            action.result = result
        elif action.kind == "action_bundle":
            from fitlife.domains import apply_bundle

            result = apply_bundle(self, session, owner, action, payload)
            if result is None:
                action.status = "superseded"
                return
            action.result = result
        else:
            raise BusinessError("UNKNOWN_OPERATION")

    def propose_activity_deletion(self, actor, record_id, request_key):
        with self.sessions() as session:
            owner = self._user(session, actor)
            previous = session.scalar(
                select(PendingAction).where(
                    PendingAction.owner_id == owner,
                    PendingAction.request_key == request_key,
                    PendingAction.kind == "delete_activity",
                )
            )
            if previous:
                if previous.payload["record_id"] != str(record_id):
                    raise BusinessError("IDEMPOTENCY_CONFLICT")
                return snapshot(previous)
            row = session.scalar(
                select(Activity).where(
                    Activity.id == str(record_id),
                    Activity.owner_id == owner,
                )
            )
            if row is None:
                raise BusinessError("ACTIVITY_NOT_FOUND")
            payload = {
                "record_id": row.id,
                "activity_id": row.activity_id,
                "occurred_on": row.occurred_on.isoformat(),
                "minutes": row.minutes,
            }
        return self.propose(actor, "delete_activity", payload, request_key)

    def list_activities(self, actor, start_date, end_date, limit=100, offset=0):
        start_date, end_date = (
            date.fromisoformat(str(start_date)),
            date.fromisoformat(str(end_date)),
        )
        if not 0 <= (end_date - start_date).days <= 365:
            raise BusinessError("INVALID_DATE_RANGE")
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100000
        ):
            raise BusinessError("INVALID_PAGINATION")
        with self.sessions.begin() as session:
            owner = self._user(session, actor, lock=True)
            count, minutes = session.execute(
                select(func.count(Activity.id), func.coalesce(func.sum(Activity.minutes), 0)).where(
                    Activity.owner_id == owner, Activity.occurred_on.between(start_date, end_date)
                )
            ).one()
            rows = session.scalars(
                select(Activity)
                .where(
                    Activity.owner_id == owner,
                    Activity.occurred_on.between(start_date, end_date),
                )
                .order_by(Activity.occurred_on, Activity.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return {
                "records": [
                    {
                        "id": row.id,
                        "activity_id": row.activity_id,
                        "occurred_on": row.occurred_on.isoformat(),
                        "minutes": row.minutes,
                    }
                    for row in rows
                ],
                "record_count": int(count),
                "recorded_minutes": int(minutes),
                "returned_count": len(rows),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(rows) < count,
            }
