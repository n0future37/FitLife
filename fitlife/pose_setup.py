"""Explicit provisioning/check for the versioned local pose model; never on API request."""

import argparse
import hashlib
import json
import urllib.request
from importlib.metadata import PackageNotFoundError, version

from fitlife.video_counter import DEFAULT_MODEL, MODEL_URL

EXPECTED_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"


def main():
    parser = argparse.ArgumentParser(description="Provision/check FitLife local pose model")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    if args.download and not DEFAULT_MODEL.is_file():
        DEFAULT_MODEL.parent.mkdir(parents=True, exist_ok=True)
        temporary = DEFAULT_MODEL.with_suffix(".download")
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
                temporary.write_bytes(response.read(20000000))
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != EXPECTED_SHA256:
                raise ValueError("POSE_MODEL_HASH_MISMATCH")
            temporary.replace(DEFAULT_MODEL)
        finally:
            temporary.unlink(missing_ok=True)
    versions = {}
    for name in ["mediapipe", "opencv-contrib-python", "numpy"]:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    valid = (
        DEFAULT_MODEL.is_file()
        and hashlib.sha256(DEFAULT_MODEL.read_bytes()).hexdigest() == EXPECTED_SHA256
    )
    print(
        json.dumps(
            {
                "model": str(DEFAULT_MODEL),
                "model_hash_valid": valid,
                "versions": versions,
                "url": MODEL_URL,
            }
        )
    )
    if not valid or any(value is None for value in versions.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
