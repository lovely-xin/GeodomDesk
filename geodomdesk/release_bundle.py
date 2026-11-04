#!/usr/bin/env python3
"""Write the release contract consumed by the cloud installer/update publisher."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

from channel import TARGETS, file_name, make_release_id, sha256, stable_json, version_tuple
from plan_upstream import customization_hash


def bundle(version, repository, windows, ubuntu, android, source_archive, output, build_config):
    version_tuple(version)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected the GitHub owner/repository")
    cfg = json.loads(build_config.read_text())
    if cfg["app"] != "GeodomDesk" or cfg["version"] != version:
        raise ValueError("Build must use the prepared GeodomDesk source/version")
    release_id = make_release_id(version, cfg["build_number"])
    if cfg["release_id"] != release_id:
        raise ValueError("Build revision mismatch")
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for payload, (platform, arch, fmt) in zip([windows, ubuntu, android], TARGETS):
        name = file_name(release_id, platform, fmt)
        if not 1000000 <= payload.stat().st_size <= 512 * 1024 * 1024:
            raise ValueError("Unexpected build artifact size")
        destination = output / name
        if destination.resolve() != payload.resolve():
            shutil.copyfile(payload, destination)
        artifacts.append(
            {
                "platform": platform,
                "arch": arch,
                "format": fmt,
                "file_name": name,
                "size": destination.stat().st_size,
                "sha256": sha256(destination),
            }
        )
    source_name = f"geodomdesk-{release_id}-source.tar.gz"
    destination = output / source_name
    if source_archive.resolve() != destination.resolve():
        shutil.copyfile(source_archive, destination)
    descriptor = {
        **cfg,
        "customization_commit": os.environ.get("GITHUB_SHA", ""),
        "customization_hash": customization_hash(),
        "schema_version": 1,
        "repository": repository,
        "artifacts": artifacts,
        "source": {
            "file_name": source_name,
            "size": destination.stat().st_size,
            "sha256": sha256(destination),
        },
    }
    (output / "geodomdesk-build.json").write_bytes(stable_json(descriptor))
    print("Release assets ready in " + str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ["version", "repository"]:
        parser.add_argument("--" + arg, required=True)
    for arg in ["windows", "ubuntu", "android", "source-archive", "output", "build-config"]:
        parser.add_argument("--" + arg, required=True, type=Path)
    args = parser.parse_args()
    bundle(
        args.version,
        args.repository,
        args.windows,
        args.ubuntu,
        args.android,
        args.source_archive,
        args.output,
        args.build_config,
    )
