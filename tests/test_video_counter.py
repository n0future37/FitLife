import asyncio
import copy
import hashlib
import math
import shutil
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fitlife import media, video_jobs
from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.media_types import MediaRequest
from fitlife.media_worker import prompt, run_once
from fitlife.video_counter import DEFAULT_MODEL, EXERCISES, CountConfig, count_series, count_video


def curve(seconds=10, offset=0):
    return [
        {"time": i / 10, "angle": 110 + 70 * math.cos(math.pi * i / 10 + offset), "people": 1}
        for i in range(int(seconds * 10) + 1)
    ]


@pytest.mark.parametrize("exercise", EXERCISES)
@pytest.mark.parametrize("offset", [0, math.pi])
def test_complete_cycles_count_in_either_starting_phase(exercise, offset):
    result = count_series(curve(offset=offset), exercise, 10)
    assert result["status"] == "counted"
    assert result["repetitions"] == 5
    assert len(result["cycles"]) == 5
    assert all(c["end_sec"] > c["start_sec"] for c in result["cycles"])


def test_half_cycle_and_static_posture_do_not_create_repetitions():
    assert count_series(curve(seconds=1), "squat", 1)["repetitions"] == 0
    samples = [{"time": i / 10, "angle": 170, "people": 1} for i in range(51)]
    assert count_series(samples, "squat", 5)["repetitions"] == 0


@pytest.mark.parametrize("failure", ["incomplete", "missing", "multiple"])
def test_coverage_failures_abstain_instead_of_returning_a_whole_count(failure):
    samples = curve(seconds=4)
    if failure == "incomplete":
        samples = samples[:20]
    elif failure == "missing":
        for item in samples:
            item["angle"] = None
    else:
        for item in samples:
            item["people"] = 2
    result = count_series(samples, "squat", 4)
    assert result["status"] == "needs_review"
    assert result["repetitions"] is None
    assert result["limitations"]


def test_a_repetition_cannot_bridge_a_detection_gap():
    samples = [
        {"time": i / 10, "angle": 170 if i < 8 else None if i < 20 else 60, "people": 1}
        for i in range(41)
    ]
    assert count_series(samples, "squat", 4)["observed_cycles"] == 0


def test_counter_request_and_legacy_model_schema_have_separate_authority():
    with pytest.raises(ValidationError):
        MediaRequest(task="meal_photo", count_exercise="squat")
    with pytest.raises(ValidationError):
        MediaRequest(task="workout_video", count_exercise="unknown")
    schema = prompt("workout_video").split("输出schema：", 1)[1]
    assert '"counting"' not in schema and "RepetitionCount" not in schema


def make_video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "empty-scene.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=128x96:r=10:d=4",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return path


def test_real_local_decoder_covers_eof_and_rejects_an_empty_scene(tmp_path):
    pytest.importorskip("mediapipe")
    if not DEFAULT_MODEL.is_file():
        pytest.skip("explicit pose model provisioning required")
    path = make_video(tmp_path)
    result, samples = count_video(path, "squat", config=CountConfig(max_runtime_seconds=30))
    assert result["decoded_frames"] == 40
    assert result["full_timeline_processed"]
    assert result["status"] == "needs_review" and result["repetitions"] is None
    assert result["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert samples[0]["time"] == 0 and samples[-1]["time"] >= 3.8


def fixture_count(path, exercise):
    result = count_series(curve(seconds=4), exercise, 4)
    result.update(
        decoded_frames=40,
        source_fps=10,
        selected_side="left",
        model_sha256="1" * 64,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        elapsed_seconds=0,
        runtime_versions={"counter": "fixed-test-fixture"},
    )
    return result


@pytest.mark.integration
def test_count_job_review_correct_confirm_replay_and_ownership(
    test_database, tmp_path, monkeypatch
):
    settings, engine = test_database
    alice, token = issue_token(engine)
    bob, other = issue_token(engine)
    try:
        path = make_video(tmp_path)

        async def fixed_counter(engine_, job, source, exercise):
            return fixture_count(source, exercise)

        monkeypatch.setattr(video_jobs, "count_process", fixed_counter)

        def forbidden_model(_):
            raise AssertionError("local counting must not initialize any cloud model")

        with TestClient(create_app(settings, engine=engine)) as client:
            asset = client.post(
                "/media/uploads?kind=video", headers=headers(token), content=path.read_bytes()
            ).json()
            key = uuid4()
            body = {"task": "workout_video", "count_exercise": "push_up"}
            url = f"/media/{asset['asset_id']}/jobs"
            job = client.post(url, headers=headers(token, key), json=body).json()
            assert (
                client.post(url, headers=headers(token, key), json=body).json()["job_id"]
                == job["job_id"]
            )
            assert (
                client.post(
                    url, headers=headers(token, key), json={**body, "count_exercise": "squat"}
                ).status_code
                == 409
            )
            assert asyncio.run(run_once(engine, "test", forbidden_model, job_id=job["job_id"]))
            done = client.get(f"/media/jobs/{job['job_id']}", headers=headers(token)).json()
            assert done["status"] == "succeeded" and done["model_calls"] == 0
            assert done["result"]["counting"]["repetitions"] == 2
            assert (
                client.get(f"/media/jobs/{job['job_id']}", headers=headers(other)).status_code
                == 404
            )
            assert DomainService(engine).lookup(alice, "workout")["records"] == []
            tampered = copy.deepcopy(done["result"])
            tampered["counting"]["selected_side"] = "right"
            assert (
                client.patch(
                    f"/media/jobs/{job['job_id']}/review",
                    headers=headers(token),
                    json={"version": 1, "result": tampered},
                ).status_code
                == 409
            )
            reviewed = client.patch(
                f"/media/jobs/{job['job_id']}/review",
                headers=headers(token),
                json={"version": 1, "result": done["result"]},
            ).json()
            # User can correct the suggestion of 2 to 3; automatic evidence remains unchanged.
            data = {
                "record_date": str(today()),
                "items": [{"category": "力量", "name": "俯卧撑", "reps": "3"}],
            }
            url = f"/media/jobs/{job['job_id']}/actions"
            pending = client.post(
                url,
                headers=headers(token, uuid4()),
                json={"version": reviewed["version"], "data": data},
            ).json()
            assert pending["status"] == "pending"
            assert (
                client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(other),
                    json=confirmation(pending),
                ).status_code
                == 404
            )
            for _ in range(2):
                assert (
                    client.post(
                        f"/actions/{pending['action_id']}/confirm",
                        headers=headers(token),
                        json=confirmation(pending),
                    ).json()["status"]
                    == "applied"
                )
            rows = DomainService(engine).lookup(alice, "workout")["records"]
            assert len(rows) == 1 and rows[0]["values"]["items"][0]["reps"] == "3"
            assert (
                client.get(f"/media/jobs/{job['job_id']}", headers=headers(token)).json()["result"][
                    "counting"
                ]["repetitions"]
                == 2
            )
    finally:
        clean(engine, [alice, bob])


@pytest.mark.integration
def test_cancelled_counter_cannot_publish_late_result(test_database, tmp_path, monkeypatch):
    _, engine = test_database
    actor, _ = issue_token(engine)
    try:
        path = make_video(tmp_path)
        asset = media.upload(engine, "test", actor, "video", path.read_bytes())
        job = media.create_job(
            engine,
            actor,
            asset["asset_id"],
            {"task": "workout_video", "count_exercise": "squat"},
            uuid4(),
        )

        async def cancelled(engine_, value, source, exercise):
            media.change_job(engine_, actor, value["id"], "cancel")
            return fixture_count(source, exercise)

        monkeypatch.setattr(video_jobs, "count_process", cancelled)
        assert asyncio.run(run_once(engine, "test", job_id=job["job_id"]))
        done = media.get_job(engine, actor, job["job_id"])
        assert done["status"] == "cancelled" and done["result"] is None
        assert DomainService(engine).lookup(actor, "workout")["records"] == []
    finally:
        clean(engine, [actor])
