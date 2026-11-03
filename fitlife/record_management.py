"""新增领域记录的确认删除；保留操作证据，不连带删除已核算餐食快照。"""

import hashlib
import json

from pydantic import Field, model_validator
from sqlalchemy import select

from fitlife.business import BusinessError, Input, snapshot
from fitlife.database import DomainRecord, PendingAction


class DeleteRecord(Input):
    record_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    record_type: str = Field(min_length=1, max_length=32)
    expected_version: int = Field(strict=True, ge=1)
    values_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict

    @model_validator(mode="after")
    def matches_snapshot(self):
        if digest(self.values) != self.values_digest:
            raise ValueError("INVALID_RECORD_SNAPSHOT")
        return self


def digest(values):
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def propose(service, actor, id_, key, allowed_types=None):
    with service.sessions() as session:
        owner = service._user(session, actor)
        previous = session.scalar(
            select(PendingAction).where(
                PendingAction.owner_id == owner,
                PendingAction.request_key == key,
                PendingAction.kind == "delete_domain_record",
            )
        )
        if previous:
            if previous.payload["record_id"] != str(id_):
                raise BusinessError("IDEMPOTENCY_CONFLICT")
            return snapshot(previous)
        record = session.scalar(
            select(DomainRecord).where(DomainRecord.owner_id == owner, DomainRecord.id == str(id_))
        )
        if not record or (allowed_types and record.record_type not in allowed_types):
            raise BusinessError("RECORD_NOT_FOUND")
        payload = {
            "record_id": record.id,
            "record_type": record.record_type,
            "expected_version": record.version,
            "values_digest": digest(record.values),
            "values": record.values,
        }
    return service.propose(actor, "delete_domain_record", payload, key)


def apply(session, owner, payload):
    row = session.scalar(
        select(DomainRecord).where(
            DomainRecord.owner_id == owner,
            DomainRecord.id == payload["record_id"],
            DomainRecord.record_type == payload["record_type"],
        )
    )
    if (
        not row
        or row.version != payload["expected_version"]
        or digest(row.values) != payload["values_digest"]
    ):
        return None
    session.delete(row)
    return {"deleted_record_id": row.id}
