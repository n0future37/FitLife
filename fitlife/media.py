"""归属校验、持久化任务、租约与媒体证据绑定；文件仅放项目私有目录。"""

import hashlib
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife.business import BusinessError, utcnow
from fitlife.database import MediaAsset, MediaJob, User
from fitlife.domains import DomainService
from fitlife.media_types import RESULTS, MediaRequest

ROOT = Path(__file__).resolve().parents[1]
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_VIDEO_SECONDS = 180


def storage(environment):
    if environment not in {"dev", "test"}:
        raise ValueError("INVALID_ENVIRONMENT")
    return ROOT / "data" / "private" / environment


def asset_path(environment, asset):
    return storage(environment) / asset.owner_id / (asset.id + ".upload")


def owned(session, model, actor, id_):
    row = session.get(model, str(id_))
    if row is None or row.owner_id != str(actor.user_id):
        raise BusinessError("MEDIA_NOT_FOUND")
    return row


def snapshot(job):
    return {
        "job_id": job.id,
        "asset_id": job.asset_id,
        "task": job.task,
        "status": job.status,
        "version": job.version,
        "progress": job.progress,
        "attempts": job.attempts,
        "model_calls": job.model_calls,
        "result": job.result,
        "error_code": job.error_code,
        "reviewed": job.reviewed,
        "evidence": job.evidence,
        "note": (
            "视觉结果可能有误；视频仅覆盖采样片段，"
            "visible_reps不是整段准确次数。尚未保存为业务记录。"
        ),
    }


def probe(path, kind):
    with path.open("rb") as f:
        head = f.read(16)
    if kind == "image":
        if not (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")):
            raise BusinessError("UNSUPPORTED_IMAGE_FORMAT")
    elif not (head[4:8] == b"ftyp" or head.startswith(b"\x1aE\xdf\xa3")):
        raise BusinessError("UNSUPPORTED_VIDEO_FORMAT")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        done = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True
        )
        info = json.loads(done.stdout)
        stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
        width, height = int(stream["width"]), int(stream["height"])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 20000000:
            raise BusinessError("MEDIA_DIMENSIONS_EXCEEDED")
        duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0)
        if kind == "video" and not 0 < duration <= MAX_VIDEO_SECONDS:
            raise BusinessError("VIDEO_DURATION_EXCEEDED")
        return {
            "width": width,
            "height": height,
            "duration": duration,
            "mime": (
                ("video/webm" if head.startswith(b"\x1aE\xdf\xa3") else "video/mp4")
                if kind == "video"
                else "image/png"
                if head.startswith(b"\x89PNG")
                else "image/jpeg"
            ),
        }
    except BusinessError:
        raise
    except Exception:
        raise BusinessError("MEDIA_DECODE_FAILED") from None


def upload(engine, environment, actor, kind, data):
    if (
        kind not in {"image", "video"}
        or not data
        or len(data) > (MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES)
    ):
        raise BusinessError("MEDIA_SIZE_OR_KIND_INVALID")
    id_ = str(uuid4())
    folder = storage(environment) / str(actor.user_id)
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        folder.chmod(0o700)
    path = folder / (id_ + ".upload")
    path.write_bytes(data)
    try:
        metadata = probe(path, kind)
        with Session(engine) as session, session.begin():
            if session.get(User, str(actor.user_id)) is None:
                raise BusinessError("USER_NOT_FOUND")
            row = MediaAsset(
                id=id_,
                owner_id=str(actor.user_id),
                kind=kind,
                sha256=hashlib.sha256(data).hexdigest(),
                metadata_=metadata,
                created_at=utcnow(),
            )
            session.add(row)
        return {"asset_id": id_, "kind": kind, **metadata}
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def create_job(engine, actor, asset_id, request, key):
    request = MediaRequest.model_validate(request)
    values = request.model_dump()
    digest = hashlib.sha256(
        json.dumps([str(asset_id), values], sort_keys=True).encode()
    ).hexdigest()
    with Session(engine) as session, session.begin():
        session.get(User, str(actor.user_id), with_for_update=True)
        asset = owned(session, MediaAsset, actor, asset_id)
        if asset.metadata_.get("deleted"):
            raise BusinessError("MEDIA_NOT_FOUND")
        if (request.task == "workout_video") != (asset.kind == "video"):
            raise BusinessError("MEDIA_TASK_KIND_MISMATCH")
        previous = session.scalar(
            select(MediaJob).where(
                MediaJob.owner_id == asset.owner_id, MediaJob.request_key == str(key)
            )
        )
        if previous:
            if previous.input_digest != digest:
                raise BusinessError("IDEMPOTENCY_CONFLICT")
            return snapshot(previous)
        row = MediaJob(
            id=str(uuid4()),
            owner_id=asset.owner_id,
            asset_id=asset.id,
            request_key=str(key),
            input_digest=digest,
            task=request.task,
            message=request.message,
            status="queued",
            version=1,
            attempts=0,
            model_calls=0,
            progress=0,
            lease_until=utcnow(),
            nonce=str(uuid4()),
            reviewed=False,
            evidence={},
            partial={},
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return snapshot(row)


def get_job(engine, actor, id_):
    with Session(engine) as session:
        return snapshot(owned(session, MediaJob, actor, id_))


def change_job(engine, actor, id_, operation, version=None, result=None):
    with Session(engine) as session, session.begin():
        session.get(User, str(actor.user_id), with_for_update=True)
        job = owned(session, MediaJob, actor, id_)
        if operation == "cancel":
            if job.status in {"queued", "running", "cancelled"}:
                job.status = "cancelled"
                job.nonce = str(uuid4())
        elif operation == "retry":
            if job.status in {"queued", "running"}:
                return snapshot(job)
            if job.status != "failed" or job.attempts >= 3 or job.model_calls >= 6:
                raise BusinessError("MEDIA_RETRY_NOT_ALLOWED")
            job.status = "queued"
            job.error_code = None
            job.nonce = str(uuid4())
        elif operation == "review":
            if job.status != "succeeded" or job.version != version:
                raise BusinessError("STALE_MEDIA_REVIEW")
            checked = RESULTS[job.task].model_validate(result).model_dump(mode="json")
            if job.task == "workout_video":
                duration = session.get(MediaAsset, job.asset_id).metadata_["duration"]
                if any(o["end_sec"] > duration for o in checked["observations"]):
                    raise BusinessError("INVALID_EVIDENCE_RANGE")
            job.result = checked
            job.reviewed = True
            job.version += 1
        else:
            raise BusinessError("UNKNOWN_OPERATION")
        job.updated_at = utcnow()
        return snapshot(job)


def claim_job(engine, job_id=None):
    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(MediaJob)
            .where(
                (MediaJob.status == "queued")
                | ((MediaJob.status == "running") & (MediaJob.lease_until <= utcnow()))
            )
            .where(MediaJob.id == str(job_id) if job_id else True)
            .order_by(MediaJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        if job.attempts >= 3 or job.model_calls >= 6:
            job.status = "failed"
            job.error_code = "MEDIA_BUDGET_EXHAUSTED"
            return None
        job.status = "running"
        job.attempts += 1
        job.nonce = str(uuid4())
        job.lease_until = utcnow() + timedelta(seconds=300)
        session.flush()
        return {
            "id": job.id,
            "nonce": job.nonce,
            "owner_id": job.owner_id,
            "asset_id": job.asset_id,
            "task": job.task,
            "message": job.message,
            "partial": job.partial or {},
        }


def update_running(engine, id_, nonce, **updates):
    with Session(engine) as session, session.begin():
        job = session.get(MediaJob, id_, with_for_update=True)
        if (
            job is None
            or job.status != "running"
            or job.nonce != nonce
            or job.lease_until <= utcnow()
        ):
            raise BusinessError("MEDIA_EXECUTION_EXPIRED")
        if updates.pop("reserve_call", False):
            if job.model_calls >= 6:
                raise BusinessError("MEDIA_BUDGET_EXHAUSTED")
            job.model_calls += 1
        for key, value in updates.items():
            setattr(job, key, value)
        job.updated_at = utcnow()
        return snapshot(job)


def propose_result(engine, actor, id_, version, data, key):
    with Session(engine) as session:
        job = owned(session, MediaJob, actor, id_)
        if job.status != "succeeded" or job.version != version or not job.reviewed:
            raise BusinessError("MEDIA_REVIEW_REQUIRED")
        kind = {"nutrition_label": "food", "meal_photo": "photo_meal", "workout_video": "workout"}[
            job.task
        ]
        source = {"job_id": job.id, "version": job.version}
    key = "media:" + str(id_) + ":" + str(version)
    service = DomainService(engine)
    # Record fields come from the authenticated user's reviewed submission.
    candidate = service.prepare_record(actor, kind, data, key)
    payload = {**candidate, "source_media": source}
    identity = hashlib.sha256(
        json.dumps([str(id_), version, data], sort_keys=True).encode()
    ).hexdigest()
    return service.propose(actor, "domain_record", payload, key, request_fingerprint=identity)
