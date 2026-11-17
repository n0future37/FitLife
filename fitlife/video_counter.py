"""Whole-timeline, single-person repetition counter; no diagnosis or posture grading."""

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from fitlife.video_signals import (
    analyze_signal,
    assign_shots,
    pose_data,
    select_signal,
    track_subject,
)

VERSION = "pose-cycle-v3.1"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/1/pose_landmarker_full.task"
)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "data/pose-models/pose_landmarker_full.task"
EXERCISES = {
    "squat": {"name": "深蹲", "joints": (23, 25, 27), "low": 105, "high": 155},
    "push_up": {"name": "俯卧撑", "joints": (11, 13, 15), "low": 95, "high": 155},
    "biceps_curl": {"name": "肱二头肌弯举", "joints": (11, 13, 15), "low": 65, "high": 150},
    "sit_up": {"name": "仰卧起坐", "joints": (11, 23, 25), "low": 85, "high": 145},
    "pull_up": {"name": "引体向上", "joints": (11, 13, 15), "low": 80, "high": 150},
    "bench_press": {"name": "卧推", "joints": (11, 13, 15), "low": 95, "high": 155},
}


@dataclass(frozen=True)
class CountConfig:
    minimum_prominence_degrees: float = 12
    interpolation_gap_seconds: float = 0.25
    inference_fps: float = 15
    visibility: float = 0.35
    min_cycle_seconds: float = 0.45
    max_cycle_seconds: float = 20
    reset_gap_seconds: float = 0.6
    min_coverage: float = 0.7
    max_missing_seconds: float = 2
    max_seconds: float = 180
    max_runtime_seconds: float = 210
    max_width: int = 640


def angle(a, b, c):
    u = [a[0] - b[0], a[1] - b[1]]
    v = [c[0] - b[0], c[1] - b[1]]
    denominator = math.hypot(*u) * math.hypot(*v)
    if denominator <= 1e-8:
        return None
    cosine = max(-1, min(1, sum(x * y for x, y in zip(u, v)) / denominator))
    return math.degrees(math.acos(cosine))


def count_series(
    samples, exercise, duration, config=None, *, signal_name="angle2d", body_scale=0.3
):
    """Count relative out-and-back excursions; missing observations never become zero."""
    config = config or CountConfig()
    spec = EXERCISES[exercise]
    if not samples or duration <= 0:
        raise ValueError("VIDEO_COUNTER_EMPTY_TIMELINE")
    if any(b["time"] <= a["time"] for a, b in zip(samples, samples[1:])):
        raise ValueError("VIDEO_COUNTER_NON_MONOTONIC_TIME")
    valid = [s for s in samples if s.get("angle") is not None]
    from itertools import groupby

    analyses = [
        analyze_signal(list(group), name[1], config, body_scale)
        for name, group in groupby(
            samples, key=lambda p: (p.get("shot", 0), p.get("selected_signal", signal_name))
        )
    ]
    cycles = [cycle for analysis in analyses for cycle in analysis["cycles"]]
    analysis = analyses[0]

    times = [0, *[s["time"] for s in valid], duration]
    missing_longest = max(b - a for a, b in zip(times, times[1:]))
    coverage = len(valid) / len(samples)
    multi_ratio = sum(s.get("people", 0) > 1 for s in samples) / len(samples)
    ambiguous = sum(s.get("target_ambiguous", s.get("people", 0) > 1) for s in samples) / len(
        samples
    )
    complete = samples[0]["time"] <= 0.25 and samples[-1]["time"] >= duration - 0.3
    reasons = []
    if not complete:
        reasons.append("未处理完整时间轴")
    if coverage < config.min_coverage:
        reasons.append("可用运动信号覆盖率不足")
    if missing_longest > config.max_missing_seconds:
        reasons.append("存在较长遮挡或缺失片段")
    if ambiguous > 0.05:
        reasons.append("多名主体相近，无法可靠确定同一训练者")
    if not valid:
        reasons.append("未检测到可用人体姿态")
    return {
        "exercise": exercise,
        "exercise_name": spec["name"],
        "status": "counted" if not reasons else "needs_review",
        "repetitions": len(cycles) if not reasons else None,
        "observed_cycles": len(cycles),
        "cycles": cycles,
        "full_timeline_processed": complete,
        "pose_coverage": round(coverage, 5),
        "longest_missing_seconds": round(missing_longest, 3),
        "multi_person_fraction": round(multi_ratio, 5),
        "sample_count": len(samples),
        "duration_seconds": round(duration, 4),
        "method_version": VERSION,
        "config": {
            **asdict(config),
            "selected_signal": signal_name,
            "effective_prominence": analysis["prominence"],
            "ambiguous_fraction": round(ambiguous, 5),
        },
        "limitations": reasons
        or ["自动计数是估计；统计可见往返运动，不评价动作幅度是否达标，需本人核对。"],
    }


def count_video(path, exercise, *, model_path=None, config=None, progress=None):
    """Decode through EOF, run pose tracking across the full timeline, retain feature evidence."""
    if exercise not in EXERCISES:
        raise ValueError("VIDEO_EXERCISE_UNSUPPORTED")
    config = config or CountConfig()
    if not 1 <= config.inference_fps <= 30:
        raise ValueError("VIDEO_COUNTER_INVALID_CONFIG")
    model_path = Path(model_path or os.getenv("FITLIFE_POSE_MODEL_FILE", DEFAULT_MODEL))
    if not model_path.is_file():
        raise ValueError("POSE_MODEL_NOT_INSTALLED")
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
    except ImportError as exc:
        raise ValueError("POSE_RUNTIME_NOT_INSTALLED") from exc
    started = time.monotonic()
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file,pipe"
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    if not capture.isOpened():
        raise ValueError("VIDEO_DECODE_FAILED")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    expected_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if not math.isfinite(fps) or not 1 <= fps <= 240 or expected_frames <= 0:
        capture.release()
        raise ValueError("VIDEO_TIMELINE_UNAVAILABLE")
    expected_duration = expected_frames / fps
    if expected_duration > config.max_seconds + 0.5:
        capture.release()
        raise ValueError("VIDEO_DURATION_EXCEEDED")
    options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=2,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    samples = []
    decoded = 0
    last_timestamp = -1
    next_sample = 0
    interval = 1 / min(fps, config.inference_fps)
    actual_end = 0
    try:
        with vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                decoded += 1
                # Container timestamps preserve VFR when available; monotonic fallback uses FPS.
                container_time = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000
                timestamp = (
                    container_time
                    if container_time >= 0 and math.isfinite(container_time)
                    else (decoded - 1) / fps
                )
                if timestamp * 1000 <= last_timestamp:
                    timestamp = (decoded - 1) / fps
                actual_end = max(actual_end, timestamp + 1 / fps)
                if timestamp > config.max_seconds + 0.1:
                    raise ValueError("VIDEO_DURATION_EXCEEDED")
                if time.monotonic() - started > config.max_runtime_seconds:
                    raise TimeoutError("VIDEO_COUNTER_TIMEOUT")
                if timestamp + 1e-6 < next_sample and decoded < expected_frames:
                    continue
                next_sample = timestamp + interval
                scale = min(1, config.max_width / max(frame.shape[:2]))
                if scale < 1:
                    frame = cv2.resize(frame, None, fx=scale, fy=scale)
                height, width = frame.shape[:2]
                image = mp.Image(
                    image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                )
                milliseconds = max(last_timestamp + 1, round(timestamp * 1000))
                result = landmarker.detect_for_video(image, milliseconds)
                last_timestamp = milliseconds
                poses = []
                for index, landmarks in enumerate(result.pose_landmarks):
                    world = (
                        result.pose_world_landmarks[index]
                        if index < len(result.pose_world_landmarks)
                        else None
                    )
                    value = pose_data(
                        landmarks,
                        world,
                        exercise,
                        EXERCISES[exercise]["joints"],
                        config.visibility,
                        width / height,
                    )
                    if value is not None:
                        poses.append(value)
                item = {
                    "time": round(timestamp, 5),
                    "people": len(result.pose_landmarks),
                    "poses": poses,
                    "legacy_signals": poses[0]["strict_signals"]
                    if len(poses) == 1 and len(result.pose_landmarks) == 1
                    else {},
                }
                samples.append(item)
                if progress and len(samples) % 30 == 0:
                    progress(min(90, int(90 * actual_end / expected_duration)))
    finally:
        capture.release()
    if decoded < expected_frames - 2 or actual_end < expected_duration - 0.5:
        raise ValueError("VIDEO_DECODE_INCOMPLETE")
    shots = assign_shots(path, samples)
    tracking = track_subject(samples)
    if time.monotonic() - started > config.max_runtime_seconds:
        raise TimeoutError("VIDEO_COUNTER_TIMEOUT")
    chosen, diagnostics = select_signal(samples, config)
    side = (
        "mixed"
        if chosen["name"] == "per_shot"
        else "right"
        if "right_" in chosen["name"]
        else "left"
    )
    scales = [s["body_scale"] for s in samples if s.get("body_scale")]
    result = count_series(
        samples,
        exercise,
        actual_end,
        config,
        signal_name=chosen["name"],
        body_scale=statistics.median(scales) if scales else 0.3,
    )
    result["config"].update(target_tracking=tracking, signal_diagnostics=diagnostics, shots=shots)
    result.update(
        decoded_frames=decoded,
        source_fps=fps,
        selected_side=side,
        model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        elapsed_seconds=round(time.monotonic() - started, 4),
        runtime_versions={"mediapipe": mp.__version__, "opencv": cv2.__version__},
    )
    return result, samples


def main():
    parser = argparse.ArgumentParser(description="FitLife whole-video pose-cycle counter")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--exercise", choices=EXERCISES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    try:
        result, samples = count_video(
            args.input,
            args.exercise,
            model_path=args.model,
            progress=lambda n: print(json.dumps({"progress": n}), flush=True),
        )
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.trace:
            args.trace.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"done": True}), flush=True)
    except Exception as exc:
        code = (
            str(exc)
            if isinstance(exc, (ValueError, TimeoutError)) and str(exc).isupper()
            else "VIDEO_COUNTER_FAILED"
        )
        print(json.dumps({"error_code": code}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
