"""Scoped media-v2 container validation, new image tags, current-user secrets in memory only."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values

from fitlife.credentials import database_password

ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--parent", type=Path, default=ROOT / "artifacts/media-container-checks")
args = parser.parse_args()
OUT = (args.parent / str(uuid4())).resolve()
OUT.mkdir(parents=True, exist_ok=False)
env = os.environ.copy()
env["FITLIFE_DB_PASSWORD"] = database_password("test", interactive=False)
env["FITLIFE_REDIS_PASSWORD"] = dotenv_values("E:/Work/Config/Redis/.env")["REDIS_PASSWORD"]
secret_values = [env["FITLIFE_DB_PASSWORD"], env["FITLIFE_REDIS_PASSWORD"]]
report = {"status": "failed", "checks": []}
name = "fitlife-media-v2-" + uuid4().hex[:12]
common = {
    "image": "fitlife:test-media-v2",
    "environment": {
        "FITLIFE_DB_ENVIRONMENT": "test",
        "FITLIFE_DB_HOST": "host.docker.internal",
        "FITLIFE_DB_PASSWORD_FILE": "/run/secrets/mysql_password",
        "FITLIFE_REDIS_HOST": "host.docker.internal",
        "FITLIFE_REDIS_PASSWORD_FILE": "/run/secrets/redis_password",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    },
    "secrets": ["mysql_password", "redis_password"],
    "volumes": [
        OUT.as_posix() + ":/app/artifacts",
        (ROOT / "data/pose-models").as_posix() + ":/app/data/pose-models:ro",
        (ROOT / "data/media-eval-v2/repcount").as_posix() + ":/app/data/public-eval:ro",
    ],
    "tmpfs": ["/tmp:rw,nosuid,size=128m"],
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
}
config = {
    "name": name,
    "services": {"test": common},
    "secrets": {
        "mysql_password": {"environment": "FITLIFE_DB_PASSWORD"},
        "redis_password": {"environment": "FITLIFE_REDIS_PASSWORD"},
    },
}
conf = OUT / "compose.json"
conf.write_text(json.dumps(config, indent=2), encoding="utf-8")


def run(args, log, timeout=900):
    print(log, "started", flush=True)
    with (OUT / log).open("w", encoding="utf-8") as file:
        proc = subprocess.run(args, cwd=ROOT, env=env, stdout=file, stderr=file, timeout=timeout)
    content = (OUT / log).read_text(encoding="utf-8", errors="replace")
    for key in secret_values:
        content = content.replace(key, "[redacted]")
    (OUT / log).write_text(content, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError(log + "_FAILED")
    print(log, "passed", flush=True)


prefix = ["docker", "compose", "-f", str(conf)]
try:
    run(
        ["docker", "build", "--target", "test", "-t", "fitlife:test-media-v2", "."],
        "build-test.log",
        1200,
    )
    run(
        ["docker", "build", "--target", "runtime", "-t", "fitlife:media-v2", "."],
        "build-runtime.log",
        300,
    )
    report["checks"].append("media_runtime_and_test_images_built")
    run(
        prefix
        + [
            "run",
            "--rm",
            "-T",
            "test",
            "python",
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "--integration",
            "-q",
            "--junitxml=/app/artifacts/linux.xml",
        ],
        "linux-tests.log",
        300,
    )
    report["checks"].append("linux_mysql_fixed_regression_with_native_empty_scene_counter")
    run(
        prefix
        + [
            "run",
            "--rm",
            "-T",
            "test",
            "python",
            "-m",
            "fitlife.video_counter",
            "--input",
            "/app/data/public-eval/stu9_53.mp4",
            "--exercise",
            "push_up",
            "--output",
            "/app/artifacts/count.json",
            "--trace",
            "/app/artifacts/features.json",
        ],
        "native-count.log",
        240,
    )
    result = json.loads((OUT / "count.json").read_text(encoding="utf-8"))
    assert result["full_timeline_processed"] and result["repetitions"] == 39
    report["checks"].append("linux_real_development_video_count_39_matches_reference")
    run(
        prefix
        + [
            "run",
            "--rm",
            "-T",
            "test",
            "python",
            "-c",
            "import os,json,importlib.metadata as m; print(json.dumps({"
            '"uid":os.getuid(),"packages":{n:m.version(n) for n in ["me'
            'diapipe","numpy","opencv-contrib-python"]}})); assert os.g'
            "etuid()==10001",
        ],
        "runtime-versions.log",
        60,
    )
    for tag in ["fitlife:test-media-v2", "fitlife:media-v2"]:
        output = subprocess.check_output(
            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], env=env, text=True
        )
        report.setdefault("images", {})[tag] = output.strip()
    report["status"] = "passed"
except Exception as exc:
    report["error_type"] = type(exc).__name__
    if isinstance(exc, RuntimeError):
        report["error_code"] = str(exc)
finally:
    try:
        run(prefix + ["down", "--remove-orphans"], "cleanup.log", 60)
        report["cleanup"] = "verified"
    except Exception:
        report["cleanup"] = "failed"
        report["status"] = "failed"
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report), flush=True)
print(str(OUT / "report.json"), flush=True)
sys.exit(0 if report["status"] == "passed" else 1)
