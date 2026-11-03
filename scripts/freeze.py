"""冻结源码/合成用例与环境；或为旧评测源码包补全公开静态资源，不读取业务秘密。"""

import argparse
import base64
import hashlib
import json
import platform
import re
import subprocess
import zipfile
from importlib.metadata import distributions
from pathlib import Path
from uuid import uuid4

from fitlife import __version__
from fitlife.model import Settings

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def supplement(report_path, image):
    report_path = report_path.resolve()
    assert report_path.is_relative_to((ROOT / "artifacts/evaluations").resolve())
    if image:
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image)
    source = report_path.parent / "source.zip"
    assert source.is_file()
    code = (
        "import json,base64,pathlib; p=pathlib.Path('/app/fitlife/web');"
        "print(json.dumps({f.name:base64.b64encode(f.read_bytes()).decode() "
        "for f in p.iterdir() if f.suffix in {'.html','.css','.js'}}))"
    )
    if image:
        result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assets = json.loads(result.stdout)
    else:
        assets = {
            p.name: base64.b64encode(p.read_bytes()).decode()
            for p in (ROOT / "fitlife/web").iterdir()
            if p.suffix in {".html", ".css", ".js"}
        }
    target = report_path.parent / "reproduction.zip"
    manifest = {
        "original_source_zip_sha256": sha(source.read_bytes()),
        "asset_image": image,
        "asset_source": "immutable_image" if image else "current_public_assets_not_original_UI",
        "scope": (
            "original code/data preserved; public static files added "
            "for API StaticFiles registration"
        ),
        "assets": {},
    }
    with (
        zipfile.ZipFile(source) as old,
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for item in old.infolist():
            assert not item.filename.startswith("/") and ".." not in Path(item.filename).parts
            archive.writestr(item, old.read(item.filename))
        for name, data in assets.items():
            assert name in {"index.html", "style.css", "app.js", "media.js"}
            path = "fitlife/web/" + name
            assert path not in old.namelist()
            raw = base64.b64decode(data)
            archive.writestr(path, raw)
            manifest["assets"][path] = sha(raw)
    manifest["reproduction_zip_sha256"] = sha(target.read_bytes())
    (report_path.parent / "reproduction-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print("原源码保留，复现补充包：" + str(target))


def freeze(name):
    assert re.fullmatch(r"[a-zA-Z0-9_.-]+", name)
    folder = ROOT / "artifacts/releases" / name
    folder.mkdir(parents=True, exist_ok=False)
    allowed = {
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
    }
    files = []
    for directory in (
        "fitlife",
        "migrations",
        "tests",
        "evals",
        "scripts",
        "docs",
        ".github",
        "config",
    ):
        files += [
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and p.suffix in allowed
            and "__pycache__" not in p.parts
            and (directory != "config" or p.name.endswith(".example.json"))
        ]
    files += [
        ROOT / name
        for name in (
            "README.md",
            "AGENTS.md",
            "STATE.md",
            "HISTORY.md",
            "pyproject.toml",
            "alembic.ini",
            "Dockerfile",
            ".dockerignore",
            ".gitignore",
            "compose.yaml",
            ".env.example",
        )
        if name not in {"AGENTS.md", "STATE.md", "HISTORY.md"} or (ROOT / name).is_file()
    ]
    files = sorted(set(files))
    manifest = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "model_settings": Settings().model_dump(mode="json"),
        "classification": "code_and_synthetic_fixtures_only",
        "files": {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in files},
        "packages": sorted(
            {d.metadata["Name"] + "==" + d.version for d in distributions() if d.metadata["Name"]}
        ),
    }
    with zipfile.ZipFile(folder / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for p in files:
            archive.write(p, str(p.relative_to(ROOT)))
    manifest["source_zip_sha256"] = sha((folder / "source.zip").read_bytes())
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("版本快照：" + str(folder / "manifest.json"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="fitlife-" + __version__ + "-" + uuid4().hex[:8])
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--assets-image")
    parser.add_argument("--assets-project", action="store_true")
    args = parser.parse_args()
    if args.evaluation:
        assert bool(args.assets_image) != args.assets_project
        supplement(args.evaluation, args.assets_image)
    else:
        freeze(args.name)


if __name__ == "__main__":
    main()
