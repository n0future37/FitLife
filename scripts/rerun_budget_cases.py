"""Prepare an isolated supplemental budget and preserve the original experiment."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fitlife.business import today  # noqa: E402
from fitlife.experiment_budget import ExperimentBudget  # noqa: E402
from fitlife.siliconflow_check import public_prices  # noqa: E402


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(origin):
    config = read(origin / "protocol.json")
    assert str(today()) == config["anchor_date"], "REQUIRES_ORIGINAL_ANCHOR_DATE"
    audit = read(origin / "operational-censoring.json")
    assert len(audit["excluded_resource_ids"]) == 5
    bad = set(audit["excluded_resource_ids"])
    pairs = {(c["case"], c["repeat"]) for c in config["schedule"] if c["id"] in bad}
    cells = [c for c in config["schedule"] if (c["case"], c["repeat"]) in pairs]
    assert len(cells) == 6 and {c["repeat"] for c in cells} == {3}
    cases = [c for c in config["resolved_cases"] if c["id"] in {x["case"] for x in cells}]
    assert len(cases) == 2
    expected_change = (
        '            config["prices"],\n',
        '            config["prices"],\n            limit=config.get("budget_limit_cny", "5"),\n',
    )
    with zipfile.ZipFile(origin / "source.zip") as archive:
        old = archive.read("fitlife/experiments.py").decode("utf-8").replace("\r\n", "\n")
    assert (ROOT / "fitlife/experiments.py").read_text(encoding="utf-8") == old.replace(
        *expected_change
    )
    for name, digest in config["source_sha256"].items():
        if name != "fitlife/experiments.py":
            assert sha(ROOT / name) == digest, "MODEL_OR_TOOL_SOURCE_CHANGED"
    prices = public_prices(set(config["prices"]))
    assert prices == config["prices"], "PRICE_SNAPSHOT_CHANGED"
    folder = origin.parent / str(uuid4())
    folder.mkdir()
    new = {
        **config,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schedule": cells,
        "resolved_cases": cases,
        "budget_limit_cny": "2",
        "budget_file": str(folder / "budget.json"),
    }
    new["source_sha256"] = {name: sha(ROOT / name) for name in config["source_sha256"]}
    new["supplement"] = {
        "origin": str(origin),
        "origin_protocol_sha256": sha(origin / "protocol.json"),
        "origin_budget_sha256": sha(Path(config["budget_file"])),
        "censored_ids": sorted(bad),
        "rerun_ids": [c["id"] for c in cells],
        "rerun_prior_success": sorted({c["id"] for c in cells} - bad),
        "reason": "Rerun full case/repeat sets after study-budget interruptions.",
        "changed_source_files": ["fitlife/experiments.py"],
        "source_change": "Read protocol budget limit; agent code unchanged.",
        "additional_limit_cny": "2",
        "cumulative_limit_cny": "7",
    }
    save(folder / "protocol.json", new)
    shutil.copy2(origin / "dataset.jsonl", folder / "dataset.jsonl")
    with (
        zipfile.ZipFile(origin / "source.zip") as source,
        zipfile.ZipFile(folder / "source.zip", "w", zipfile.ZIP_DEFLATED) as dest,
    ):
        for entry in source.infolist():
            data = (
                (ROOT / entry.filename).read_bytes()
                if entry.filename == "fitlife/experiments.py"
                else source.read(entry.filename)
            )
            dest.writestr(entry.filename, data)
        dest.write(Path(__file__), "support/rerun_budget_cases.py")
    ExperimentBudget(folder / "budget.json", prices, limit="2")
    print("SUPPLEMENT " + str(folder))
    return folder


def finish(folder):
    config = read(folder / "protocol.json")
    origin = Path(config["supplement"]["origin"])
    old = read(origin / "protocol.json")
    assert sha(Path(old["budget_file"])) == config["supplement"]["origin_budget_sha256"]
    rows = [read(folder / "episodes" / c["id"] / "result.json") for c in config["schedule"]]
    assert len(rows) == 6 and all(r.get("cleanup_verified") for r in rows)
    assert not any(
        e.get("error_type") == "ValueError" for r in rows for e in r.get("model_events", [])
    ), "STUDY_BUDGET_INTERRUPTED_AGAIN"
    combined = folder / "combined"
    combined.mkdir()
    selected = {r["id"] for r in rows}
    provenance = {}
    for cell in old["schedule"]:
        source = folder if cell["id"] in selected else origin
        shutil.copytree(source / "episodes" / cell["id"], combined / "episodes" / cell["id"])
        provenance[cell["id"]] = str(source)
    merged = {
        **old,
        "mode": "combined-live-analysis-only",
        "analysis_only": True,
        "episode_provenance": provenance,
        "source_sets": {"original": old["source_sha256"], "supplement": config["source_sha256"]},
        "supplement": config["supplement"],
        "budget_files": [old["budget_file"], config["budget_file"]],
    }
    save(combined / "protocol.json", merged)
    shutil.copy2(origin / "dataset.jsonl", combined / "dataset.jsonl")
    shutil.copy2(origin / "source.zip", combined / "source.zip")
    shutil.copy2(folder / "source.zip", combined / "supplement-source.zip")
    from fitlife.experiments import summary

    summary(combined)
    for target in (folder, combined):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/analyze_experiments.py"), str(target)], check=True
        )
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/rescore_experiments.py"), str(target)], check=True
        )
    assert len(read(combined / "summary.json")["results"]) == 84
    save(
        folder / "merge-provenance.json",
        {
            "original_runs": 84,
            "supplement_runs": 6,
            "total_actual_runs": 90,
            "combined_positions": 84,
            "replacement_ids": sorted(selected),
            "rule": "Replace all six predefined positions; preserve originals.",
            "episode_provenance": provenance,
        },
    )
    print("COMBINED " + str(combined))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", type=Path)
    parser.add_argument("--finish", action="store_true")
    args = parser.parse_args()
    if args.finish:
        finish(args.origin.resolve())
    else:
        prepare(args.origin.resolve())
