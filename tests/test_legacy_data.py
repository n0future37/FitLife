"""合成旧库验证抽取边界；不依赖真实个人数据库。"""

import hashlib
import json
import sqlite3

import pytest

from fitlife.legacy_data import export_legacy, sha256


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "old"
    folder = root / "data" / "nutrition_base"
    folder.mkdir(parents=True)
    chunks, sources = [], []
    for key in ("cns2022", "who_diet", "who_sodium"):
        text = "合成参考资料 " + key
        raw = ("<p>" + text + "</p>").encode()
        (folder / f"{key}.html").write_bytes(raw)
        (folder / f"{key}.txt").write_text(text, encoding="utf-8")
        url = "https://example.org/" + key
        sources.append(
            {
                "id": key,
                "title": key,
                "url": url,
                "version": "2000-01-01",
                "retrieved_at": "2000-01-02T00:00:00+00:00",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "chunks": 1,
            }
        )
        chunks.append(
            {"chunk_id": key + "_01", "title": key, "locator": "2000-01-01;" + url, "text": text}
        )
    (folder / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    (folder / "base.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in chunks), encoding="utf-8"
    )
    with sqlite3.connect(root / "data" / "nutrition_knowledge.db") as c:
        c.execute("CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,payload TEXT)")
        c.executemany(
            "INSERT INTO chunks VALUES(?,?)", [(x["chunk_id"], json.dumps(x)) for x in chunks]
        )
    with sqlite3.connect(root / "data" / "fit.db") as c:
        c.execute(
            "CREATE TABLE foods(id INTEGER,name TEXT,unit TEXT,energy_kcal REAL,"
            "protein_g REAL,carbs_g REAL,fat_g REAL,source TEXT)"
        )
        c.execute("INSERT INTO foods VALUES(1,'synthetic','g',10,1,1,0,'rag')")
        c.execute("CREATE TABLE messages(content TEXT)")
        c.execute("INSERT INTO messages VALUES('PRIVATE_SENTINEL_DO_NOT_EXPORT')")
    return root


def test_export_retains_provenance_without_personal_or_authoritative_food(legacy, tmp_path):
    before = sha256(legacy / "data" / "fit.db")
    output = tmp_path / "export"
    result = export_legacy(legacy, output)
    assert result["knowledge_count"] == 3
    assert result["food_candidate_count"] == result["food_schema_valid"] == 1
    assert result["food_authoritative_import_count"] == 0
    assert result["source_unchanged"] and sha256(legacy / "data" / "fit.db") == before
    assert "data/nutrition_knowledge.db" in result["source_hashes"]
    for path in output.rglob("*"):
        if path.is_file():
            assert "PRIVATE_SENTINEL_DO_NOT_EXPORT" not in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="USE_NEW_OUTPUT"):
        export_legacy(legacy, output)


def test_tampered_snapshot_is_rejected_before_export(legacy, tmp_path):
    (legacy / "data/nutrition_base/cns2022.html").write_text("tampered")
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="SOURCE_SNAPSHOT_HASH_MISMATCH"):
        export_legacy(legacy, output)
    assert not output.exists()


def test_active_wal_is_rejected(legacy, tmp_path):
    (legacy / "data/fit.db-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="CHECKPOINT_FIRST"):
        export_legacy(legacy, tmp_path / "export")


def test_invalid_food_remains_pending_and_does_not_block_public_reference(legacy, tmp_path):
    with sqlite3.connect(legacy / "data/fit.db") as c:
        c.execute("UPDATE foods SET protein_g=NULL")
    result = export_legacy(legacy, tmp_path / "export")
    assert result["knowledge_count"] == 3
    assert result["food_schema_valid"] == 0
    assert result["food_authoritative_import_count"] == 0
    row = json.loads((tmp_path / "export/foods-pending-review.jsonl").read_text())
    assert row["food"]["protein_g"] is None
    assert row["status"] == "pending_review"


def test_database_and_public_snapshot_mismatch_is_rejected(legacy, tmp_path):
    with sqlite3.connect(legacy / "data/nutrition_knowledge.db") as c:
        payload = json.loads(c.execute("SELECT payload FROM chunks LIMIT 1").fetchone()[0])
        payload["text"] = "altered"
        c.execute(
            "UPDATE chunks SET payload=? WHERE chunk_id=?",
            (json.dumps(payload), payload["chunk_id"]),
        )
    with pytest.raises(ValueError, match="DATABASE_SNAPSHOT_CONTENT_MISMATCH"):
        export_legacy(legacy, tmp_path / "export")
