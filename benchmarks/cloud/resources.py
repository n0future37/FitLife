"""Sample host/container resource use on the dedicated Linux benchmark host."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def container_stats(ids):
    try:
        return command("docker", "stats", "--no-stream", "--format", "{{json .}}", *ids)
    except subprocess.CalledProcessError:
        alive = command(
            "docker", "ps", "-q", "--filter", "label=com.docker.compose.project=fitlife-benchmark"
        ).split()
        if set(ids) - set(alive):
            # Compose --rm can remove a load container between listing and sampling.
            # Skip that sample; do not fabricate CPU data or mask an unrelated failure.
            return None
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args()
    cpu = Path("/proc/cpuinfo").read_text()
    metadata = {
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "cpu_model": next(
            x.split(":", 1)[1].strip() for x in cpu.splitlines() if x.startswith("model name")
        ),
        "memory": Path("/proc/meminfo").read_text(),
        "disk": dict(zip(("total", "used", "free"), shutil.disk_usage("/"))),
        "docker": command("docker", "version", "--format", "{{.Server.Version}}"),
        "compose": command("docker", "compose", "version", "--short"),
        "containers": [],
    }
    ids = command(
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=fitlife-benchmark",
    ).split()
    for container in ids:
        info = json.loads(command("docker", "inspect", container))[0]
        metadata["containers"].append(
            {
                "name": info["Name"],
                "image": info["Config"]["Image"],
                "image_id": info["Image"],
                "memory_limit_bytes": info["HostConfig"]["Memory"],
                "cpu_limit_nanocpus": info["HostConfig"]["NanoCpus"],
                "command": info["Config"]["Cmd"],
                "published_ports": info["HostConfig"]["PortBindings"],
            }
        )
    args.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2))
    with args.output.open("w") as output:
        while not args.stop_file.exists():
            started = time.time()
            ids = command(
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=fitlife-benchmark",
            ).split()
            stats = container_stats(ids) if ids else ""
            if stats is None:
                output.write(
                    json.dumps({"timestamp_epoch": started, "event": "container_removed"}) + "\n"
                )
                output.flush()
                continue
            sample = {
                "timestamp_epoch": started,
                "sampling_seconds": round(time.time() - started, 3),
                "host_cpu_counters": Path("/proc/stat").read_text().splitlines()[0],
                "host_load": Path("/proc/loadavg").read_text().split()[:3],
                "host_mem_available_kib": next(
                    int(x.split()[1])
                    for x in Path("/proc/meminfo").read_text().splitlines()
                    if x.startswith("MemAvailable:")
                ),
                "containers": [json.loads(line) for line in stats.splitlines()],
            }
            output.write(json.dumps(sample) + "\n")
            output.flush()
            time.sleep(1)


if __name__ == "__main__":
    main()
