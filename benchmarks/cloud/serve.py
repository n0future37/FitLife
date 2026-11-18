"""Fixed-model benchmark server with the same HTTP transport as the real server."""

import argparse

import uvicorn

from fitlife.http_protocol import NoDelayH11Protocol


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--keepalive", type=int, default=5)
    args = parser.parse_args()
    uvicorn.run(
        "benchmarks.cloud.runtime:create_benchmark_app",
        factory=True,
        http=NoDelayH11Protocol,
        host="0.0.0.0",
        port=8100,
        workers=args.workers,
        timeout_keep_alive=args.keepalive,
        access_log=False,
    )


if __name__ == "__main__":
    main()
