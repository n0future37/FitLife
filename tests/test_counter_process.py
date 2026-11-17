import asyncio
from uuid import uuid4

import pytest
from test_video_counter import fixture_count, make_video

from fitlife import media, video_jobs
from fitlife.auth import issue_token
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.media_worker import run_once


@pytest.mark.asyncio
async def test_cancelling_native_process_reaps_it_and_does_not_pass_cloud_keys(
    tmp_path, monkeypatch
):
    captured = {}

    class Output:
        async def readline(self):
            await asyncio.sleep(60)

    class Process:
        stdout, returncode = Output(), None

        def kill(self):
            captured["killed"] = True
            self.returncode = -1

        async def wait(self):
            captured["reaped"] = True
            return self.returncode

    async def spawn(*args, **kwargs):
        captured.update(env=kwargs["env"], target=args[args.index("--output") + 1])
        return Process()

    monkeypatch.setenv("SILI_API_KEY", "synthetic-do-not-forward")
    monkeypatch.setenv("OPENROUTER", "synthetic-do-not-forward")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        video_jobs.count_process(None, {"id": str(uuid4())}, tmp_path / "input.upload", "squat")
    )
    for _ in range(100):
        if "target" in captured:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured["killed"] and captured["reaped"]
    assert "SILI_API_KEY" not in captured["env"] and "OPENROUTER" not in captured["env"]
    from pathlib import Path

    assert not Path(captured["target"]).parent.exists()


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["hash", "duration"])
def test_counter_rejects_result_from_another_source(test_database, tmp_path, monkeypatch, invalid):
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

        async def wrong_result(engine_, job_, source, exercise):
            result = fixture_count(source, exercise)
            if invalid == "hash":
                result["source_sha256"] = "0" * 64
            else:
                result["duration_seconds"] += 2
            return result

        monkeypatch.setattr(video_jobs, "count_process", wrong_result)
        assert asyncio.run(run_once(engine, "test", job_id=job["job_id"]))
        done = media.get_job(engine, actor, job["job_id"])
        assert done["status"] == "failed" and done["error_code"] == "VIDEO_COUNT_SOURCE_MISMATCH"
        assert done["result"] is None
        assert DomainService(engine).lookup(actor, "workout")["records"] == []
    finally:
        clean(engine, [actor])
