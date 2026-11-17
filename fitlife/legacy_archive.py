"""旧库一次性封存及MySQL历史资料；运行查询仅依赖新版项目。"""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from fitlife.business import BusinessError, BusinessService, utcnow
from fitlife.database import DatabaseSettings, DomainRecord, PendingAction, User
from fitlife.legacy_data import export_legacy, read_rows, sha256

TABLES = {
    "foods": "食品资料",
    "meals": "饮食记录",
    "body_records": "身体测量",
    "workouts": "训练记录",
    "daily_steps": "步数记录",
    "body_profile": "身体档案",
    "training_profile": "训练偏好",
    "daily_training_status": "训练日状态",
    "photo_meals": "照片餐食",
    "conversations": "历史会话",
    "messages": "历史消息",
    "chunks": "历史知识原文",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def snapshot(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source):
        raise ValueError("USE_NEW_OUTPUT_OUTSIDE_SOURCE")
    # The previously checked public-reference exporter also verifies source hashes/WAL state.
    reference = export_legacy(source, output / "reference")
    records, schemas, counts = [], {}, {}
    originals = output / "originals"
    originals.mkdir()
    for name in ("fit.db", "nutrition_knowledge.db"):
        path = source / "data" / name
        raw = path.read_bytes()
        expected = reference["source_hashes"]["data/" + name]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("SOURCE_CHANGED_DURING_ARCHIVE")
        (originals / name).write_bytes(raw)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SOURCE_DATABASE_INTEGRITY_FAILED")
            tables = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            expected_tables = set(TABLES) - {"chunks"} if name == "fit.db" else {"chunks"}
            if {x[0] for x in tables} != expected_tables:
                raise ValueError("UNREVIEWED_DATABASE_TABLE_SET")
            for table, ddl in tables:
                schemas[table] = ddl
                rows = read_rows(path, f'SELECT * FROM "{table}"')
                counts[table] = len(rows)
                key = (
                    "chunk_id"
                    if table == "chunks"
                    else (
                        "record_date" if table in {"daily_steps", "daily_training_status"} else "id"
                    )
                )
                for row in rows:
                    records.append(
                        {
                            "table": table,
                            "key": str(row[key]),
                            "payload": row,
                            "review_status": "historical_unverified",
                        }
                    )
        finally:
            connection.close()
        if sha256(path) != expected:
            raise ValueError("SOURCE_CHANGED_DURING_ARCHIVE")
    records.sort(key=lambda x: (x["table"], x["key"]))
    data = b"".join(canonical(row) + b"\n" for row in records)
    (output / "records.jsonl").write_bytes(data)
    dataset_id = hashlib.sha256(data).hexdigest()
    files = {p.relative_to(output).as_posix(): sha256(p) for p in output.rglob("*") if p.is_file()}
    manifest = {
        "format": "fitlife-history-v1",
        "dataset_id": dataset_id,
        "created_at": utcnow().isoformat() + "Z",
        "table_counts": counts,
        "record_count": len(records),
        "schemas": schemas,
        "files": files,
        "source_database_hashes": {
            name: reference["source_hashes"]["data/" + name]
            for name in ("fit.db", "nutrition_knowledge.db")
        },
        "source_unchanged": True,
        "default_owner_id": str(uuid5(NAMESPACE_URL, dataset_id)),
        "policy": "Historical source data only; pending actions never execute; no eval labels.",
        "not_needed": (
            "Superseded DB backups, duplicate reproduction exports and unreferenced media caches"
        ),
        "references": {
            "messages.conversation_id": "conversations.id",
            "meals.items_json[].food_id": "foods.id",
            "messages.adopted_*": "Original IDs retained, not live confirmations",
        },
    }
    (output / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    # Local evidence copies only. These immutable source snapshots are never the runtime backend.
    for path in originals.iterdir():
        path.chmod(0o444)
    return manifest


def load_bundle(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "fitlife-history-v1":
        raise ValueError("UNSUPPORTED_HISTORY_FORMAT")
    for name, sha in manifest["files"].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or sha256(path) != sha:
            raise ValueError("ARCHIVE_HASH_MISMATCH")
    raw = (folder / "records.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["dataset_id"]:
        raise ValueError("ARCHIVE_DATASET_MISMATCH")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    keys = [(r["table"], r["key"]) for r in rows]
    counts = Counter(r["table"] for r in rows)
    if (
        len(rows) != manifest["record_count"]
        or len(set(keys)) != len(keys)
        or any(k not in TABLES for k in counts)
        or any(counts[k] != n for k, n in manifest["table_counts"].items())
    ):
        raise ValueError("ARCHIVE_ROW_COUNTS_MISMATCH")
    return manifest, rows


def import_history(engine, folder, owner_id=None):
    manifest, rows = load_bundle(folder)
    owner = str(UUID(str(owner_id or manifest["default_owner_id"])))
    action_id = str(uuid5(UUID(owner), manifest["dataset_id"]))
    service = HistoryService(engine)
    with service.sessions() as session, session.begin():
        user = session.get(User, owner, with_for_update=True)
        if not user:
            session.add(User(id=owner))
            session.flush()
        action = session.get(PendingAction, action_id)
        if action:
            existing = session.scalars(
                select(DomainRecord).where(
                    DomainRecord.owner_id == owner, DomainRecord.source_action_id == action_id
                )
            ).all()
            expected = {f"{r['table']}:{r['key']}": r for r in rows}
            if (
                action.digest != manifest["dataset_id"]
                or len(existing) != len(rows)
                or any(r.values != expected.get(r.record_key) for r in existing)
            ):
                raise ValueError("IMPORTED_HISTORY_CHANGED")
            return {
                "owner_id": owner,
                "dataset_id": manifest["dataset_id"],
                "imported": 0,
                "unchanged": len(rows),
            }
        collision = session.scalar(
            select(DomainRecord.id)
            .where(DomainRecord.owner_id == owner, DomainRecord.record_type == "legacy_archive")
            .limit(1)
        )
        if collision:
            raise ValueError("USE_SEPARATE_HISTORY_OWNER_FOR_NEW_DATASET")
        now = utcnow()
        session.add(
            PendingAction(
                id=action_id,
                owner_id=owner,
                request_key=action_id,
                kind="legacy_data_import",
                payload={
                    "dataset_id": manifest["dataset_id"],
                    "record_count": len(rows),
                    "table_counts": manifest["table_counts"],
                },
                revision=1,
                digest=manifest["dataset_id"],
                base_profile_version=0,
                status="archived",
                created_at=now,
                expires_at=now + timedelta(seconds=1),
                result={"archived": len(rows)},
            )
        )
        session.flush()
        for row in rows:
            key = f"{row['table']}:{row['key']}"
            session.add(
                DomainRecord(
                    id=str(uuid5(UUID(action_id), key)),
                    owner_id=owner,
                    record_type="legacy_archive",
                    record_key=key,
                    values=row,
                    version=1,
                    source_action_id=action_id,
                    updated_at=now,
                )
            )
    return {
        "owner_id": owner,
        "dataset_id": manifest["dataset_id"],
        "imported": len(rows),
        "unchanged": 0,
    }


class HistoryService(BusinessService):
    def summary(self, actor):
        with self.sessions() as session:
            owner = self._user(session, actor)
            keys = session.scalars(
                select(DomainRecord.record_key).where(
                    DomainRecord.owner_id == owner, DomainRecord.record_type == "legacy_archive"
                )
            ).all()
        counts = Counter(k.split(":", 1)[0] for k in keys)
        return {
            "tables": [{"table": k, "label": v, "count": counts[k]} for k, v in TABLES.items()],
            "record_count": len(keys),
            "note": "历史资料保留旧值和关联，未经重新核实；不自动覆盖当前画像、计划或活动。",
        }

    def records(self, actor, table, offset=0, limit=30):
        if table not in TABLES or offset < 0 or not 1 <= limit <= 100:
            raise BusinessError("INVALID_INPUT")
        with self.sessions() as session:
            owner = self._user(session, actor)
            query = select(DomainRecord).where(
                DomainRecord.owner_id == owner,
                DomainRecord.record_type == "legacy_archive",
                DomainRecord.record_key.startswith(table + ":", autoescape=True),
            )
            count = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(
                query.order_by(DomainRecord.record_key).offset(offset).limit(limit)
            ).all()
            return {
                "table": table,
                "label": TABLES[table],
                "record_count": count,
                "offset": offset,
                "has_more": offset + len(rows) < count,
                "records": [r.values for r in rows],
                "read_only": True,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("snapshot")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    load = commands.add_parser("import")
    load.add_argument("folder", type=Path)
    load.add_argument("--environment", choices=("dev", "test"), required=True)
    load.add_argument("--user-id", type=UUID)
    args = parser.parse_args()
    if args.command == "snapshot":
        manifest = snapshot(args.source, args.output)
        print(
            json.dumps(
                {k: manifest[k] for k in ("record_count", "table_counts", "default_owner_id")}
            )
        )
    else:
        engine = DatabaseSettings(environment=args.environment).engine()
        try:
            from fitlife.manage import migrate

            migrate(engine, args.environment)
            print(json.dumps(import_history(engine, args.folder, args.user_id)))
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
