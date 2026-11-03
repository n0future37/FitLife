"""身体、饮食和补充训练工具的 MySQL 服务；不接触旧 SQLite 或个人材料。"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from fitlife.business import BusinessError, BusinessService, utcnow
from fitlife.database import DomainRecord, Profile
from fitlife.domain_types import SCHEMAS, DomainPayload

NUTRIENTS = ("energy_kcal", "protein_g", "carbs_g", "fat_g")


def record_snapshot(row):
    return {
        "id": row.id,
        "record_type": row.record_type,
        "record_key": row.record_key,
        "version": row.version,
        "values": row.values,
        "source_action_id": row.source_action_id,
        "source_media": row.source_media,
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def find_record(session, owner, kind, key):
    return session.scalar(
        select(DomainRecord).where(
            DomainRecord.owner_id == owner,
            DomainRecord.record_type == kind,
            DomainRecord.record_key == key,
        )
    )


def calculation(session, owner, items):
    totals = {n: Decimal(0) for n in NUTRIENTS}
    details, versions = [], {}
    for item in items:
        food = session.scalar(
            select(DomainRecord).where(
                DomainRecord.owner_id == owner,
                DomainRecord.record_type == "food",
                DomainRecord.id == str(item["food_id"]),
            )
        )
        if not food:
            raise BusinessError("FOOD_NOT_FOUND")
        amount = Decimal(str(item["amount"]))
        values = {n: Decimal(str(food.values[n])) * amount / 100 for n in NUTRIENTS}
        for n in NUTRIENTS:
            totals[n] += values[n]
        details.append(
            {
                "food_id": food.id,
                "name": food.values["name"],
                "unit": food.values["unit"],
                "amount": float(amount),
                **{n: float(v.quantize(Decimal(".01"))) for n, v in values.items()},
            }
        )
        versions[food.id] = food.version
    return {
        "items": details,
        "total": {n: float(v.quantize(Decimal(".01"))) for n, v in totals.items()},
        "food_versions": versions,
    }


def apply_domain(session, owner, action, payload):
    parsed = DomainPayload.model_validate(payload)
    row = find_record(session, owner, parsed.record_type, parsed.record_key)
    if (row.version if row else 0) != parsed.expected_version:
        return None
    values = parsed.values
    if parsed.source_media:
        from fitlife.database import MediaJob

        source = parsed.source_media
        if set(source) != {"job_id", "version"}:
            raise BusinessError("INVALID_MEDIA_SOURCE")
        job = session.get(MediaJob, str(source["job_id"]))
        if not job or job.owner_id != owner:
            raise BusinessError("MEDIA_NOT_FOUND")
        if job.status != "succeeded" or not job.reviewed or job.version != source["version"]:
            return None
    if parsed.record_type == "meal":
        try:
            actual = calculation(session, owner, values["items"])
        except BusinessError:
            return None
        if actual != values.get("calculation"):
            return None
    if row is None:
        row = DomainRecord(
            id=str(uuid4()),
            owner_id=owner,
            record_type=parsed.record_type,
            record_key=parsed.record_key,
            version=0,
        )
        session.add(row)
    # Partial fields patch existing daily/profile values; omitted fields are not erased.
    row.values = {**(row.values or {}), **values}
    row.version += 1
    row.source_media = parsed.source_media
    row.source_action_id, row.updated_at = action.id, utcnow()
    session.flush()
    return {"record_id": row.id, "record_type": row.record_type, "version": row.version}


def validate_bundle_constraints(session, owner, payload):
    from fitlife.constraints import plan_rules

    profile = session.get(Profile, owner)
    final_values = dict(profile.values if profile else {})
    for operation in payload["operations"]:
        if operation["kind"] == "profile_patch":
            final_values.update(operation["payload"])
        elif operation["kind"] == "profile_forget":
            for field in operation["payload"]["fields"]:
                final_values.pop(field, None)
    for operation in payload["operations"]:
        if operation["kind"] in {"save_plan", "adjust_plan"}:
            plan = operation["payload"]
            violations, applied = plan_rules(
                final_values, plan["sessions"], plan.get("temporary_constraint_exceptions", [])
            )
            if violations:
                return False
            plan["temporary_constraints_applied"] = applied
    return True


def apply_bundle(service, session, owner, action, payload):
    class StaleBundle(Exception):
        pass

    results = []
    if not validate_bundle_constraints(session, owner, payload):
        return None
    try:
        # A stale operation rolls back the whole bundle, including earlier successful operations.
        with session.begin_nested():
            for operation in sorted(
                payload["operations"], key=lambda op: op["kind"] == "profile_patch"
            ):
                child = SimpleNamespace(
                    id=action.id,
                    kind=operation["kind"],
                    payload=operation["payload"],
                    base_profile_version=operation["base_profile_version"],
                    base_plan_version=operation.get("base_plan_version"),
                    result=None,
                    status="pending",
                )
                service._apply_operation(session, owner, child, child.payload)
                if child.status == "superseded":
                    raise StaleBundle
                results.append(child.result)
    except StaleBundle:
        return None
    return {"operations": results}


class DomainService(BusinessService):
    def lookup(self, actor, record_type, start_date=None, end_date=None, *, name=None):
        if record_type not in SCHEMAS:
            raise BusinessError("UNKNOWN_RECORD_TYPE")
        if (start_date is None) != (end_date is None):
            raise BusinessError("DATE_RANGE_REQUIRED")
        if start_date is not None and not 0 <= (end_date - start_date).days <= 365:
            raise BusinessError("INVALID_DATE_RANGE")
        with self.sessions() as session:
            owner = self._user(session, actor)
            if record_type in {"body_profile", "training_profile"}:
                fields = (
                    {"height_cm", "age", "sex"}
                    if record_type == "body_profile"
                    else {"space", "equipment", "goal", "limitations"}
                )
                profile = session.get(Profile, owner)
                values = {
                    k: v for k, v in (profile.values if profile else {}).items() if k in fields
                }
                return {
                    "profile": values,
                    "status": "ok" if values else "empty",
                    "version": profile.version if profile else 0,
                    "provenance": {
                        k: v
                        for k, v in (profile.provenance if profile else {}).items()
                        if k in fields
                    },
                }
            query = select(DomainRecord).where(
                DomainRecord.owner_id == owner, DomainRecord.record_type == record_type
            )
            if start_date is not None and record_type != "food":
                query = query.where(
                    DomainRecord.record_key.between(
                        start_date.isoformat(), end_date.isoformat() + "~"
                    )
                )
            if name is not None:
                if not 1 <= len(name.strip()) <= 100:
                    raise BusinessError("INVALID_FOOD_NAME")
                query = query.where(DomainRecord.record_key.contains(name.strip(), autoescape=True))
            rows = session.scalars(
                query.order_by(DomainRecord.record_key, DomainRecord.id).limit(101)
            ).all()
            records = [record_snapshot(row) for row in rows[:100]]
            result = {
                "records": records,
                "has_more": len(rows) > 100,
                "status": "ok" if rows else "empty",
                "record_type": record_type,
                "start_date": str(start_date) if start_date else None,
                "end_date": str(end_date) if end_date else None,
                "note": "无记录不等于实际零；明细最多100条，has_more为真时不能当完整统计。",
            }
            if record_type in {"body_profile", "training_profile"}:
                result["profile"] = records[0]["values"] if records else {}
            return result

    def propose_record(self, actor, record_type, data, request_key, request_day):
        if record_type in {"body_profile", "training_profile"}:
            values = (
                SCHEMAS[record_type].model_validate(data).model_dump(mode="json", exclude_none=True)
            )
            return self.propose(actor, "profile_patch", values, request_key)
        payload = self.prepare_record(actor, record_type, data, request_key, request_day)
        import hashlib
        import json

        identity = hashlib.sha256(
            json.dumps([record_type, data, str(request_day)], sort_keys=True, default=str).encode()
        ).hexdigest()
        return self.propose(
            actor, "domain_record", payload, request_key, request_fingerprint=identity
        )

    def prepare_record(self, actor, record_type, data, request_key, request_day=None):
        from fitlife.business import today

        request_day = request_day or today()
        values = (
            SCHEMAS[record_type].model_validate(data).model_dump(mode="json", exclude_none=True)
        )
        if not values:
            raise BusinessError("EMPTY_UPDATE")
        if record_type in {"body", "steps", "workout"}:
            values.setdefault("record_date", request_day.isoformat())
        if record_type == "meal":
            values.setdefault("meal_date", request_day.isoformat())
        if record_type == "food":
            key = values["name"].strip()
        elif record_type in {"body", "steps"}:
            key = values["record_date"]
        else:
            day = values.get("record_date") or values["meal_date"]
            key = day + ":" + request_key.removeprefix("agent:")
        with self.sessions() as session:
            owner = self._user(session, actor)
            row = find_record(session, owner, record_type, key)
            version = row.version if row else 0
            if record_type == "meal":
                values["calculation"] = calculation(session, owner, values["items"])
        return {
            "record_type": record_type,
            "record_key": key,
            "values": values,
            "expected_version": version,
        }

    def calculate_meal(self, actor, items):
        with self.sessions() as session:
            owner = self._user(session, actor)
            return calculation(session, owner, items)

    def feedback(self, actor, data, request_key):
        with self.sessions() as session:
            owner = self._user(session, actor)
            row = session.scalar(
                select(DomainRecord).where(
                    DomainRecord.owner_id == owner,
                    DomainRecord.id == str(data.workout_id),
                    DomainRecord.record_type == "workout",
                )
            )
            if row is None:
                raise BusinessError("WORKOUT_NOT_FOUND")
            values = dict(row.values)
            if data.item_index is None:
                values["feedback"] = data.feedback
            else:
                items = [dict(x) for x in values["items"]]
                if data.item_index >= len(items):
                    raise BusinessError("INVALID_ITEM_INDEX")
                items[data.item_index]["feedback"] = data.feedback
                values["items"] = items
            payload = {
                "record_type": "workout",
                "record_key": row.record_key,
                "values": values,
                "expected_version": row.version,
            }
        return self.propose(actor, "domain_record", payload, request_key)

    def collect_proposal(self, actor, operations, request_key):
        return self.propose(actor, "action_bundle", {"operations": operations}, request_key)


def register_domain_tools(server, engine, actor, key, request_day, call, collect=False):
    service = DomainService(engine)
    service.collect_candidates = collect

    def propose(kind, data):
        return call(
            service.propose_record,
            actor,
            kind,
            data.model_dump(mode="json", exclude_none=True),
            key,
            request_day,
        )

    from fitlife.domain_types import (
        BodyInput,
        BodyProfileInput,
        FoodInput,
        HeightInput,
        MealItem,
        MealRecordInput,
        StepsInput,
        TrainingProfileInput,
        WorkoutFeedbackInput,
        WorkoutInput,
    )

    @server.tool()
    def lookup_body_tool(start_date: date, end_date: date) -> dict:
        """查本人起止日期内身体测量。含首尾，至多366天。空记录不表示体重为0。"""
        return call(service.lookup, actor, "body", start_date, end_date)

    @server.tool()
    def lookup_body_profile_tool() -> dict:
        """查本人身高/填写时年龄/性别；缺少不猜测，不推断生日。"""
        return call(service.lookup, actor, "body_profile")

    @server.tool()
    def save_body_profile_tool(data: BodyProfileInput) -> dict:
        """提出身体基础档案候选；只填用户明确字段，等待确认。"""
        return propose("body_profile", data)

    @server.tool()
    def record_height_tool(data: HeightInput) -> dict:
        """提出身高更新候选，等待用户确认。"""
        return propose("body_profile", data)

    @server.tool()
    def record_body_tool(data: BodyInput) -> dict:
        """提出当天或历史身体测量候选；体重必填，不猜体脂、腰围、睡眠。"""
        return propose("body", data)

    @server.tool()
    def lookup_steps_tool(start_date: date, end_date: date) -> dict:
        """查本人指定日期（含首尾）已记录步数；未记录不等于0。"""
        return call(service.lookup, actor, "steps", start_date, end_date)

    @server.tool()
    def record_steps_tool(data: StepsInput) -> dict:
        """提出本人每日步数候选，不以步行分钟推算步数；确认前不保存。"""
        return propose("steps", data)

    @server.tool()
    def lookup_training_profile_tool() -> dict:
        """查本人训练场地、器械、目标和限制；建议前先查询。"""
        return call(service.lookup, actor, "training_profile")

    @server.tool()
    def save_training_profile_tool(data: TrainingProfileInput) -> dict:
        """提出训练档案更新候选；当前明确字段优先，确认前不保存。"""
        return propose("training_profile", data)

    @server.tool()
    def lookup_workouts_tool(start_date: date, end_date: date) -> dict:
        """查本人文字训练明细、组次和反馈；步行分钟记录另用query_activities。"""
        return call(service.lookup, actor, "workout", start_date, end_date)

    @server.tool()
    def record_workout_tool(data: WorkoutInput) -> dict:
        """提出一份文字训练候选，不猜时长/次数/阻力；力量字段仅用于力量项目。"""
        return propose("workout", data)

    @server.tool()
    def add_workout_feedback_tool(data: WorkoutFeedbackInput) -> dict:
        """先查本人的workout UUID和从0开始的item_index，再提出反馈候选。"""
        return call(service.feedback, actor, data, key)

    @server.tool()
    def lookup_food_tool(name: str) -> dict:
        """按名字查本人确认食物库，返回真实UUID及每100g/ml营养值；不可编造ID。"""
        return call(lambda: service.lookup(actor, "food", name=name))

    @server.tool()
    def save_food_tool(food: FoodInput) -> dict:
        """提出食物及营养来源候选；用户确认后进入本人食物库，不是医学结论。"""
        return propose("food", food)

    @server.tool()
    def calculate_meal_tool(items: list[MealItem]) -> dict:
        """用已查询的本人食物UUID和g/ml用量计算营养，只读。"""
        return call(service.calculate_meal, actor, [x.model_dump(mode="json") for x in items])

    @server.tool()
    def record_meal_tool(meal: MealRecordInput) -> dict:
        """提出一餐候选，服务器按真实食物核算；确认前不保存。"""
        return propose("meal", meal)

    @server.tool()
    def lookup_meals_tool(start_date: date, end_date: date) -> dict:
        """查本人起止日期内餐食与当时营养快照；无记录不是零摄入。"""
        return call(service.lookup, actor, "meal", start_date, end_date)

    @server.tool()
    def lookup_photo_meals_tool(start_date: date, end_date: date) -> dict:
        """查本人日期范围内已确认照片估算餐食；未知营养不补零，不当成精确摄入。"""
        return call(service.lookup, actor, "photo_meal", start_date, end_date)

    from fitlife import knowledge
    from fitlife.statistics import summarize

    @server.tool()
    def summarize_body_tool(start_date: date, end_date: date) -> dict:
        """代码统计本人区间身体测量及首末变化，缺失保持未知，不推断变化原因。"""
        return call(summarize, service, actor, "body", start_date, end_date)

    @server.tool()
    def summarize_exercise_tool(start_date: date, end_date: date) -> dict:
        """代码统计本人步数、训练数量和活动分钟，分开保留，未记录不算实际零。"""
        return call(summarize, service, actor, "exercise", start_date, end_date)

    @server.tool()
    def summarize_diet_tool(start_date: date, end_date: date) -> dict:
        """代码统计本人餐食；食物库核算与照片估算分开，未知营养不补零。"""
        return call(summarize, service, actor, "diet", start_date, end_date)

    @server.tool()
    def search_nutrition_knowledge_tool(
        query: str, purpose: str = "advice", limit: int = 5
    ) -> dict:
        """检索用户已核对导入的本地资料，带出处/版本摘要；无资料明确not_configured。"""
        return call(knowledge.search, engine, query, purpose, limit)

    @server.tool()
    def propose_knowledge_food_tool(chunk_id: str) -> dict:
        """用实际检索到的完整食品知识条目提出食物候选，不保存、不冒充已有食品UUID。"""
        values = call(knowledge.food_from_chunk, engine, chunk_id)
        if values.get("status") == "error":
            return values
        return call(service.propose_record, actor, "food", values, key, request_day)

    @server.tool()
    def get_diet_preferences_tool() -> dict:
        """读取本人确认饮食偏好，来源与更新时间来自唯一权威画像。"""
        profile = service.get_profile(actor)
        return {
            "diet_preferences": profile["values"].get("diet_preferences"),
            "version": profile["version"],
            "provenance": profile["provenance"].get("diet_preferences"),
        }

    @server.tool()
    def save_diet_preferences_tool(preference: str) -> dict:
        """仅按本轮明确要求提出饮食偏好更新候选，用户确认前不生效。"""
        return call(service.propose, actor, "profile_patch", {"diet_preferences": preference}, key)

    from uuid import UUID

    from fitlife.record_management import propose as propose_delete

    @server.tool()
    def propose_body_record_deletion_tool(record_id: UUID) -> dict:
        """先查本人身体记录真实UUID，再提出删除候选；确认前不删除。"""
        return call(propose_delete, service, actor, record_id, key, {"body"})

    @server.tool()
    def propose_diet_record_deletion_tool(record_id: UUID) -> dict:
        """先查本人食物或餐食真实UUID，再提出删除候选；已核算历史餐食快照保留。"""
        return call(propose_delete, service, actor, record_id, key, {"food", "meal", "photo_meal"})

    @server.tool()
    def propose_training_record_deletion_tool(record_id: UUID) -> dict:
        """先查本人训练/步数真实UUID，再提出删除候选；确认前不删除。"""
        return call(propose_delete, service, actor, record_id, key, {"steps", "workout"})

    import os

    from fitlife.business import TemporaryConstraint
    from fitlife.constraints import applicable

    trusted_domain = os.getenv("FITLIFE_TRUSTED_DOMAIN")

    @server.tool()
    def get_temporary_constraints_tool(start_date: date, end_date: date) -> dict:
        """查本人已确认、与目标日期范围相交的临时约束；过去/未来范围不以今天替代。"""
        if not 0 <= (end_date - start_date).days <= 365:
            return {"status": "error", "error_code": "INVALID_DATE_RANGE"}
        profile = service.get_profile(actor)
        return {
            "constraints": applicable(profile["values"], start_date, end_date, trusted_domain),
            "profile_version": profile["version"],
            "start_date": str(start_date),
            "end_date": str(end_date),
        }

    def change_constraint(data=None, remove=None):
        with service.sessions() as session:
            owner = service._user(session, actor)
            profile = session.get(Profile, owner)
            current = list((profile.values if profile else {}).get("temporary_constraints", []))
            version = profile.version if profile else 0
        name = data.name if data else remove
        if remove and not any(c["name"] == name for c in current):
            raise BusinessError("CONSTRAINT_NOT_FOUND")
        updated = [c for c in current if c["name"] != name]
        if data:
            updated.append(data.model_dump(mode="json", exclude_none=True))
        return service.propose(
            actor,
            "profile_patch",
            {"temporary_constraints": updated},
            key,
            expected_profile_version=version,
        )

    @server.tool()
    def propose_temporary_constraint_tool(data: TemporaryConstraint) -> dict:
        """仅按用户明确要求提出带日期的临时约束；同名更新、保留其他项，确认前不生效。"""
        return call(change_constraint, data)

    @server.tool()
    def propose_constraint_forget_tool(name: str) -> dict:
        """仅按明确要求，提出忘记指定名称临时约束的候选；先查询真实名称，不删除其他项。"""
        return call(lambda: change_constraint(remove=name))

    return service
