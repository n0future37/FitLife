"""迁移开发库并短暂启动独立本机服务；不签发令牌、不写业务事实、不请求模型。"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text

from fitlife.database import DatabaseSettings
from fitlife.manage import migrate

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "artifacts/dev-checks" / str(uuid4())
    folder.mkdir(parents=True)
    report = {"status": "failed", "model_calls": 0, "business_writes": 0}
    engine = DatabaseSettings(environment="dev").engine()
    process = None
    try:
        migrate(engine, "dev")
        with engine.connect() as connection:
            report["migration"] = connection.execute(
                text("SELECT version_num FROM fl_alembic_version")
            ).scalar_one()
            # 验收启动器不应处理用户已有的媒体队列。
            queued = connection.execute(
                text("SELECT COUNT(*) FROM fl_media_jobs WHERE status IN ('queued', 'running')")
            ).scalar_one()
            if queued:
                raise RuntimeError("EXISTING_MEDIA_WORK_REQUIRES_SEPARATE_STARTUP")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with (folder / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fitlife.server",
                    "--environment",
                    "dev",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3
            ) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        response = client.get("/ready")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("STARTUP_FAILED")
                assert response.json() == {"status": "ready", "shared_capacity": True}
                assert client.get("/profile").status_code == 401
                assert client.get("/").status_code == 200
                for filename in ("app.js", "media.js", "style.css"):
                    asset = client.get("/assets/" + filename)
                    assert asset.status_code == 200
                    assert (
                        hashlib.sha256(asset.content).digest()
                        == hashlib.sha256((ROOT / "fitlife/web" / filename).read_bytes()).digest()
                    )
            report.update(
                status="passed",
                checks=["dev_migration", "mysql_redis_ready", "private_401", "static_hashes"],
            )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            report["error_code"] = str(exc)
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            report["own_server_stopped"] = process.poll() is not None
        engine.dispose()
        (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Development startup: {report['status']}; {folder / 'report.json'}")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
