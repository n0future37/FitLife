import asyncio
import json
import shutil
import struct
import subprocess
import zlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife import media
from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import BusinessError
from fitlife.database import DomainRecord
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.media_worker import run_once
from fitlife.model import Reply

pytestmark = pytest.mark.integration


def png():
    def chunk(tag, data):
        return (
            struct.pack("!I", len(data))
            + tag
            + data
            + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 64) * 64))
        + chunk(b"IEND", b"")
    )


class VisionFixture:
    def __init__(self, task="nutrition_label", fail=False):
        self.task, self.fail = task, fail

    async def complete(self, instructions, items, tools):
        assert not tools and any(p["type"] == "input_image" for p in items[0]["content"])
        if self.fail:
            raise RuntimeError("SYNTHETIC_FAILURE")
        if self.task == "nutrition_label":
            result = {
                "kind": self.task,
                "name": "合成食品",
                "basis_amount": 100,
                "unit": "g",
                "energy_value": 100,
                "energy_unit": "kcal",
                "protein_g": 5,
                "carbs_g": 10,
                "fat_g": 2,
                "evidence": "synthetic",
                "uncertainty": "fixture only",
            }
        elif self.task == "meal_photo":
            result = {
                "kind": self.task,
                "items": [{"name": "合成食物", "amount_g": None, "estimated_energy_kcal": None}],
                "evidence": "synthetic",
                "uncertainty": "重量和营养未知",
            }
        else:
            result = {
                "kind": self.task,
                "observations": [
                    {
                        "name": "可见测试动作",
                        "start_sec": 0,
                        "end_sec": 2,
                        "visible_reps": 99,
                        "observation": "只观察采样帧",
                    }
                ],
                "uncertainty": "采样不能验证整段次数",
            }
        return Reply(
            text=json.dumps(result), response_model="vision-fixture", usage={"total_tokens": 10}
        )

    async def close(self):
        pass


def test_private_upload_review_confirm_and_media_version_fence(test_database):
    settings, engine = test_database
    alice, token = issue_token(engine)
    bob, other = issue_token(engine)
    try:
        with TestClient(create_app(settings, engine=engine)) as client:
            assert client.post("/media/uploads?kind=image", content=png()).status_code == 401
            assert (
                client.post(
                    "/media/uploads?kind=image", headers=headers(token), content=b"not an image"
                ).status_code
                == 409
            )
            response = client.post(
                "/media/uploads?kind=image", headers=headers(token), content=png()
            )
            assert response.status_code == 200, response.text
            asset = response.json()["asset_id"]
            assert client.get(f"/media/{asset}/content", headers=headers(other)).status_code == 404
            assert client.get(f"/media/{asset}/content", headers=headers(token)).content == png()
            key = uuid4()
            path = f"/media/{asset}/jobs"
            body = {"task": "nutrition_label"}
            job = client.post(path, headers=headers(token, key), json=body).json()
            assert (
                client.post(path, headers=headers(token, key), json=body).json()["job_id"]
                == job["job_id"]
            )
            assert (
                client.post(
                    path, headers=headers(token, key), json={"task": "meal_photo"}
                ).status_code
                == 409
            )
            assert asyncio.run(run_once(engine, "test", lambda _: VisionFixture()))
            job = client.get(f"/media/jobs/{job['job_id']}", headers=headers(token)).json()
            assert job["status"] == "succeeded" and job["model_calls"] == 1
            assert (
                client.get(f"/media/jobs/{job['job_id']}", headers=headers(other)).status_code
                == 404
            )
            record = {
                "name": "合成食品",
                "unit": "g",
                "energy_kcal": 100,
                "protein_g": 5,
                "carbs_g": 10,
                "fat_g": 2,
                "source": "synthetic reviewed label",
            }
            proposal_path = f"/media/jobs/{job['job_id']}/actions"
            assert (
                client.post(
                    proposal_path,
                    headers=headers(token, uuid4()),
                    json={"version": 1, "data": record},
                ).status_code
                == 409
            )
            reviewed = client.patch(
                f"/media/jobs/{job['job_id']}/review",
                headers=headers(token),
                json={"version": 1, "result": job["result"]},
            ).json()
            assert reviewed["reviewed"] and reviewed["version"] == 2
            pending = client.post(
                proposal_path, headers=headers(token, uuid4()), json={"version": 2, "data": record}
            ).json()
            assert pending["status"] == "pending"
            assert DomainService(engine).lookup(alice, "food")["records"] == []
            # A new reviewed version invalidates the previously bound confirmation.
            client.patch(
                f"/media/jobs/{job['job_id']}/review",
                headers=headers(token),
                json={"version": 2, "result": job["result"]},
            )
            stale = client.post(
                f"/actions/{pending['action_id']}/confirm",
                headers=headers(token),
                json=confirmation(pending),
            )
            assert stale.json()["status"] == "superseded"
            pending = client.post(
                proposal_path, headers=headers(token, uuid4()), json={"version": 3, "data": record}
            ).json()
            for _ in range(2):
                saved = client.post(
                    f"/actions/{pending['action_id']}/confirm",
                    headers=headers(token),
                    json=confirmation(pending),
                )
                assert saved.json()["status"] == "applied"
            replay = client.post(
                proposal_path, headers=headers(token, uuid4()), json={"version": 3, "data": record}
            )
            assert replay.json()["status"] == "applied", replay.text
            rows = DomainService(engine).lookup(alice, "food")["records"]
            assert len(rows) == 1 and rows[0]["source_media"]["version"] == 3
            assert client.delete(f"/media/{asset}", headers=headers(other)).status_code == 404
            assert (
                client.delete(f"/media/{asset}", headers=headers(token)).json()["status"]
                == "deleted"
            )
            assert client.get(f"/media/{asset}/content", headers=headers(token)).status_code == 404
            assert len(DomainService(engine).lookup(alice, "food")["records"]) == 1
    finally:
        clean(engine, [alice, bob])


def test_cancellation_retry_fences_and_persisted_budget(test_database):
    _, engine = test_database
    actor, _ = issue_token(engine)
    try:
        asset = media.upload(engine, "test", actor, "image", png())
        job = media.create_job(engine, actor, asset["asset_id"], {"task": "meal_photo"}, uuid4())
        claim = media.claim_job(engine)
        assert claim["id"] == job["job_id"]
        media.change_job(engine, actor, job["job_id"], "cancel")
        with pytest.raises(BusinessError, match="MEDIA_EXECUTION_EXPIRED"):
            media.update_running(engine, claim["id"], claim["nonce"], reserve_call=True)
        assert media.get_job(engine, actor, job["job_id"])["status"] == "cancelled"
        job = media.create_job(engine, actor, asset["asset_id"], {"task": "meal_photo"}, uuid4())
        assert asyncio.run(run_once(engine, "test", lambda _: VisionFixture(fail=True)))
        failed = media.get_job(engine, actor, job["job_id"])
        assert failed["status"] == "failed" and failed["model_calls"] == 1
        media.change_job(engine, actor, job["job_id"], "retry")
        assert asyncio.run(run_once(engine, "test", lambda _: VisionFixture(task="meal_photo")))
        done = media.get_job(engine, actor, job["job_id"])
        assert done["status"] == "succeeded" and done["model_calls"] == 2 and done["attempts"] == 2
        assert done["result"]["items"][0]["estimated_energy_kcal"] is None
    finally:
        clean(engine, [actor])


@pytest.mark.parametrize("extension", ["mp4", "webm"])
def test_short_video_sampling_never_claims_whole_video_repetitions(
    test_database, tmp_path, extension
):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    _, engine = test_database
    actor, _ = issue_token(engine)
    try:
        video = tmp_path / ("synthetic." + extension)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x64:r=5:d=4",
                "-an",
                "-c:v",
                "libx264" if extension == "mp4" else "libvpx-vp9",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            timeout=20,
        )
        asset = media.upload(engine, "test", actor, "video", video.read_bytes())
        assert asset["mime"] == ("video/mp4" if extension == "mp4" else "video/webm")
        job = media.create_job(engine, actor, asset["asset_id"], {"task": "workout_video"}, uuid4())
        assert asyncio.run(run_once(engine, "test", lambda _: VisionFixture(task="workout_video")))
        done = media.get_job(engine, actor, job["job_id"])
        assert done["status"] == "succeeded", done
        assert done["result"]["observations"][0]["visible_reps"] is None
        assert done["evidence"]["whole_video_count_verified"] is False
        assert len(done["evidence"]["sample_times"]) >= 2
        with Session(engine) as session:
            assert not session.scalar(
                select(DomainRecord).where(DomainRecord.owner_id == str(actor.user_id))
            )
    finally:
        clean(engine, [actor])
