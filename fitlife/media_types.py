"""多模态结果是待核对的观察；未知值保留null，不补零或自动写业务事实。"""

from typing import Literal

from pydantic import Field, model_validator

from fitlife.business import Input


class MediaRequest(Input):
    task: Literal["nutrition_label", "meal_photo", "workout_video"]
    message: str = Field(default="", max_length=2000)


class LabelResult(Input):
    kind: Literal["nutrition_label"] = "nutrition_label"
    name: str | None = Field(default=None, max_length=100)
    basis_amount: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)
    unit: Literal["g", "ml"] | None = None
    energy_value: float | None = Field(default=None, ge=0, le=50000, allow_inf_nan=False)
    energy_unit: Literal["kJ", "kcal"] | None = None
    protein_g: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    carbs_g: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    fat_g: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    evidence: str = Field(default="", max_length=2000)
    uncertainty: str = Field(default="", max_length=2000)


class PhotoItem(Input):
    name: str = Field(min_length=1, max_length=100)
    amount_g: float | None = Field(default=None, gt=0, le=20000, allow_inf_nan=False)
    estimated_energy_kcal: float | None = Field(default=None, ge=0, le=20000, allow_inf_nan=False)


class PhotoResult(Input):
    kind: Literal["meal_photo"] = "meal_photo"
    items: list[PhotoItem] = Field(default_factory=list, max_length=30)
    evidence: str = Field(default="", max_length=2000)
    uncertainty: str = Field(default="", max_length=2000)


class VideoObservation(Input):
    name: str = Field(min_length=1, max_length=100)
    start_sec: float = Field(ge=0, allow_inf_nan=False)
    end_sec: float = Field(ge=0, allow_inf_nan=False)
    visible_reps: int | None = Field(default=None, ge=0, le=10000, strict=True)
    observation: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_sec < self.start_sec:
            raise ValueError("INVALID_EVIDENCE_RANGE")
        return self


class VideoResult(Input):
    kind: Literal["workout_video"] = "workout_video"
    observations: list[VideoObservation] = Field(default_factory=list, max_length=30)
    uncertainty: str = Field(default="", max_length=2000)


RESULTS = {"nutrition_label": LabelResult, "meal_photo": PhotoResult, "workout_video": VideoResult}


class PhotoMealInput(Input):
    record_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    meal_type: Literal["早餐", "午餐", "晚餐", "加餐"]
    items: list[PhotoItem] = Field(min_length=1, max_length=30)
    source: Literal["vision_estimate_user_confirmed"] = "vision_estimate_user_confirmed"

    @model_validator(mode="after")
    def valid_day(self):
        from datetime import date

        from fitlife.business import today

        if date.fromisoformat(self.record_date) > today():
            raise ValueError("FUTURE_RECORD_NOT_ALLOWED")
        return self
