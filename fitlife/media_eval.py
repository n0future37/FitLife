"""Small real-media evaluation; author labels stay outside model inputs; append-only campaigns."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from fitlife.api import create_app
from fitlife.api_check import confirmation, headers
from fitlife.auth import issue_token
from fitlife.business import today
from fitlife.database import DatabaseSettings
from fitlife.domains import DomainService
from fitlife.evaluate import clean
from fitlife.experiment_budget import ExperimentBudget
from fitlife.manage import migrate
from fitlife.media_worker import prompt, run_once
from fitlife.model import Settings
from fitlife.model_profiles import configured, resolve
from fitlife.video_counter import DEFAULT_MODEL, EXERCISES, VERSION, CountConfig

ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "real-media-rules-v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(rows, task):
    """Abstentions never become zero; conditional error always includes its denominator."""
    usable = [r for r in rows if r.get("prediction") is not None]
    errors = [r["prediction"] - r["truth"] for r in usable]
    result = {
        "runs": len(rows),
        "distinct_assets": len({r["id"] for r in rows}),
        "estimated_runs": len(usable),
        "coverage": len(usable) / len(rows) if rows else None,
        "abstained_or_failed": len(rows) - len(usable),
        "conditional_mae": statistics.mean(map(abs, errors)) if errors else None,
        "conditional_rmse": statistics.mean(e * e for e in errors) ** 0.5 if errors else None,
        "conditional_bias": statistics.mean(errors) if errors else None,
        "conditional_mape": statistics.mean(
            abs(r["prediction"] - r["truth"]) / r["truth"] for r in usable if r["truth"] > 0
        )
        if any(r["truth"] > 0 for r in usable)
        else None,
        "mean_wall_seconds": statistics.mean(r["wall_seconds"] for r in rows) if rows else None,
        "wall_scope": "sequential upload+inference+automated review/confirm/DB checks"
        if task == "food"
        else "sequential subprocess startup+decode+pose+cycle counting+trace write",
        "independence_note": (
            "Repeated runs are not independent assets; "
            "small selected public datasets, not population accuracy."
        ),
    }
    if task == "video":
        result.update(
            exact_count_all_runs=sum(e == 0 for e in errors),
            within_one_all_runs=sum(abs(e) <= 1 for e in errors),
            exact_rate_all_runs=sum(e == 0 for e in errors) / len(rows) if rows else None,
            within_one_rate_all_runs=sum(abs(e) <= 1 for e in errors) / len(rows) if rows else None,
            given_action=True,
            cloud_model_calls=0,
        )
    else:
        result["within_20pct_all_runs"] = sum(
            abs(r["prediction"] - r["truth"]) <= r["truth"] * 0.2 for r in usable
        )
        result["side_effect_checks_passed"] = sum(r.get("side_effects_passed", False) for r in rows)
    per_asset = []
    for id_ in sorted({r["id"] for r in rows}):
        group = [r for r in rows if r["id"] == id_]
        values = [r["prediction"] for r in group if r.get("prediction") is not None]
        per_asset.append(
            {
                "id": id_,
                "runs": len(group),
                "predictions": [r.get("prediction") for r in group],
                "truth": group[0]["truth"],
                "prediction_stdev": statistics.stdev(values) if len(values) >= 2 else None,
            }
        )
    result["per_asset"] = per_asset
    return result


def frozen_protocol(task, split, repeats, manifest, dataset):
    paths = [
        "fitlife/media_eval.py",
        "fitlife/media_worker.py",
        "fitlife/media_types.py",
        "fitlife/video_counter.py",
        "fitlife/video_signals.py",
        "fitlife/video_jobs.py",
        "fitlife/model.py",
        "fitlife/execution.py",
        "pyproject.toml",
        "config/siliconflow.models.json",
    ]
    protocol = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "split": split,
        "repeats": repeats,
        "rule_version": RULE_VERSION,
        "dataset_sha256": sha(dataset),
        "dataset": manifest,
        "source_sha256": {p: sha(ROOT / p) for p in paths},
        "runtime": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "cpu": platform.processor(),
            "concurrency": 1,
        },
        "evaluation_order": "repeat-major, case order fixed in manifest; no test-driven retuning",
        "truth_access": (
            "scorer only; no author filenames, weights, counts "
            "or nutritional values passed to inference"
        ),
        "side_effect_policy": (
            "only synthetic fitlife_test owners; automated confirmation "
            "is an evaluation script role, not model approval"
        ),
    }
    if task == "video":
        protocol.update(
            method=VERSION,
            config=asdict(CountConfig()),
            actions=EXERCISES,
            pose_model_sha256=sha(os.getenv("FITLIFE_POSE_MODEL_FILE", DEFAULT_MODEL)),
            packages={
                name: version(name) for name in ("mediapipe", "opencv-contrib-python", "numpy")
            },
            limitations=(
                "Given user-selected action, one person/one action, <=180s; "
                f"full decode with <={CountConfig().inference_fps}fps pose inference. "
                "Counts are estimates; no form or safety assessment."
            ),
        )
    else:
        base = Settings()
        model = configured(base, resolve(base, base.vision_model, capability="images"))
        protocol.update(
            model={
                k: getattr(model, k)
                for k in (
                    "provider",
                    "base_url",
                    "api_family",
                    "model",
                    "temperature",
                    "reasoning_effort",
                    "enable_thinking",
                )
            },
            packages={name: version(name) for name in ("openai", "httpx", "sqlalchemy")},
            prompt=prompt("meal_photo"),
            limits={
                "request_timeout": min(model.request_timeout, 45),
                "model_retries": 1,
                "max_output_tokens": 2048,
                "schema_repairs": 1,
                "job_attempts_in_this_protocol": 1,
            },
            nutritional_truth_note=(
                "Weighed ingredients with USDA-derived labels, not direct calorimetry; "
                "prediction must cover all returned items and item omission "
                "is not semantically graded."
            ),
        )
    return protocol


def video_case(case, folder, repeat):
    stem = f"{repeat:02}-{case['id']}"
    target = folder / (stem + "-result.json")
    command = [
        sys.executable,
        "-m",
        "fitlife.video_counter",
        "--input",
        str(ROOT / case["file"].replace("\\", "/")),
        "--exercise",
        case["exercise"],
        "--output",
        str(target),
        "--trace",
        str(folder / (stem + "-features.json")),
    ]
    result = {
        "id": case["id"],
        "repeat": repeat,
        "exercise": case["exercise"],
        "truth": case["count"],
        "prediction": None,
    }
    started = time.perf_counter()
    try:
        if sha(ROOT / case["file"].replace("\\", "/")) != case["sha256"]:
            raise ValueError("DATASET_ASSET_CHANGED")
        with (folder / (stem + ".log")).open("w", encoding="utf-8") as log:
            proc = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, timeout=240)
        result["process_exit"] = proc.returncode
        if proc.returncode == 0:
            output = json.loads(target.read_text(encoding="utf-8"))
            result.update(
                prediction=output["repetitions"],
                status=output["status"],
                observed_cycles=output["observed_cycles"],
                full_timeline_processed=output["full_timeline_processed"],
                pose_coverage=output["pose_coverage"],
                limitations=output["limitations"],
                inference_seconds=output["elapsed_seconds"],
            )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    result["wall_seconds"] = time.perf_counter() - started
    return result


def food_case(case, folder, repeat, settings, engine, prices):
    stem = f"{repeat:02}-{case['id']}"
    result = {
        "id": case["id"],
        "repeat": repeat,
        "truth": case["truth"]["calories_kcal"],
        "prediction": None,
        "side_effects_passed": False,
    }
    actors = []
    started = time.perf_counter()
    try:
        path = ROOT / case["image"].replace("\\", "/")
        if sha(path) != case["image_sha256"]:
            raise ValueError("DATASET_ASSET_CHANGED")
        actor, token = issue_token(engine)
        actors.append(actor)
        result["fixture_owner_id"] = str(actor.user_id)
        budget = ExperimentBudget(
            folder / "budget.json", prices, limit="5", tag=stem, response_dir=folder / "responses"
        )
        service = DomainService(engine)
        with TestClient(create_app(settings, engine=engine)) as client:
            asset_response = client.post(
                "/media/uploads?kind=image", headers=headers(token), content=path.read_bytes()
            )
            asset_response.raise_for_status()
            asset = asset_response.json()
            response = client.post(
                f"/media/{asset['asset_id']}/jobs",
                headers=headers(token, uuid4()),
                json={"task": "meal_photo"},
            )
            response.raise_for_status()
            job_id = response.json()["job_id"]
            result["job_id"] = job_id
            asyncio.run(
                run_once(
                    engine,
                    "test",
                    model_factory=budget.factory,
                    job_id=job_id,
                    model_mode="real-public-food-evaluation",
                )
            )
            done = client.get(f"/media/jobs/{job_id}", headers=headers(token)).json()
            save(folder / (stem + "-job.json"), done)
            result.update(
                status=done["status"],
                error_code=done.get("error_code"),
                model_calls=done["model_calls"],
            )
            before = service.lookup(actor, "photo_meal")["records"]
            result["before_confirmation_records"] = len(before)
            assert not before
            if done["status"] != "succeeded":
                result["side_effects_passed"] = True
                return result
            items = done["result"]["items"]
            energies = [i["estimated_energy_kcal"] for i in items]
            result.update(
                prediction=sum(energies)
                if energies and all(v is not None for v in energies)
                else None,
                returned_items=len(items),
                known_items=sum(v is not None for v in energies),
                uncertainty=done["result"]["uncertainty"],
            )
            if items:
                response = client.patch(
                    f"/media/jobs/{job_id}/review",
                    headers=headers(token),
                    json={"version": done["version"], "result": done["result"]},
                )
                response.raise_for_status()
                reviewed = response.json()
                data = {"record_date": str(today()), "meal_type": "午餐", "items": items}
                response = client.post(
                    f"/media/jobs/{job_id}/actions",
                    headers=headers(token, uuid4()),
                    json={"version": reviewed["version"], "data": data},
                )
                response.raise_for_status()
                pending = response.json()
                assert service.lookup(actor, "photo_meal")["records"] == []
                for _ in range(2):
                    response = client.post(
                        f"/actions/{pending['action_id']}/confirm",
                        headers=headers(token),
                        json=confirmation(pending),
                    )
                    response.raise_for_status()
                    assert response.json()["status"] == "applied"
                records = service.lookup(actor, "photo_meal")["records"]
                assert len(records) == 1 and records[0]["values"]["items"] == [
                    {k: v for k, v in i.items() if v is not None} for i in items
                ]
                result["after_repeated_confirmation_records"] = len(records)
                result["stored_values"] = records[0]["values"]
            result["side_effects_passed"] = True
    except Exception as exc:
        result["pipeline_error_type"] = type(exc).__name__
    finally:
        clean(engine, actors)
        result["cleanup"] = "verified"
        result["wall_seconds"] = time.perf_counter() - started
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=["video", "food"], required=True)
    parser.add_argument("--split", choices=["dev", "heldout", "fresh"], required=True)
    parser.add_argument("--manifest", type=Path, help="explicit frozen local dataset manifest")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--live", action="store_true", help="required for paid food inference")
    parser.add_argument("--parent", type=Path, default=ROOT / "artifacts/media-evals-v2")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5 or (args.task == "food" and not args.live):
        parser.error("repeats 1..5; food requires --live")
    dataset = (
        ROOT
        / "data/media-eval-v2"
        / ("repcount" if args.task == "video" else "nutrition5k")
        / "manifest.json"
    )
    dataset = args.manifest or dataset
    manifest = json.loads(dataset.read_text(encoding="utf-8"))
    cases = [c for c in manifest["cases"] if c["split"] == args.split]
    if not cases:
        raise ValueError("NO_CASES_IN_SPLIT")
    protocol = frozen_protocol(args.task, args.split, args.repeats, manifest, dataset)
    folder = args.parent / f"{args.task}-{args.split}-{uuid4()}"
    folder.mkdir(parents=True, exist_ok=False)
    settings = engine = prices = None
    rows = []
    try:
        if args.task == "food":
            from fitlife.siliconflow_check import public_prices

            prices = public_prices({protocol["model"]["model"]})
            protocol["prices"] = prices
            settings = DatabaseSettings(environment="test")
            engine = settings.engine()
            migrate(engine, "test")
        save(folder / "protocol.json", protocol)
        print(json.dumps({"campaign": str(folder), "runs": len(cases) * args.repeats}), flush=True)
        for repeat in range(1, args.repeats + 1):
            for case in cases:
                row = (
                    video_case(case, folder, repeat)
                    if args.task == "video"
                    else food_case(case, folder, repeat, settings, engine, prices)
                )
                rows.append(row)
                with (folder / "runs.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                save(folder / "summary.json", summarize(rows, args.task))
                print(
                    json.dumps(
                        {
                            k: v
                            for k, v in row.items()
                            if k
                            in {
                                "id",
                                "repeat",
                                "prediction",
                                "truth",
                                "status",
                                "wall_seconds",
                                "pipeline_error_type",
                                "error_code",
                            }
                        }
                    ),
                    flush=True,
                )
    finally:
        if engine:
            engine.dispose()
        save(
            folder / "completion.json",
            {
                "expected_runs": len(cases) * args.repeats,
                "completed_runs": len(rows),
                "complete": len(rows) == len(cases) * args.repeats,
            },
        )
    print(str(folder / "summary.json"))


if __name__ == "__main__":
    main()
