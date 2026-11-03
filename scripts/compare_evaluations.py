"""仅比较同源代码/数据/模型条件、不同工具提示策略的冻结报告。"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("variant", type=Path)
    args = parser.parse_args()
    reports = []
    for path in (args.baseline, args.variant):
        path = path.resolve()
        assert path.is_relative_to((ROOT / "artifacts/evaluations").resolve())
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    a, b = reports
    for key in (
        "dataset_sha256",
        "source_sha256",
        "packages",
        "python",
        "repeat",
        "n_scripts",
        "split",
    ):
        assert a[key] == b[key], key
    assert a["mode"] == b["mode"] == "live"
    differences = {
        k: [a["settings"].get(k), b["settings"].get(k)]
        for k in a["settings"]
        if a["settings"].get(k) != b["settings"].get(k)
    }
    assert differences == {"tool_policy": ["baseline", "direct_recording"]}
    assert [(r["id"], r["repeat"]) for r in a["cases"]] == [
        (r["id"], r["repeat"]) for r in b["cases"]
    ]
    summary = {
        "settings_differences": differences,
        "dataset_sha256": a["dataset_sha256"],
        "arms": [],
    }
    for path, report in zip((args.baseline, args.variant), reports, strict=True):
        summary["arms"].append(
            {
                "report": str(path),
                "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "policy": report["settings"]["tool_policy"],
                "n_scripts": report["n_scripts"],
                "rule_passed": report["rule_passed"],
                "model_calls": report["model_calls"],
                "tool_attempts": sum(r["tool_calls"] for r in report["cases"]),
                "latency_seconds": report["latency_seconds"],
                "cost": None,
            }
        )
    summary["interpretation"] = (
        "单一开发场景各2次探索性对照；并行同时间段运行，模型随机性和API超时仍可影响结果。"
        "不能据此归因稳定质量或速度提升，不能代替全开发/保留评测；默认保持baseline。"
    )
    folder = ROOT / "artifacts/experiments" / str(uuid4())
    folder.mkdir(parents=True)
    (folder / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("同源条件核对通过；对照报告：" + str(folder / "comparison.json"))


if __name__ == "__main__":
    main()
