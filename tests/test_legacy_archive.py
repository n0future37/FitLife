"""历史包完整性、跨用户隔离和导入重放；仅使用合成数据。"""

import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from fitlife.api import create_app
from fitlife.auth import issue_token
from fitlife.business import BusinessError
from fitlife.evaluate import clean
from fitlife.legacy_archive import HistoryService, canonical, import_history, load_bundle


@pytest.fixture
def bundle(tmp_path):
    rows = [
        {
            "table": "foods",
            "key": "1",
            "payload": {"id": 1, "name": "合成食品", "source": "rag"},
            "review_status": "historical_unverified",
        },
        {
            "table": "conversations",
            "key": "1",
            "payload": {"id": 1, "title": "合成历史"},
            "review_status": "historical_unverified",
        },
        {
            "table": "messages",
            "key": "1",
            "payload": {
                "id": 1,
                "conversation_id": 1,
                "pending_save": '{"name":"do not execute"}',
                "content": "<img src=x onerror=alert(1)>",
            },
            "review_status": "historical_unverified",
        },
    ]
    raw = b"".join(canonical(r) + b"\n" for r in rows)
    (tmp_path / "records.jsonl").write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path / "manifest.json").write_bytes(
        canonical(
            {
                "format": "fitlife-history-v1",
                "dataset_id": sha,
                "files": {"records.jsonl": sha},
                "record_count": 3,
                "default_owner_id": str(uuid4()),
                "table_counts": {"foods": 1, "conversations": 1, "messages": 1},
            }
        )
    )
    return tmp_path


def test_tampered_history_bundle_is_rejected(bundle):
    (bundle / "records.jsonl").write_text("[]\n")
    with pytest.raises(ValueError, match="ARCHIVE_HASH_MISMATCH"):
        load_bundle(bundle)


def test_manifest_path_escape_is_rejected(bundle):
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"] = {"../outside.txt": "0" * 64}
    path.write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match="ARCHIVE_HASH_MISMATCH"):
        load_bundle(bundle)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_history_import_and_authenticated_read_are_isolated(test_database, bundle):
    settings, engine = test_database
    alice, token = issue_token(engine)
    bob, other_token = issue_token(engine)
    try:
        first = import_history(engine, bundle, alice.user_id)
        second = import_history(engine, bundle, alice.user_id)
        assert first["imported"] == 3 and second["unchanged"] == 3
        service = HistoryService(engine)
        assert service.summary(alice)["record_count"] == 3
        assert service.summary(bob)["record_count"] == 0
        assert service.get_profile(alice)["version"] == 0
        messages = service.records(alice, "messages")["records"]
        assert messages[0]["payload"]["conversation_id"] == 1
        assert messages[0]["payload"]["pending_save"]
        with pytest.raises(BusinessError, match="INVALID_INPUT"):
            service.records(alice, "../messages")
        app = create_app(settings, engine=engine, model_factory=lambda _: None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/history")).status_code == 401
            own = await client.get(
                "/history/messages", headers={"Authorization": "Bearer " + token}
            )
            other = await client.get(
                "/history/messages", headers={"Authorization": "Bearer " + other_token}
            )
            assert own.status_code == 200 and own.json()["record_count"] == 1
            assert own.json()["read_only"]
            assert other.json()["record_count"] == 0
            assert (
                await client.get(
                    "/history/messages?limit=101", headers={"Authorization": "Bearer " + token}
                )
            ).status_code == 422
    finally:
        clean(engine, [alice, bob])
