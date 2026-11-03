"""私有上传/任务/核对/候选API；识别和业务确认分开。"""

import asyncio
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fitlife import media
from fitlife.business import Actor, BusinessError, Input
from fitlife.database import MediaAsset, MediaJob, User
from fitlife.domains import DomainService
from fitlife.media_types import MediaRequest


class Review(Input):
    version: int = Field(ge=1, strict=True)
    result: dict


class Proposal(Input):
    version: int = Field(ge=1, strict=True)
    data: dict


def register(app, engine, environment, identity):
    Identity = Annotated[Actor, Depends(identity)]
    RequestKey = Annotated[UUID, Header(alias="Idempotency-Key")]
    gate = asyncio.Semaphore(2)

    @app.post("/media/uploads")
    async def upload(request: Request, actor: Identity, kind: str):
        if kind not in {"image", "video"}:
            raise HTTPException(422, detail="INVALID_MEDIA_KIND")
        limit = media.MAX_IMAGE_BYTES if kind == "image" else media.MAX_VIDEO_BYTES
        try:
            await asyncio.wait_for(gate.acquire(), 2)
        except TimeoutError:
            raise HTTPException(429, detail="UPLOAD_CAPACITY_REACHED") from None
        try:
            parts = []
            size = 0
            async with asyncio.timeout(60):
                async for part in request.stream():
                    size += len(part)
                    if size > limit:
                        raise HTTPException(413, detail="MEDIA_SIZE_EXCEEDED")
                    parts.append(part)
            return await asyncio.to_thread(
                media.upload, engine, environment, actor, kind, b"".join(parts)
            )
        finally:
            gate.release()

    @app.post("/media/{asset_id}/jobs")
    def start(asset_id: UUID, body: MediaRequest, actor: Identity, key: RequestKey):
        return media.create_job(engine, actor, asset_id, body, key)

    @app.get("/media/jobs")
    def recent(actor: Identity):
        with Session(engine) as session:
            rows = session.scalars(
                select(MediaJob)
                .where(MediaJob.owner_id == str(actor.user_id))
                .order_by(MediaJob.created_at.desc())
                .limit(50)
            ).all()
            return {"jobs": [media.snapshot(row) for row in rows]}

    @app.get("/media/jobs/{job_id}")
    def status(job_id: UUID, actor: Identity):
        return media.get_job(engine, actor, job_id)

    @app.post("/media/jobs/{job_id}/cancel")
    def cancel(job_id: UUID, actor: Identity):
        return media.change_job(engine, actor, job_id, "cancel")

    @app.post("/media/jobs/{job_id}/retry")
    def retry(job_id: UUID, actor: Identity):
        return media.change_job(engine, actor, job_id, "retry")

    @app.patch("/media/jobs/{job_id}/review")
    def review(job_id: UUID, body: Review, actor: Identity):
        return media.change_job(engine, actor, job_id, "review", body.version, body.result)

    @app.post("/media/jobs/{job_id}/actions")
    def proposal(job_id: UUID, body: Proposal, actor: Identity, key: RequestKey):
        return media.propose_result(engine, actor, job_id, body.version, body.data, key)

    @app.get("/media/{asset_id}/content")
    def content(asset_id: UUID, actor: Identity):
        with Session(engine) as session:
            asset = media.owned(session, MediaAsset, actor, asset_id)
            path = media.asset_path(environment, asset)
            if asset.metadata_.get("deleted") or not path.is_file():
                raise BusinessError("MEDIA_NOT_FOUND")
            return FileResponse(
                path, media_type=asset.metadata_.get("mime", "application/octet-stream")
            )

    @app.delete("/media/{asset_id}")
    def delete(asset_id: UUID, actor: Identity):
        with Session(engine) as session, session.begin():
            session.get(User, str(actor.user_id), with_for_update=True)
            asset = media.owned(session, MediaAsset, actor, asset_id)
            jobs = session.scalars(
                select(MediaJob).where(
                    MediaJob.asset_id == asset.id, MediaJob.owner_id == asset.owner_id
                )
            ).all()
            for job in jobs:
                if job.status in {"queued", "running"}:
                    job.status = "cancelled"
                    from uuid import uuid4

                    job.nonce = str(uuid4())
            asset.metadata_ = {**asset.metadata_, "deleted": True}
            path = media.asset_path(environment, asset)
        path.unlink(missing_ok=True)
        return {"status": "deleted", "note": "源文件已删除；已确认业务记录和必要操作证据保留。"}

    @app.get("/domain-records/{record_type}")
    def records(record_type: str, start_date: date, end_date: date, actor: Identity):
        return DomainService(engine).lookup(actor, record_type, start_date, end_date)

    @app.post("/domain-records/{record_id}/deletion-actions")
    def deletion(record_id: UUID, actor: Identity, key: RequestKey):
        from fitlife.record_management import propose

        return propose(DomainService(engine), actor, record_id, "api:" + str(key))
