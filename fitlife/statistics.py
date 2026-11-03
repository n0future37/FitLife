"""区间统计由代码计算；缺失、照片估算与食物库核算分别保留。"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from fitlife.business import BusinessError
from fitlife.database import Activity, DomainRecord


def summarize(service, actor, domain, start, end):
    if not 0 <= (end - start).days <= 365:
        raise BusinessError("INVALID_DATE_RANGE")
    types = {"body": {"body"}, "diet": {"meal", "photo_meal"}, "exercise": {"steps", "workout"}}[
        domain
    ]
    with service.sessions() as session:
        owner = service._user(session, actor)
        rows = session.scalars(
            select(DomainRecord)
            .where(
                DomainRecord.owner_id == owner,
                DomainRecord.record_type.in_(types),
                DomainRecord.record_key.between(str(start), str(end) + "~"),
            )
            .order_by(DomainRecord.record_key, DomainRecord.id)
            .limit(5001)
        ).all()
        if len(rows) > 5000:
            return {
                "status": "incomplete",
                "error_code": "SUMMARY_ROW_LIMIT",
                "record_count_at_least": 5001,
            }
        activities = (
            session.scalars(
                select(Activity).where(
                    Activity.owner_id == owner, Activity.occurred_on.between(start, end)
                )
            ).all()
            if domain == "exercise"
            else []
        )
        daily = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            records = [r for r in rows if r.record_key[:10] == str(day)]
            entry = {"date": str(day), "record_count": len(records)}
            if domain == "body":
                entry["weight_kg"] = records[-1].values["weight_kg"] if records else None
            elif domain == "exercise":
                steps = [r.values["steps"] for r in records if r.record_type == "steps"]
                entry.update(
                    steps=steps[-1] if steps else None,
                    workout_count=sum(r.record_type == "workout" for r in records),
                    activity_minutes=sum(a.minutes for a in activities if a.occurred_on == day),
                )
                entry["activity_record_count"] = sum(a.occurred_on == day for a in activities)
            else:
                meals = [r for r in records if r.record_type == "meal"]
                photos = [r for r in records if r.record_type == "photo_meal"]
                photo_items = [item for row in photos for item in row.values["items"]]
                known = [
                    i["estimated_energy_kcal"]
                    for i in photo_items
                    if i.get("estimated_energy_kcal") is not None
                ]
                entry.update(
                    calculated_nutrients={
                        n: float(
                            sum(
                                (Decimal(str(r.values["calculation"]["total"][n])) for r in meals),
                                Decimal(0),
                            )
                        )
                        if meals
                        else None
                        for n in ("energy_kcal", "protein_g", "carbs_g", "fat_g")
                    },
                    estimated_photo_energy_kcal=float(
                        sum((Decimal(str(x)) for x in known), Decimal(0))
                    )
                    if known
                    else None,
                    photo_items_unknown_energy=len(photo_items) - len(known),
                    photo_meal_count=len(photos),
                )
            daily.append(entry)
        result = {
            "status": "ok" if rows or activities else "empty",
            "start_date": str(start),
            "end_date": str(end),
            "record_count": len(rows),
            "days_in_range": len(daily),
            "daily": daily,
            "note": (
                "未记录不等于实际为零；食物库核算与照片估算分开；步数、训练和活动分钟不相互换算。"
            ),
        }
        if domain == "body":
            known = [d for d in daily if d["weight_kg"] is not None]
            result.update(
                first=known[0] if known else None,
                last=known[-1] if known else None,
                weight_delta_kg=float(
                    Decimal(str(known[-1]["weight_kg"])) - Decimal(str(known[0]["weight_kg"]))
                )
                if len(known) > 1
                else None,
            )
        return result
