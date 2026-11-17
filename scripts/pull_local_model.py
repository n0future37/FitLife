"""固定版本本地备用模型下载；保留TLS校验，逐段续传并验证完整SHA256。"""

import argparse
import concurrent.futures
import hashlib
import json
import math
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "4b-instruct-2507-q4_K_M"
MANIFEST = "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
WEIGHT = "85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9"
BASE = "https://registry.ollama.ai/v2/library/qwen3/"


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", required=True)
    parser.parse_args()
    models = ROOT / "data/local-models"
    blobs = models / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(BASE + "manifests/" + TAG, timeout=30) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != MANIFEST:
        raise RuntimeError("PINNED_MODEL_MANIFEST_CHANGED")
    manifest = json.loads(raw)
    weight = manifest["layers"][0]
    assert weight["digest"] == "sha256:" + WEIGHT
    final = blobs / ("sha256-" + WEIGHT)
    size = weight["size"]
    parts = models / ".fitlife-instruct-download"
    parts.mkdir(exist_ok=True)
    length = math.ceil(size / 16)

    def segment(index):
        path = parts / f"{index:02}.part"
        end = min(size, (index + 1) * length) - 1
        for attempt in range(3):
            have = path.stat().st_size if path.exists() else 0
            start = index * length + have
            if start > end:
                return index
            try:
                request = urllib.request.Request(
                    BASE + "blobs/sha256:" + WEIGHT,
                    headers={"Range": f"bytes={start}-{end}", "User-Agent": "FitLife-model-setup"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    expected = f"bytes {start}-{end}/{size}"
                    if response.status != 206 or response.headers.get("Content-Range") != expected:
                        raise RuntimeError("MODEL_RANGE_RESPONSE_MISMATCH")
                    with path.open("ab") as output:
                        while block := response.read(1024 * 1024):
                            output.write(block)
                if path.stat().st_size != end - index * length + 1:
                    raise RuntimeError("MODEL_SEGMENT_SIZE_MISMATCH")
                return index
            except Exception:
                if attempt == 2:
                    raise RuntimeError("MODEL_SEGMENT_DOWNLOAD_FAILED") from None
                time.sleep(attempt + 1)

    if not final.exists():
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(segment, index) for index in range(16)]
            for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
                future.result()
                print(f"Verified download segments: {count}/16", flush=True)
        temporary = final.with_suffix(".verified-download")
        with temporary.open("wb") as output:
            for index in range(16):
                with (parts / f"{index:02}.part").open("rb") as source:
                    while block := source.read(1024 * 1024):
                        output.write(block)
        if temporary.stat().st_size != size or checksum(temporary) != WEIGHT:
            raise RuntimeError("MODEL_FULL_HASH_MISMATCH")
        temporary.replace(final)
    if checksum(final) != WEIGHT:
        raise RuntimeError("EXISTING_MODEL_HASH_MISMATCH")
    verified = [{"digest": weight["digest"], "bytes": size}]
    for item in [manifest["config"], *manifest["layers"][1:]]:
        sha = item["digest"].split(":")[1]
        path = blobs / ("sha256-" + sha)
        if not path.exists():
            with urllib.request.urlopen(BASE + "blobs/" + item["digest"], timeout=30) as response:
                data = response.read()
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError("MODEL_METADATA_HASH_MISMATCH")
            path.write_bytes(data)
        if checksum(path) != sha:
            raise RuntimeError("EXISTING_METADATA_HASH_MISMATCH")
        verified.append({"digest": item["digest"], "bytes": item["size"]})
    target = models / "manifests/registry.ollama.ai/library/qwen3" / TAG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    report = {
        "status": "passed",
        "model": "qwen3:" + TAG,
        "manifest_sha256": MANIFEST,
        "source": BASE + "manifests/" + TAG,
        "blobs": verified,
        "tls_verified": True,
    }
    folder = ROOT / "artifacts/local-model-20261103"
    folder.mkdir(exist_ok=True, parents=True)
    (folder / "instruct-download.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for index in range(16):
        path = parts / f"{index:02}.part"
        if path.exists() and path.resolve().is_relative_to(models.resolve()):
            path.unlink()
    if not any(parts.iterdir()):
        parts.rmdir()
    print("Pinned Qwen3 4B Instruct is ready; all hashes verified.", flush=True)


if __name__ == "__main__":
    main()
