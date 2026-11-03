"""本机容器验收：安全配置仅传给Compose secrets，专属临时项目最后关闭。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from fitlife.credentials import database_password

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup-only", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "artifacts/container-checks" / str(uuid4())
    folder.mkdir(parents=True)
    name = "fitlife-check-" + uuid4().hex[:12]
    environment = os.environ.copy()
    environment["FITLIFE_DB_PASSWORD"] = database_password("test", interactive=False)
    redis_secret = environment.get("FITLIFE_REDIS_PASSWORD")
    if not redis_secret:
        path = environment.get("FITLIFE_REDIS_CREDENTIALS_FILE")
        if not path:
            raise SystemExit("请通过本机环境指定既有Redis安全配置路径。")
        redis_secret = dotenv_values(path).get("REDIS_PASSWORD")
    if not redis_secret:
        raise SystemExit("REDIS_PASSWORD_REQUIRED")
    environment["FITLIFE_REDIS_PASSWORD"] = redis_secret
    secret_values = [environment["FITLIFE_DB_PASSWORD"], redis_secret]
    common = {
        "environment": {
            "FITLIFE_DB_ENVIRONMENT": "test",
            "FITLIFE_DB_HOST": "host.docker.internal",
            "FITLIFE_DB_PASSWORD_FILE": "/run/secrets/mysql_password",
            "FITLIFE_REDIS_HOST": "host.docker.internal",
            "FITLIFE_REDIS_PASSWORD_FILE": "/run/secrets/redis_password",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        "secrets": ["mysql_password", "redis_password"],
        "volumes": [folder.as_posix() + ":/app/artifacts"],
        "tmpfs": ["/tmp:rw,nosuid,size=64m"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
    }
    compose = {
        "name": name,
        "services": {
            "test": {**common, "image": "fitlife:test-0.6.0"},
            "app": {
                **common,
                "image": "fitlife:0.6.0",
                "init": True,
                "ports": ["127.0.0.1:0:8100"],
                "command": [
                    "python",
                    "-m",
                    "fitlife.server",
                    "--environment",
                    "test",
                    "--host",
                    "0.0.0.0",
                ],
            },
        },
        "secrets": {
            "mysql_password": {"environment": "FITLIFE_DB_PASSWORD"},
            "redis_password": {"environment": "FITLIFE_REDIS_PASSWORD"},
        },
    }
    config = folder / "compose.json"
    config.write_text(json.dumps(compose, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"status": "failed", "checks": [], "project": name}
    prefix = ["docker", "compose", "-f", str(config)]

    def run(arguments, log_name):
        result = subprocess.run(
            prefix + arguments,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        output = result.stdout + result.stderr
        for value in secret_values:
            output = output.replace(value, "[redacted]")
        (folder / log_name).write_text(output, encoding="utf-8")
        if result.returncode:
            raise RuntimeError("CONTAINER_COMMAND_FAILED")
        return result.stdout.strip()

    try:
        if not args.startup_only:
            run(
                [
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
                "tests.log",
            )
            report["checks"].append("linux_mysql_redis_mcp_regressions")
            run(
                ["run", "--rm", "-T", "test", "python", "-m", "fitlife.process_check"],
                "process-check.log",
            )
            report["checks"].append("linux_real_process_recovery_and_dual_instances")
        run(["up", "--detach", "app"], "start.log")
        address = run(["port", "app", "8100"], "port.log")
        if not re.fullmatch(r"127\.0\.0\.1:\d+", address):
            raise RuntimeError("UNEXPECTED_CONTAINER_BIND")
        with httpx.Client(base_url="http://" + address, timeout=5, trust_env=False) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    response = client.get("/ready")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("CONTAINER_START_TIMEOUT")
            assert response.json() == {"status": "ready", "shared_capacity": True}
            assert client.get("/profile").status_code == 401
            response = client.get("/")
            assert response.status_code == 200 and "FitLife" in response.text
            assert response.headers["cache-control"] == "no-store"
            for filename in ("app.js", "media.js", "style.css"):
                asset = client.get("/assets/" + filename)
                assert asset.status_code == 200
                assert (
                    hashlib.sha256(asset.content).digest()
                    == hashlib.sha256((ROOT / "fitlife/web" / filename).read_bytes()).digest()
                )
        assert (
            run(["exec", "-T", "app", "python", "-c", "import os; print(os.getuid())"], "uid.log")
            == "10001"
        )
        report["checks"] += [
            "container_ready_mysql_and_redis",
            "unauthenticated_private_api_blocked",
            "static_interface",
            "non_root_process",
        ]
        run(["exec", "-T", "app", "ffmpeg", "-version"], "ffmpeg.log")
        run(["exec", "-T", "app", "ffprobe", "-version"], "ffprobe.log")
        report["checks"].append("media_decoders_available")
        report["status"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            report["error_code"] = str(exc)
    finally:
        try:
            run(["down", "--remove-orphans"], "cleanup.log")
            report["container_cleanup"] = "verified"
        except Exception:
            report["container_cleanup"] = "failed"
            report["status"] = "failed"
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"Linux容器验收：{report['status']}；{folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
