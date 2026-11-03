"""从旧 Fit 文字工具迁入的领域输入；身份、版本和确认仍由服务器管理。"""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from fitlife.business import Input, today
from fitlife.media_types import PhotoMealInput


class Dated(Input):
    record_date: date | None = None

    @model_validator(mode="after")
    def past_only(self):
        if self.record_date and self.record_date > today():
            raise ValueError("FUTURE_RECORD_NOT_ALLOWED")
        return self


class BodyProfileInput(Input):
    height_cm: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    age: int | None = Field(default=None, ge=0, le=130, strict=True)
    sex: str | None = Field(default=None, min_length=1, max_length=40)


class HeightInput(Input):
    height_cm: float = Field(gt=0, le=300, allow_inf_nan=False)


class BodyInput(Dated):
    weight_kg: float = Field(gt=0, le=700, allow_inf_nan=False)
    body_fat_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    waist_cm: float | None = Field(default=None, gt=0, le=500, allow_inf_nan=False)
    sleep_hours: float | None = Field(default=None, ge=0, le=24, allow_inf_nan=False)
    fatigue: str | None = Field(default=None, max_length=500)


class StepsInput(Dated):
    steps: int = Field(ge=0, le=200000, strict=True)


class TrainingProfileInput(Input):
    space: str | None = Field(default=None, min_length=1, max_length=500)
    equipment: str | None = Field(default=None, min_length=1, max_length=500)
    goal: str | None = Field(default=None, min_length=1, max_length=500)
    limitations: str | None = Field(default=None, min_length=1, max_length=500)


class WorkoutItem(Input):
    category: Literal["力量", "有氧", "其他"]
    name: str = Field(min_length=1, max_length=100)
    duration_min: float | None = Field(default=None, gt=0, le=1440, allow_inf_nan=False)
    sets: int | None = Field(default=None, gt=0, le=100, strict=True)
    reps: str | None = Field(default=None, max_length=100)
    resistance: str | None = Field(default=None, max_length=100)
    feedback: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def strength_only(self):
        if self.category != "力量" and any(
            v is not None for v in (self.sets, self.reps, self.resistance)
        ):
            raise ValueError("STRENGTH_FIELDS_ONLY")
        return self


class WorkoutInput(Dated):
    items: list[WorkoutItem] = Field(min_length=1, max_length=30)
    feedback: str | None = Field(default=None, max_length=1000)


class WorkoutFeedbackInput(Input):
    workout_id: UUID
    item_index: int | None = Field(default=None, ge=0, le=29, strict=True)
    feedback: str = Field(min_length=1, max_length=1000)


class FoodInput(Input):
    name: str = Field(min_length=1, max_length=100)
    unit: Literal["g", "ml"]
    energy_kcal: float = Field(ge=0, le=2000, allow_inf_nan=False)
    protein_g: float = Field(ge=0, le=100, allow_inf_nan=False)
    carbs_g: float = Field(ge=0, le=100, allow_inf_nan=False)
    fat_g: float = Field(ge=0, le=100, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=500)


class MealItem(Input):
    food_id: UUID
    amount: float = Field(gt=0, le=20000, allow_inf_nan=False)


class MealRecordInput(Input):
    meal_date: date | None = None
    meal_type: Literal["早餐", "午餐", "晚餐", "加餐"]
    items: list[MealItem] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def past_only(self):
        if self.meal_date and self.meal_date > today():
            raise ValueError("FUTURE_RECORD_NOT_ALLOWED")
        return self


SCHEMAS = {
    "photo_meal": PhotoMealInput,
    "body_profile": BodyProfileInput,
    "body": BodyInput,
    "steps": StepsInput,
    "training_profile": TrainingProfileInput,
    "workout": WorkoutInput,
    "food": FoodInput,
    "meal": MealRecordInput,
}
DOMAINS = {
    "body_profile": "body",
    "body": "body",
    "steps": "exercise",
    "training_profile": "exercise",
    "workout": "exercise",
    "food": "diet",
    "meal": "diet",
}


class DomainPayload(Input):
    source_media: dict | None = None
    record_type: Literal[
        "photo_meal", "body_profile", "body", "steps", "training_profile", "workout", "food", "meal"
    ]
    record_key: str = Field(min_length=1, max_length=100)
    values: dict
    expected_version: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_values(self):
        # Internal meal calculations are server snapshots, never accepted as arbitrary model fields.
        raw = dict(self.values)
        calculation = raw.pop("calculation", None) if self.record_type == "meal" else None
        checked = (
            SCHEMAS[self.record_type].model_validate(raw).model_dump(mode="json", exclude_none=True)
        )
        if not checked:
            raise ValueError("EMPTY_UPDATE")
        if calculation is not None:
            if not isinstance(calculation, dict) or set(calculation) != {
                "items",
                "total",
                "food_versions",
            }:
                raise ValueError("INVALID_CALCULATION")
            checked["calculation"] = calculation
        object.__setattr__(self, "values", checked)
        return self


class BundleOperation(Input):
    kind: Literal[
        "profile_patch",
        "record_activity",
        "save_plan",
        "adjust_plan",
        "profile_forget",
        "delete_activity",
        "domain_record",
        "delete_domain_record",
    ]
    payload: dict
    base_profile_version: int = Field(ge=0, strict=True)
    base_plan_version: int | None = Field(default=None, ge=0, strict=True)


class ActionBundle(Input):
    operations: list[BundleOperation] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def checked(self):
        from fitlife.business import validate_payload

        seen = set()
        for op in self.operations:
            object.__setattr__(op, "payload", validate_payload(op.kind, op.payload))
            scope = (op.kind, op.payload.get("record_type"), op.payload.get("record_key"))
            if scope in seen:
                raise ValueError("DUPLICATE_BUNDLE_SCOPE")
            seen.add(scope)
        return self
