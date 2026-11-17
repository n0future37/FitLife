"""Retrieve only the frozen public evaluation subset; never download the complete dataset."""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["nutrition5k", "repcount"], required=True)
    parser.add_argument("--manifest", type=Path, help="explicit frozen subset manifest")
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    template = args.manifest or ROOT / "evals/media-v2" / (args.dataset + ".json")
    manifest = json.loads(template.read_text(encoding="utf-8"))
    datasets = {}
    for case in manifest["cases"]:
        image = args.dataset == "nutrition5k"
        path = ROOT / case["image" if image else "file"].replace("\\", "/")
        expected = case["image_sha256" if image else "sha256"]
        if path.is_file():
            if digest(path.read_bytes()) != expected:
                raise ValueError("EXISTING_ASSET_HASH_MISMATCH")
            print(case["id"], "verified", flush=True)
            continue
        if not args.download:
            raise ValueError("ASSET_MISSING_USE_EXPLICIT_DOWNLOAD")
        if image:
            with urllib.request.urlopen(case["url"], timeout=45) as response:
                blob = response.read(8 * 1024 * 1024 + 1)
        else:
            import lance

            split = case["mirror_split"]
            if split not in datasets:
                dataset = lance.dataset(
                    "hf://datasets/lmms-lab-eval/repcounta-lance/data/" + split + ".lance",
                    version=1,
                    storage_options={"timeout": "45s", "client_max_retries": "2"},
                )
                ids = dataset.to_table(columns=["video_id"]).column("video_id").to_pylist()
                datasets[split] = dataset, ids
            dataset, ids = datasets[split]
            index = ids.index(case["video_id"])
            blob = dataset.read_blobs(
                "video_blob", indices=[index], io_buffer_size=8 * 1024 * 1024
            )[0][1]
        if len(blob) > 8 * 1024 * 1024 or digest(blob) != expected:
            raise ValueError("DOWNLOADED_ASSET_HASH_MISMATCH")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".download")
        temporary.write_bytes(blob)
        temporary.replace(path)
        print(case["id"], "downloaded_and_verified", flush=True)
    first = manifest["cases"][0]["image" if args.dataset == "nutrition5k" else "file"]
    target = (ROOT / first.replace("\\", "/")).parent / "manifest.json"
    if target.exists() and target.read_bytes() != template.read_bytes():
        raise ValueError("EXISTING_MANIFEST_DIFFERENT")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(template.read_bytes())
    print("Frozen subset verified; public research data retained locally, not redistributed.")


if __name__ == "__main__":
    main()
