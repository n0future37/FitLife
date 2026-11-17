import math

from fitlife.video_counter import count_series
from fitlife.video_signals import select_signal, track_subject


def test_local_excursions_keep_reps_when_depth_changes():
    samples = [
        {
            "time": i / 20,
            "angle": 110 + 4 * i / 20 + (60 - 4 * i / 20) * math.cos(math.pi * i / 20),
            "people": 1,
        }
        for i in range(201)
    ]
    result = count_series(samples, "squat", 10)
    assert result["repetitions"] == 5
    assert all(c["end_sec"] - c["start_sec"] >= 0.45 for c in result["cycles"])


def test_return_not_finished_at_video_end_is_not_a_full_rep():
    samples = [
        {
            "time": i / 20,
            "angle": 170 - 100 * i / 20 if i <= 20 else 70 + 50 * (i - 20) / 20,
            "people": 1,
        }
        for i in range(41)
    ]
    assert count_series(samples, "squat", 2)["repetitions"] == 0


def test_no_cycle_is_joined_across_a_camera_cut():
    samples = [
        {
            "time": i / 20,
            "angle": 170 if i < 12 or i >= 28 else 70,
            "people": 1,
            "shot": int(i >= 28),
        }
        for i in range(43)
    ]
    assert count_series(samples, "squat", 2.1)["repetitions"] == 0
    for sample in samples:
        sample["shot"] = 0
    assert count_series(samples, "squat", 2.1)["repetitions"] == 1


def person(box, value):
    center = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    return {
        "box": box,
        "area": (box[2] - box[0]) * (box[3] - box[1]),
        "anchors": [center] * 5,
        "body_scale": 0.3,
        "signals": {"left_angle2d": value},
    }


def test_small_background_person_does_not_erase_the_tracked_foreground():
    from fitlife.video_counter import CountConfig

    samples = [
        {
            "time": i / 20,
            "people": 2,
            "poses": [
                person([0.1, 0.1, 0.6, 0.9], 110 + 60 * math.cos(math.pi * i / 20)),
                person([0.8, 0.2, 0.9, 0.4], 170),
            ],
        }
        for i in range(121)
    ]
    tracking = track_subject(samples)
    chosen, _ = select_signal(samples, CountConfig())
    assert tracking["segments"][0]["competitor_ratio"] < 0.1
    assert not any(s["target_ambiguous"] for s in samples)
    assert count_series(samples, "squat", 6, signal_name=chosen["name"])["repetitions"] == 3


def test_two_comparable_people_still_require_review():
    samples = [
        {
            "time": i / 20,
            "people": 2,
            "poses": [person([0.05, 0.1, 0.45, 0.9], 110), person([0.55, 0.1, 0.95, 0.9], 110)],
        }
        for i in range(61)
    ]
    track_subject(samples)
    for s in samples:
        s["angle"] = s["signals"].get("left_angle2d")
    assert all(s["target_ambiguous"] for s in samples)
    assert count_series(samples, "squat", 3)["repetitions"] is None


def test_each_shot_selects_a_new_subject_and_signal():
    from fitlife.video_counter import CountConfig

    samples = []
    for i in range(121):
        shot = int(i >= 61)
        pose = person(
            [0.1, 0.1, 0.6, 0.9] if not shot else [0.4, 0.2, 0.9, 1],
            110 + 60 * math.cos(math.pi * i / 20),
        )
        if shot:
            pose["signals"] = {"right_angle3d": pose["signals"].pop("left_angle2d")}
        samples.append({"time": i / 20, "people": 1, "poses": [pose], "shot": shot})
    tracking = track_subject(samples)
    chosen, diagnostics = select_signal(samples, CountConfig())
    assert len(tracking["segments"]) == 2 and chosen["name"] == "per_shot"
    assert {d["shot"] for d in diagnostics} == {0, 1}
    assert samples[0]["selected_signal"] == "left_angle2d"
    assert samples[-1]["selected_signal"] == "right_angle3d"
