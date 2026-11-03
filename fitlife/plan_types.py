"""计划输入契约；参数范围为产品约束，不是医学推荐标准。"""

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from fitlife.business import ActivityId, Input, Weekday


def monday(value):
    if value.weekday() != 0:
        raise ValueError("WEEK_START_MUST_BE_MONDAY")
    return value


class PlanOverrides(Input):
    temporary_constraint_exceptions: tuple[str, ...] | None = Field(default=None, max_length=10)
    activity_id: ActivityId | None = None
    available_weekdays: tuple[Weekday, ...] | None = Field(default=None, min_length=1, max_length=7)
    weekly_sessions: int | None = Field(default=None, strict=True, ge=1, le=7)
    session_minutes: int | None = Field(default=None, strict=True, ge=1, le=180)

    @model_validator(mode="after")
    def unique_days(self):
        if self.available_weekdays and len(set(self.available_weekdays)) != len(
            self.available_weekdays
        ):
            raise ValueError("DUPLICATE_WEEKDAY")
        return self


class PlanRequest(PlanOverrides):
    week_start: date
    expected_plan_version: int = Field(strict=True, ge=0)
    _monday = field_validator("week_start")(monday)


class PlanSession(Input):
    day: date
    activity_id: ActivityId
    minutes: int = Field(strict=True, ge=1, le=180)


class PlanPayload(Input):
    temporary_constraint_exceptions: tuple[str, ...] = Field(default=(), max_length=10)
    temporary_constraints_applied: tuple[str, ...] = Field(default=(), max_length=10)
    week_start: date
    sessions: tuple[PlanSession, ...] = Field(min_length=1, max_length=7)
    source_profile_version: int = Field(strict=True, ge=0)
    source_plan_version: int = Field(strict=True, ge=0)
    source: Literal["rule_generated", "user_modified"] = "rule_generated"
    rule_version: Literal["weekly-v1"] = "weekly-v1"
    explicit_overrides: tuple[
        Literal["activity_id", "available_weekdays", "weekly_sessions", "session_minutes"], ...
    ] = ()
    _monday = field_validator("week_start")(monday)

    @model_validator(mode="after")
    def correct_week(self):
        days = [item.day for item in self.sessions]
        if days != sorted(set(days)):
            raise ValueError("PLAN_DAYS_MUST_BE_UNIQUE_AND_ORDERED")
        if any(not self.week_start <= day <= self.week_start + timedelta(days=6) for day in days):
            raise ValueError("PLAN_DAY_OUTSIDE_WEEK")
        return self
