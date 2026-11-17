"""多模态结果是待核对的观察；未知值保留null，不补零或自动写业务事实。"""

from typing import Literal

from pydantic import Field, model_validator

from fitlife.business import Input


class MediaRequest(Input):
    task: Literal["nutrition_label", "meal_photo", "workout_video"]
    message: str = Field(default="", max_length=2000)
    count_exercise: (
        Literal["squat", "push_up", "biceps_curl", "sit_up", "pull_up", "bench_press"] | None
    ) = None

    @model_validator(mode="after")
    def counting_is_video_only(self):
        if self.count_exercise is not None and self.task != "workout_video":
            raise ValueError("COUNTING_REQUIRES_VIDEO")
        return self


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


class RepetitionCycle(Input):
    start_sec: float = Field(ge=0, le=181, allow_inf_nan=False)
    end_sec: float = Field(ge=0, le=181, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_sec <= self.start_sec:
            raise ValueError("INVALID_CYCLE_RANGE")
        return self


class RepetitionCount(Input):
    exercise: Literal["squat", "push_up", "biceps_curl", "sit_up", "pull_up", "bench_press"]
    exercise_name: str = Field(min_length=1, max_length=100)
    status: Literal["counted", "needs_review"]
    repetitions: int | None = Field(ge=0, le=10000, strict=True)
    observed_cycles: int = Field(ge=0, le=10000, strict=True)
    cycles: list[RepetitionCycle] = Field(max_length=10000)
    full_timeline_processed: bool
    pose_coverage: float = Field(ge=0, le=1, allow_inf_nan=False)
    longest_missing_seconds: float = Field(ge=0, le=181, allow_inf_nan=False)
    multi_person_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    sample_count: int = Field(ge=1, le=50000, strict=True)
    duration_seconds: float = Field(gt=0, le=181, allow_inf_nan=False)
    method_version: str = Field(min_length=1, max_length=100)
    config: dict
    limitations: list[str] = Field(max_length=20)
    decoded_frames: int = Field(ge=1, le=50000, strict=True)
    source_fps: float = Field(gt=0, le=240, allow_inf_nan=False)
    selected_side: Literal["left", "right", "mixed"]
    model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    runtime_versions: dict[str, str]

    @model_validator(mode="after")
    def consistent(self):
        if self.observed_cycles != len(self.cycles):
            raise ValueError("COUNT_CYCLE_MISMATCH")
        if (
            self.status == "counted"
            and (not self.full_timeline_processed or self.repetitions != self.observed_cycles)
        ) or (self.status != "counted" and self.repetitions is not None):
            raise ValueError("COUNT_STATUS_MISMATCH")
        if any(c.end_sec > self.duration_seconds + 0.01 for c in self.cycles):
            raise ValueError("CYCLE_OUTSIDE_VIDEO")
        if any(b.start_sec < a.end_sec for a, b in zip(self.cycles, self.cycles[1:])):
            raise ValueError("OVERLAPPING_REPETITION_CYCLES")
        return self


class VideoResult(Input):
    kind: Literal["workout_video"] = "workout_video"
    observations: list[VideoObservation] = Field(default_factory=list, max_length=30)
    counting: RepetitionCount | None = None
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
