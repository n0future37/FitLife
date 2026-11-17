"""生成可审阅的源码上传副本；不创建Git提交、不读取数据库数据或上传。"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("fitlife", "migrations", "tests", "scripts", "evals", "docs", ".github", "config")
ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "alembic.ini",
    "Dockerfile",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
    ".env.example",
)
SUFFIXES = {
    ".py",
    ".md",
    ".html",
    ".css",
    ".js",
    ".jsonl",
    ".json",
    ".toml",
    ".ini",
    ".yaml",
    ".yml",
    ".mako",
    ".ps1",
    ".png",
}


PUBLIC_CONFIGS = {
    "openrouter.free.models.json",
    "openrouter.free.routing.json",
    "siliconflow.models.json",
}
PUBLIC_IMAGES = {
    "chat-confirmation.png",
    "activity-mobile.png",
    "media-review.png",
    "plan-adjustment.png",
}


def files():
    paths = [ROOT / name for name in ROOT_FILES]
    for directory in DIRECTORIES:
        paths.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and p.suffix in SUFFIXES
            and "__pycache__" not in p.parts
            and p.name not in {"AGENTS.md", "STATE.md", "HISTORY.md"}
            and p.name not in {"learning-roadmap.md", "project-understanding.md"}
            and (
                p.suffix != ".png" or (p.parent == ROOT / "docs/images" and p.name in PUBLIC_IMAGES)
            )
            and (
                directory != "config"
                or p.name.endswith(".example.json")
                or p.name in PUBLIC_CONFIGS
            )
        )
    return sorted(set(paths))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="publish-v06")
    args = parser.parse_args()
    if not args.name.replace("-", "").isalnum():
        raise SystemExit("INVALID_EXPORT_NAME")
    folder = ROOT / "artifacts" / args.name
    folder.mkdir(exist_ok=False)
    destination = folder / "source"
    destination.mkdir()
    records = {}
    keys = [
        value
        for name in ("MODELFLARE_API_KEY", "SILI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER")
        if (value := os.getenv(name)) and len(value) >= 12
    ]
    for path in files():
        assert not path.is_symlink() and path.resolve().is_relative_to(ROOT.resolve())
        relative = path.relative_to(ROOT)
        assert relative.parts[0] not in {"artifacts", "data", ".idea", ".git"}
        raw = path.read_bytes()
        if any(key.encode() in raw for key in keys):
            raise SystemExit("MODEL_KEY_FOUND_IN_SOURCE: " + relative.as_posix())
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        records[relative.as_posix()] = dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    manifest = dict(
        target_repository="https://github.com/n0future37/FitLife",
        target_branch="main",
        status="review_ready_not_uploaded",
        file_count=len(records),
        total_bytes=sum(r["bytes"] for r in records.values()),
        excluded=[
            "business data",
            "artifacts",
            "credentials and .env",
            "IDE/main.py",
            "local STATE/AGENTS/HISTORY and personal learning roadmap",
            "raw media and model weights",
        ],
        known_model_key_absent=True,
        files=records,
    )
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Review-ready source: {len(records)} files, {manifest['total_bytes']} bytes; "
        f"{folder / 'manifest.json'}"
    )


if __name__ == "__main__":
    main()
