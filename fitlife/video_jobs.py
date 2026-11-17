"""Bounded local counter process; cancellation/nonce fencing and no model credentials."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from fitlife.business import BusinessError
from fitlife.media import ROOT, update_running
from fitlife.media_types import VideoResult


async def count_process(engine, job, path, exercise):

    process = None
    with tempfile.TemporaryDirectory(prefix=job["id"] + "-count-", dir=path.parent) as folder:
        target = Path(folder) / "result.json"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.endswith("_API_KEY") and k not in {"OPENROUTER", "FITLIFE_MODEL_API_KEY"}
        }
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "fitlife.video_counter",
                "--input",
                str(path),
                "--exercise",
                exercise,
                "--output",
                str(target),
                cwd=ROOT,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            error_code = None
            async with asyncio.timeout(235):
                while True:
                    try:
                        raw = await asyncio.wait_for(process.stdout.readline(), 3)
                    except TimeoutError:
                        await asyncio.to_thread(update_running, engine, job["id"], job["nonce"])
                        continue
                    if not raw:
                        break
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if "progress" in event:
                        await asyncio.to_thread(
                            update_running,
                            engine,
                            job["id"],
                            job["nonce"],
                            progress=max(0, min(95, int(event["progress"]))),
                        )
                    if "error_code" in event:
                        error_code = event["error_code"]
                return_code = await process.wait()
            if return_code or not target.is_file():
                allowed = {
                    "POSE_MODEL_NOT_INSTALLED",
                    "POSE_RUNTIME_NOT_INSTALLED",
                    "VIDEO_DECODE_FAILED",
                    "VIDEO_DECODE_INCOMPLETE",
                    "VIDEO_TIMELINE_UNAVAILABLE",
                    "VIDEO_COUNTER_TIMEOUT",
                    "VIDEO_DURATION_EXCEEDED",
                    "VIDEO_COUNTER_FAILED",
                }
                raise BusinessError(error_code if error_code in allowed else "VIDEO_COUNTER_FAILED")
            result = json.loads(target.read_text(encoding="utf-8"))
            await asyncio.to_thread(update_running, engine, job["id"], job["nonce"])
            return result
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()


async def run_count_job(engine, job, path, metadata, sha, trace):
    exercise = job["partial"]["count_request"]["exercise"]
    started = time.perf_counter()
    count = await count_process(engine, job, path, exercise)
    if count["source_sha256"] != sha or abs(count["duration_seconds"] - metadata["duration"]) > 0.5:
        raise BusinessError("VIDEO_COUNT_SOURCE_MISMATCH")
    result = VideoResult.model_validate(
        {
            "kind": "workout_video",
            "observations": [
                {
                    "name": count["exercise_name"],
                    "start_sec": 0,
                    "end_sec": min(count["duration_seconds"], metadata["duration"]),
                    "visible_reps": None,
                    "observation": "完整时间轴运动分析，按镜头统计可见往返；不评价动作安全性。",
                }
            ],
            "counting": count,
            "uncertainty": "；".join(count["limitations"]),
        }
    ).model_dump(mode="json")
    evidence = {
        "asset_sha256": sha,
        "analysis_version": count["method_version"],
        "provider": "local_mediapipe",
        "model_sha256": count["model_sha256"],
        "full_timeline_processed": count["full_timeline_processed"],
        "whole_video_count_verified": False,
        "note": "完整时间轴自动估计不等于人工真值；用户核对后才可提出待确认训练记录。",
    }
    await asyncio.to_thread(
        update_running,
        engine,
        job["id"],
        job["nonce"],
        status="succeeded",
        result=result,
        evidence=evidence,
        progress=100,
        error_code=None,
    )
    trace.append(
        {
            "kind": "local_video_count",
            "method_version": count["method_version"],
            "seconds": round(time.perf_counter() - started, 4),
            "model_calls": 0,
        }
    )
