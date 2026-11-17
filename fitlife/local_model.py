"""FitLife专用本地模型入口；serve前台运行，Ctrl+C关闭本次启动的进程。"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:11435"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["serve", "status", "warmup"], default="status", nargs="?"
    )
    parser.add_argument(
        "--executable", type=Path, default=Path("E:/Work/Apps/Ollama/v0.40.13/ollama.exe")
    )
    args = parser.parse_args()
    if args.action != "serve":
        with httpx.Client(timeout=90, trust_env=False) as client:
            if args.action == "status":
                response = client.get(ENDPOINT + "/api/tags")
            else:
                response = client.post(
                    ENDPOINT + "/api/chat",
                    json={
                        "model": "qwen3:4b-instruct-2507-q4_K_M",
                        "messages": [{"role": "user", "content": "只回复：准备就绪"}],
                        "stream": False,
                        "think": False,
                        "options": {"num_ctx": 16384, "num_predict": 32},
                        "keep_alive": "10m",
                    },
                )
            response.raise_for_status()
            result = response.json()
            if args.action == "warmup" and (
                result.get("done_reason") != "stop"
                or result.get("message", {}).get("content", "").strip() != "准备就绪"
            ):
                raise RuntimeError("LOCAL_WARMUP_RESPONSE_NOT_READY")
            print(json.dumps(result, ensure_ascii=False))
        return
    if not args.executable.is_file():
        raise SystemExit("LOCAL_RUNTIME_NOT_INSTALLED: see docs/model-resilience.md")
    weights = ROOT / "data/local-models"
    weights.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        OLLAMA_HOST="127.0.0.1:11435",
        OLLAMA_MODELS=str(weights),
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_MAX_QUEUE="2",
        OLLAMA_CONTEXT_LENGTH="16384",
        OLLAMA_KEEP_ALIVE="10m",
        OLLAMA_NO_CLOUD="true",
        OLLAMA_DEBUG="false",
        OLLAMA_DEBUG_LOG_REQUESTS="false",
    )
    process = subprocess.Popen(
        [str(args.executable.resolve()), "serve"],
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
