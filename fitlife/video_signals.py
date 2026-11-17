"""Target continuity, task-relevant signals and relative excursion counting; no labels used."""

import math
import statistics


def joint_angle(points):
    import numpy as np

    a, b, c = [np.asarray(p, dtype=float) for p in points]
    u, v = a - b, c - b
    norm = np.linalg.norm(u) * np.linalg.norm(v)
    if norm < 1e-8:
        return None
    return float(np.degrees(np.arccos(np.clip(np.dot(u, v) / norm, -1, 1))))


def pose_data(landmarks, world, exercise, joints, visibility, aspect):
    points = [[p.x * aspect, p.y] for p in landmarks]
    confidence = [min(p.visibility, p.presence) for p in landmarks]
    visible = [p for p, c in zip(points, confidence) if c >= visibility]
    if len(visible) < 5:
        return None
    # Visible body only: off-screen hallucinated feet must not dominate subject size.
    xs = [min(aspect, max(0, p[0])) for p in visible]
    ys = [min(1, max(0, p[1])) for p in visible]
    box = [min(xs), min(ys), max(xs), max(ys)]
    area = (box[2] - box[0]) * (box[3] - box[1])
    if area <= 0.002:
        return None
    signals, scales, strict = {}, [], {}

    def available(indices):
        return all(confidence[i] >= visibility for i in indices)

    for side, offset in [("left", 0), ("right", 1)]:
        indices = [i + offset for i in joints]
        if all(confidence[i] >= 0.55 for i in indices):
            strict["legacy_" + side + "_angle2d"] = joint_angle([points[i] for i in indices])
        if available(indices):
            signals[side + "_angle2d"] = joint_angle([points[i] for i in indices])
            if world:
                signals[side + "_angle3d"] = joint_angle(
                    [[world[i].x, world[i].y, world[i].z] for i in indices]
                )
        shoulder, wrist, hip, ankle = (i + offset for i in (11, 15, 23, 27))
        if available([shoulder, hip]):
            scales.append(math.dist(points[shoulder], points[hip]))
        if exercise in {"push_up", "pull_up", "bench_press", "biceps_curl"}:
            pair = [shoulder, wrist]
        elif exercise == "sit_up":
            pair = [hip, shoulder]
        else:
            pair = [ankle, hip]
        if available(pair):
            signals[side + "_lift"] = points[pair[0]][1] - points[pair[1]][1]
        if exercise == "pull_up" and available([0, wrist]):
            signals[side + "_head_lift"] = points[wrist][1] - points[0][1]
        if exercise == "squat" and available([hip]):
            signals[side + "_hip_height"] = points[hip][1]
    return {
        "box": box,
        "anchors": [
            points[i] if confidence[i] >= visibility else None for i in (0, 11, 12, 23, 24)
        ],
        "area": area,
        "signals": signals,
        "strict_signals": strict,
        "body_scale": statistics.median(scales) if scales else (box[3] - box[1]) * 0.4,
    }


def _track_segment(samples):
    """Associate detections over time, then select the dominant continuous body track."""
    tracks = []
    for sample in samples:
        timestamp, poses = sample["time"], sample["poses"]
        options = []
        for p, pose in enumerate(poses):
            for track in tracks:
                if timestamp - track["last"] > 2:
                    continue
                old, new = track["box"], pose["box"]
                scale = max(0.1, math.hypot(old[2] - old[0], old[3] - old[1]))
                distance = (
                    math.hypot(
                        (old[0] + old[2] - new[0] - new[2]) / 2,
                        (old[1] + old[3] - new[1] - new[3]) / 2,
                    )
                    / scale
                )
                ratio = abs(math.log(max(0.001, pose["area"]) / max(0.001, track["area"])))
                shared = [
                    math.dist(a, b) / scale
                    for a, b in zip(track["anchors"], pose["anchors"])
                    if a is not None and b is not None
                ]
                if shared:
                    distance = statistics.median(shared)
                cost = distance + 0.05 * ratio
                if cost < 1.2 and ratio < 2.5:
                    options.append((cost, p, track["id"]))
        matched, used = {}, set()
        for _, p, id_ in sorted(options):
            if p not in matched and id_ not in used:
                matched[p] = id_
                used.add(id_)
        sample["subjects"] = []
        for p, pose in enumerate(poses):
            if p in matched:
                track = tracks[matched[p]]
            else:
                track = {"id": len(tracks), "weight": 0, "samples": 0}
                tracks.append(track)
            track.update(
                last=timestamp, box=pose["box"], area=pose["area"], anchors=pose["anchors"]
            )
            track["weight"] += pose["area"]
            track["samples"] += 1
            sample["subjects"].append({**pose, "track_id": track["id"]})
    if not tracks:
        for sample in samples:
            sample.update(signals={}, body_scale=None, target_box=None, target_ambiguous=False)
        return {"track_id": None, "competitor_ratio": None, "tracks": 0}
    ranked = sorted(tracks, key=lambda x: x["weight"], reverse=True)
    primary = ranked[0]
    competitor = ranked[1]["weight"] / primary["weight"] if len(ranked) > 1 else 0
    ambiguous_ids = {t["id"] for t in ranked[1:] if t["weight"] >= primary["weight"] * 0.65}
    for sample in samples:
        subjects = sample.pop("subjects")
        selected = next((p for p in subjects if p["track_id"] == primary["id"]), None)
        sample.update(
            signals=selected["signals"] if selected else {},
            body_scale=selected["body_scale"] if selected else None,
            target_box=selected["box"] if selected else None,
            target_ambiguous=any(p["track_id"] in ambiguous_ids for p in subjects)
            and selected is not None,
        )
        if sample["target_ambiguous"]:
            sample["signals"] = {}
        else:
            sample["signals"] = {**sample["signals"], **sample.get("legacy_signals", {})}
    return {
        "track_id": primary["id"],
        "competitor_ratio": round(competitor, 4),
        "tracks": len(tracks),
        "primary_samples": primary["samples"],
    }


def turning_cycles(samples, *, prominence, min_cycle=0.45, max_cycle=20, max_gap=0.25):
    """Relative reversals with left/right excursion support, no global posture threshold."""
    segments, current = [], []
    for item in samples:
        value = item.get("angle")
        if value is None:
            continue
        if current and (
            item["time"] - current[-1]["time"] > max_gap
            or item.get("shot", 0) != current[-1].get("shot", 0)
        ):
            segments.append(current)
            current = []
        current.append(item)
    if current:
        segments.append(current)
    cycles, signal_pairs = [], []
    for segment in segments:
        if len(segment) < 3:
            continue
        times = [p["time"] for p in segment]
        raw = [p["angle"] for p in segment]
        # Centered median rejects isolated pose spikes without shifting every event in time.
        values = [
            statistics.median(raw[max(0, i - 1) : min(len(raw), i + 2)]) for i in range(len(raw))
        ]
        high = low = 0
        direction = 0
        extrema = []
        for i, value in enumerate(values):
            if direction == 0:
                if value > values[high]:
                    high = i
                if value < values[low]:
                    low = i
                if value - values[low] >= prominence:
                    extrema.append(low)
                    direction = 1
                    high = i
                elif values[high] - value >= prominence:
                    extrema.append(high)
                    direction = -1
                    low = i
            elif direction == 1:
                if value >= values[high]:
                    high = i
                elif values[high] - value >= prominence:
                    extrema.append(high)
                    direction = -1
                    low = i
            else:
                if value <= values[low]:
                    low = i
                elif value - values[low] >= prominence:
                    extrema.append(low)
                    direction = 1
                    high = i
        if extrema:
            last = high if direction == 1 else low
            if last > extrema[-1] and abs(values[last] - values[extrema[-1]]) >= prominence:
                extrema.append(last)
        # Two excursions form one completed out-and-back; adjacent cycles share an endpoint.
        start = 0
        while start + 2 < len(extrema):
            a, b, c = extrema[start : start + 3]
            duration = times[c] - times[a]
            if duration < min_cycle:
                # Merge a short reversal into the next candidate instead of double counting it.
                extrema.pop(start + 1)
                if start + 1 < len(extrema):
                    extrema.pop(start + 1)
                continue
            unfinished_tail = (
                c == extrema[-1]
                and c == last
                and (
                    (values[a] > values[b] and values[c] < values[a] - prominence * 0.5)
                    or (values[a] < values[b] and values[c] > values[a] + prominence * 0.5)
                )
            )
            if duration <= max_cycle and not unfinished_tail:
                cycles.append({"start_sec": round(times[a], 3), "end_sec": round(times[c], 3)})
            start += 2
        signal_pairs.extend(zip(times, values))
    return cycles, signal_pairs


def analyze_signal(samples, name, config, body_scale=0.3):
    values = sorted(p["angle"] for p in samples if p.get("angle") is not None)
    coverage = len(values) / len(samples)
    span = (
        values[int((len(values) - 1) * 0.9)] - values[int((len(values) - 1) * 0.1)] if values else 0
    )
    floor = config.minimum_prominence_degrees if "angle" in name else max(0.012, body_scale * 0.08)
    prominence = max(floor, span * 0.25)
    cycles, pairs = turning_cycles(
        samples,
        prominence=prominence,
        min_cycle=config.min_cycle_seconds,
        max_cycle=config.max_cycle_seconds,
        max_gap=config.interpolation_gap_seconds,
    )
    durations = [c["end_sec"] - c["start_sec"] for c in cycles]
    regularity = (
        max(0, 1 - statistics.pstdev(durations) / statistics.mean(durations))
        if len(durations) > 1
        else 0.4
        if durations
        else 0
    )
    score = coverage * (0.5 + regularity) * min(1, span / max(floor * 3, 1e-6))
    return {
        "name": name,
        "coverage": round(coverage, 5),
        "span": round(span, 5),
        "prominence": round(prominence, 5),
        "count": len(cycles),
        "regularity": round(regularity, 4),
        "score": round(score, 5),
        "cycles": cycles,
    }


def _select_segment(samples, config):
    names = sorted({name for s in samples for name in s["signals"]})
    scales = [s["body_scale"] for s in samples if s.get("body_scale")]
    scale = statistics.median(scales) if scales else 0.3
    results = []
    for name in names:
        series = [
            {"time": s["time"], "angle": s["signals"].get(name), "shot": s.get("shot", 0)}
            for s in samples
        ]
        results.append(analyze_signal(series, name, config, scale))
    # Coverage and gap limits gate every task-relevant anatomical signal.
    for result in results:
        times = [
            samples[0]["time"],
            *[s["time"] for s in samples if result["name"] in s["signals"]],
            samples[-1]["time"],
        ]
        result["longest_gap"] = max(b - a for a, b in zip(times, times[1:]))
        result["usable"] = (
            result["coverage"] >= config.min_coverage
            and result["longest_gap"] <= config.max_missing_seconds
        )
    eligible = [x for x in results if x["usable"] and x["name"].startswith("legacy_")]
    if not eligible:
        eligible = [x for x in results if x["usable"]]
    chosen = max(
        eligible or results,
        key=lambda x: x["score"],
        default={"name": "unavailable", "cycles": [], "score": 0, "coverage": 0, "prominence": 0},
    )
    for s in samples:
        s["angle"] = s["signals"].get(chosen["name"])
    return chosen, [{k: v for k, v in x.items() if k != "cycles"} for x in results]


def assign_shots(path, samples):
    """Only abrupt, whole-frame scene changes; no identity inference across a cut."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    index, decoded, shot = 0, 0, 0
    fps = cap.get(cv2.CAP_PROP_FPS)
    previous = None
    cuts = []
    try:
        while index < len(samples):
            ok, frame = cap.read()
            if not ok:
                raise ValueError("VIDEO_DECODE_INCOMPLETE")
            decoded += 1
            timestamp = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if not math.isfinite(timestamp) or timestamp < 0:
                timestamp = (decoded - 1) / fps
            if timestamp + 1e-4 < samples[index]["time"]:
                continue
            small = cv2.resize(frame, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
            if previous is not None:
                difference = np.abs(small - previous) / 255
                mean_change = float(difference.mean())
                fraction = float((difference.max(axis=2) > 0.2).mean())
                if mean_change > 0.15 and fraction > 0.45:
                    shot += 1
                    cuts.append(
                        {
                            "time": round(timestamp, 4),
                            "mean_change": mean_change,
                            "changed_fraction": fraction,
                        }
                    )
            previous = small
            while index < len(samples) and samples[index]["time"] <= timestamp + 1e-4:
                samples[index]["shot"] = shot
                index += 1
    finally:
        cap.release()
    return {
        "count": shot + 1,
        "cuts": cuts,
        "note": (
            "Visible movement per shot; never join half cycles across cuts or deduplicate replays."
        ),
    }


def track_subject(samples):
    from itertools import groupby

    summaries = []
    for shot, group in groupby(samples, key=lambda s: s.get("shot", 0)):
        segment = list(group)
        summaries.append({"shot": shot, **_track_segment(segment)})
    return {
        "segments": summaries,
        "note": "Main visible subject per shot, not biometric identity across edits.",
    }


def select_signal(samples, config):
    from itertools import groupby

    diagnostics = []
    chosen_signals = []
    for shot, group in groupby(samples, key=lambda s: s.get("shot", 0)):
        segment = list(group)
        chosen, candidates = _select_segment(segment, config)
        chosen_signals.append(chosen)
        diagnostics.extend({"shot": shot, **item} for item in candidates)
        for item in segment:
            item["selected_signal"] = chosen["name"]
    if len(chosen_signals) == 1:
        return chosen_signals[0], diagnostics
    return {"name": "per_shot", "cycles": []}, diagnostics
