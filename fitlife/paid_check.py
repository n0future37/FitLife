"""小额真实验收：跨域任务、标签识别、极短视频与Chat工具调用，共用一个预留账本。"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import zipfile
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from fitlife import media
from fitlife.auth import issue_token
from fitlife.database import DatabaseSettings
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.evaluate_multi import DATA, execute_case
from fitlife.evaluation_budget import PaidBudget
from fitlife.manage import migrate
from fitlife.media_worker import run_once
from fitlife.model import Settings

ROOT = Path(__file__).resolve().parents[1]


def fixtures(folder):
    label = folder / "label.txt"
    label.write_text(
        "SYNTHETIC TEST OATS\nPer 100 g\nEnergy 400 kcal\nProtein 10 g\nCarbs 60 g\nFat 12 g",
        encoding="utf-8",
        newline="\n",
    )
    font = Path("C:/Windows/Fonts/arial.ttf")
    if not font.exists():
        font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

    def escaped(p):
        return str(p.as_posix()).replace(":", "\\:")

    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=white:s=640x360",
        "-vf",
        f"drawtext=fontfile='{escaped(font)}':textfile='{escaped(label)}':fontsize=30:fontcolor=black:x=20:y=20:line_spacing=12",
        "-frames:v",
        "1",
        str(folder / "label.png"),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=20)
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
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(folder / "short.mp4"),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )


async def chat(budget):
    model = budget.factory(Settings(model="gpt-6-luna", api_family="chat", reasoning_effort="low"))
    try:
        tools = [
            {
                "type": "function",
                "name": "connectivity_ping",
                "description": "Connectivity probe.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "strict": False,
            }
        ]
        items = [
            {
                "role": "user",
                "content": "Call connectivity_ping with value 1, then report its tool result.",
            }
        ]
        reply = await model.complete("Follow the requested tool call.", items, tools)
        assert (
            len(reply.calls) == 1
            and reply.calls[0]["name"] == "connectivity_ping"
            and json.loads(reply.calls[0]["arguments"]) == {"value": 1}
        )
        items += reply.continuation + [
            {
                "type": "function_call_output",
                "call_id": reply.calls[0]["call_id"],
                "output": '{"status":"ok","value":1}',
            }
        ]
        final = await model.complete("Report only the actual tool result.", items, tools)
        assert final.text and not final.calls
        return {
            "passed": True,
            "response_model": final.response_model,
            "provider_request_id": final.request_id,
        }
    finally:
        await model.close()


def perception(engine, budget, folder, task, filename):
    actor, _ = issue_token(engine)
    started = time.perf_counter()
    try:
        asset = media.upload(
            engine,
            "test",
            actor,
            "video" if task == "workout_video" else "image",
            (folder / filename).read_bytes(),
        )
        pending = media.create_job(engine, actor, asset["asset_id"], {"task": task}, uuid4())
        asyncio.run(
            run_once(engine, "test", budget.factory, job_id=pending["job_id"], model_mode="live")
        )
        job = media.get_job(engine, actor, pending["job_id"])
        checks = {"succeeded": job["status"] == "succeeded"}
        if job["status"] == "succeeded" and task == "nutrition_label":
            value = job["result"]
            checks["label_values"] = all(
                value.get(k) == v
                for k, v in {
                    "basis_amount": 100,
                    "unit": "g",
                    "energy_value": 400,
                    "energy_unit": "kcal",
                    "protein_g": 10,
                    "carbs_g": 60,
                    "fat_g": 12,
                }.items()
            )
            checks["name"] = bool(value.get("name") and "OAT" in value["name"].upper())
            media.change_job(engine, actor, job["job_id"], "review", job["version"], value)
            if checks["label_values"] and checks["name"]:
                data = {
                    "name": value["name"],
                    "unit": "g",
                    "energy_kcal": value["energy_value"],
                    "protein_g": value["protein_g"],
                    "carbs_g": value["carbs_g"],
                    "fat_g": value["fat_g"],
                    "source": "synthetic known label, scripted review",
                }
                candidate = media.propose_result(
                    engine, actor, job["job_id"], 2, data, str(uuid4())
                )
                service = DomainService(engine)
                checks["no_unconfirmed_food"] = service.lookup(actor, "food")["records"] == []
                service.confirm(
                    actor, candidate["action_id"], candidate["revision"], candidate["digest"]
                )
                checks["confirmed_food_count"] = len(service.lookup(actor, "food")["records"]) == 1
        elif job["status"] == "succeeded":
            checks["no_invented_action"] = job["result"]["observations"] == []
            checks["no_whole_video_count"] = job["evidence"]["whole_video_count_verified"] is False
        return {
            "passed": all(checks.values()),
            "job_id": job["job_id"],
            "checks": checks,
            "error_code": job["error_code"],
            "data_classification": "synthetic",
            "case_seconds_excluding_cleanup": round(time.perf_counter() - started, 3),
            "observed_result": job["result"],
            "evidence": job["evidence"],
            "note": (
                "Known synthetic fixture; scripted user review. Not real-world perception accuracy."
            ),
        }
    finally:
        clean(engine, [actor])


def main():
    parser = argparse.ArgumentParser(
        description=("显式付费小额验收，所有部分共用预算文件，重跑不重置已预留金额")
    )
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=["cross_domain", "nutrition_label", "short_video", "chat_tools"],
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--budget-file", type=Path, default=ROOT / "artifacts/paid-checks/budget-20261102.json"
    )
    parser.add_argument("--budget-usd", default="1.5")
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("No model calls. Add --live only after checking model prices and budget.")
    os.environ.setdefault("FITLIFE_REASONING_EFFORT", "low")
    budget = PaidBudget(args.budget_file, args.budget_usd)
    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    folder = ROOT / "artifacts/paid-checks" / str(uuid4())
    folder.mkdir(parents=True)
    report = {
        "mode": "live",
        "results": {},
        "actual_billed_usd": None,
        "budget_file": str(args.budget_file),
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "fitlife").glob("*.py"))
        },
        "dataset_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
        "sdk_versions": {name: version(name) for name in ("openai", "mcp", "pydantic")},
    }
    with zipfile.ZipFile(folder / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in ("fitlife", "migrations", "evals"):
            for path in (ROOT / directory).rglob("*"):
                if (
                    path.is_file()
                    and path.suffix in {".py", ".jsonl", ".html", ".css", ".js", ".mako"}
                    and "__pycache__" not in path.parts
                ):
                    archive.write(path, str(path.relative_to(ROOT)))
        for name in ("pyproject.toml", "alembic.ini"):
            archive.write(ROOT / name, name)
    try:
        fixtures(folder)
        case = next(
            json.loads(line)
            for line in DATA.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["id"] == "cross-domain-record"
        )
        actions = [
            (
                "cross_domain",
                lambda: execute_case(engine, settings, case, live=True, budget=budget),
            ),
            (
                "nutrition_label",
                lambda: perception(engine, budget, folder, "nutrition_label", "label.png"),
            ),
            (
                "short_video",
                lambda: perception(engine, budget, folder, "workout_video", "short.mp4"),
            ),
            ("chat_tools", lambda: asyncio.run(chat(budget))),
        ]
        report["repeat"] = args.repeat
        for base_name, action in actions:
            if args.only and base_name not in args.only:
                continue
            for trial in range(args.repeat):
                name = base_name if args.repeat == 1 else f"{base_name}#{trial + 1}"
                try:
                    report["results"][name] = action()
                except Exception as exc:
                    report["results"][name] = {"passed": False, "error_type": type(exc).__name__}
                (folder / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        report["passed"] = all(r.get("passed") for r in report["results"].values())
        report["budget_reserved_usd"] = budget.state["reserved_usd"]
    finally:
        engine.dispose()
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"Paid smoke check: {report.get('passed', False)}; {folder / 'report.json'}")
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
