#!/usr/bin/env python3
"""Activate only after a complete custom release is available and verified."""
import argparse
import json
import subprocess
from pathlib import Path

from publisher import run, save_json, validate_repository

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    validate_repository(args.repository)
    path = Path("/etc/geodomdesk-builder/config.json")
    config = json.loads(path.read_text())
    config.update(enabled=True, repository=args.repository, activate_portal=False)
    # Build and verify first; failed/missing Releases leave the old timer and links intact.
    run(config)
    subprocess.run(["systemctl", "disable", "--now", "geodom-rustdesk-builder.timer"], check=True)
    subprocess.run(["systemctl", "stop", "geodom-rustdesk-builder.service"], check=True)
    config["activate_portal"] = True
    save_json(path, config, 0o600)
    run(config)
    print("GeodomDesk download and update channel activated.")
