"""只读抽取旧Fit公共资料和待审核食品；不迁移个人事实，不连接MySQL。"""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from fitlife.domain_types import FoodInput
from fitlife.knowledge import Chunk


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path, query):
    if not path.is_file():
        raise ValueError("SOURCE_DATABASE_NOT_FOUND")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("CLOSE_SOURCE_APP_AND_CHECKPOINT_FIRST")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query).fetchmany(5001)
        if len(rows) > 5000:
            raise ValueError("LEGACY_EXPORT_LIMIT")
        return [dict(row) for row in rows]
    finally:
        connection.close()


def export_legacy(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or output.exists():
        raise ValueError("USE_NEW_OUTPUT_OUTSIDE_SOURCE")
    folder = source / "data" / "nutrition_base"
    databases = [source / "data" / name for name in ("fit.db", "nutrition_knowledge.db")]
    sources = json.loads((folder / "sources.json").read_text(encoding="utf-8"))
    if {s["id"] for s in sources} != {"cns2022", "who_diet", "who_sodium"}:
        raise ValueError("UNREVIEWED_SOURCE_SET")
    files = [*databases, folder / "sources.json", folder / "base.jsonl"]
    files += [folder / f"{s['id']}.{ext}" for s in sources for ext in ("html", "txt")]
    before = {p.relative_to(source).as_posix(): sha256(p) for p in files}
    originals = {
        c.chunk_id: c
        for line in (folder / "base.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
        for c in [Chunk.model_validate_json(line)]
    }
    for item in sources:
        if sha256(folder / f"{item['id']}.html") != item["sha256"]:
            raise ValueError("SOURCE_SNAPSHOT_HASH_MISMATCH")
    rows = read_rows(databases[1], "SELECT chunk_id,payload FROM chunks ORDER BY chunk_id")
    if len(rows) != len(originals) or len(rows) != sum(s["chunks"] for s in sources):
        raise ValueError("SOURCE_CHUNK_COUNT_MISMATCH")
    chunks, counts, provenance = [], Counter(), []
    prefix = "legacy-" + before["data/nutrition_knowledge.db"][:12] + "-"
    for row in rows:
        chunk = Chunk.model_validate_json(row["payload"])
        if chunk.chunk_id != row["chunk_id"] or originals.get(chunk.chunk_id) != chunk:
            raise ValueError("DATABASE_SNAPSHOT_CONTENT_MISMATCH")
        matches = [s for s in sources if chunk.chunk_id.startswith(s["id"] + "_")]
        if len(matches) != 1 or chunk.food is not None:
            raise ValueError("UNREVIEWED_CHUNK_TYPE")
        item = matches[0]
        text = (folder / f"{item['id']}.txt").read_text(encoding="utf-8")
        if (
            chunk.title != item["title"]
            or item["url"] not in chunk.locator
            or item["version"] not in chunk.locator
            or chunk.text not in text
        ):
            raise ValueError("SOURCE_PROVENANCE_MISMATCH")
        payload = chunk.model_dump(mode="json")
        payload["chunk_id"] = prefix + chunk.chunk_id
        payload["title"] = "【历史公开资料快照】" + chunk.title
        payload["locator"] += "；旧采集日期" + item["retrieved_at"][:10]
        chunks.append(Chunk.model_validate(payload).model_dump(mode="json"))
        counts[item["id"]] += 1
        provenance.append(
            {
                "chunk_id": payload["chunk_id"],
                "legacy_id": chunk.chunk_id,
                "source_id": item["id"],
                "snapshot_sha256": item["sha256"],
            }
        )
    if any(counts[s["id"]] != s["chunks"] for s in sources):
        raise ValueError("SOURCE_GROUP_COUNT_MISMATCH")
    foods = read_rows(databases[0], "SELECT * FROM foods ORDER BY id")
    candidates, food_counts, names = [], Counter(), Counter()
    for row in foods:
        payload = {k: v for k, v in row.items() if k != "id"}
        try:
            FoodInput.model_validate(payload)
            valid = True
        except ValidationError:
            valid = False
        category = (
            "label_claim_unverified"
            if row.get("source") == "包装营养标签"
            else "retrieval_without_citation"
            if row.get("source") == "rag"
            else "unverified_other"
        )
        key = (str(row.get("name", "")).strip().casefold(), row.get("unit"))
        names[key] += 1
        food_counts[category] += 1
        candidates.append(
            {
                "legacy_id": row["id"],
                "status": "pending_review",
                "schema_valid": valid,
                "category": category,
                "basis": "per_100_g_or_ml_legacy_tool_contract",
                "food": payload,
            }
        )
    after = {p.relative_to(source).as_posix(): sha256(p) for p in files}
    if before != after:
        raise ValueError("SOURCE_CHANGED_DURING_EXPORT")
    for path in databases:
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError("SOURCE_BECAME_ACTIVE_DURING_EXPORT")
    output.mkdir(parents=True)
    snapshots = output / "snapshots"
    snapshots.mkdir()
    for path in files[2:]:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != before[path.relative_to(source).as_posix()]:
            raise ValueError("SOURCE_CHANGED_DURING_EXPORT")
        (snapshots / path.name).write_bytes(raw)
    for name, records in (
        ("knowledge.jsonl", chunks),
        ("foods-pending-review.jsonl", candidates),
    ):
        (output / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
            encoding="utf-8",
        )
    manifest = {
        "export_version": "legacy-selective-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source_root": str(source),
        "source_hashes": before,
        "source_unchanged": True,
        "sources": sources,
        "chunk_provenance": provenance,
        "knowledge_count": len(chunks),
        "knowledge_source_counts": dict(counts),
        "knowledge_status": "historical_snapshot_integrity_checked_not_current_clinical_review",
        "food_candidate_count": len(candidates),
        "food_categories": dict(food_counts),
        "food_schema_valid": sum(c["schema_valid"] for c in candidates),
        "food_duplicate_name_unit_groups": sum(n > 1 for n in names.values()),
        "food_authoritative_import_count": 0,
        "excluded": [
            "body_profile",
            "body_records",
            "conversations",
            "messages",
            "daily_steps",
            "daily_training_status",
            "meals",
            "photo_meals",
            "training_profile",
            "workouts",
        ],
        "no_model_calls": True,
        "output_hashes": {
            str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = export_legacy(args.source, args.output)
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "knowledge_count",
                    "food_candidate_count",
                    "food_schema_valid",
                    "source_unchanged",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
