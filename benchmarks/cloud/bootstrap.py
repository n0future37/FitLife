"""Create new benchmark-only secrets on the Linux deployment host, never print them."""

import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if os.name == "nt":
        raise SystemExit("Run on the dedicated Linux host; Windows uses different ACL rules.")
    folder = args.directory.expanduser().resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise SystemExit("Secret directory must be empty; existing secrets were not modified.")
    folder.chmod(0o700)
    for name in ("root_password", "db_password", "redis_password"):
        file = folder / name
        file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        file.chmod(0o444)  # Required by bind-mounted Compose secrets; parent stays 0700.
    redis_config = folder / "redis_config"
    redis_config.write_text(
        'bind 0.0.0.0\nprotected-mode yes\nappendonly no\nsave ""\n'
        "maxmemory 64mb\nmaxmemory-policy noeviction\nrequirepass "
        + (folder / "redis_password").read_text(encoding="utf-8").strip()
        + "\n",
        encoding="utf-8",
    )
    redis_config.chmod(0o444)
    print("Private benchmark credentials created; no secrets printed.")


if __name__ == "__main__":
    main()
