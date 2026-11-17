from fitlife.media_eval import summarize
from fitlife.video_counter import count_series


def test_abstentions_are_never_silently_graded_as_zero():
    rows = [
        {"id": "a", "prediction": 8, "truth": 10, "wall_seconds": 1},
        {"id": "a", "prediction": None, "truth": 10, "wall_seconds": 2},
        {"id": "b", "prediction": 0, "truth": 0, "wall_seconds": 3},
    ]
    metrics = summarize(rows, "video")
    assert metrics["runs"] == 3 and metrics["distinct_assets"] == 2
    assert metrics["estimated_runs"] == 2 and metrics["coverage"] == 2 / 3
    assert metrics["conditional_mae"] == 1
    assert metrics["conditional_mape"] == 0.2  # zero truth excluded from MAPE only
    assert metrics["exact_rate_all_runs"] == 1 / 3
    assert metrics["per_asset"][0]["predictions"] == [8, None]
    assert summarize([], "food")["conditional_mae"] is None


def test_camera_dependent_angle_range_and_small_noise():
    import math

    samples = [
        {"time": i / 10, "angle": 110 + 30 * math.cos(math.pi * i / 10), "people": 1}
        for i in range(101)
    ]
    assert count_series(samples, "squat", 10)["repetitions"] == 5
    for item in samples:
        item["angle"] = 120 + (item["angle"] - 110) * 0.05
    assert count_series(samples, "squat", 10)["repetitions"] == 0
