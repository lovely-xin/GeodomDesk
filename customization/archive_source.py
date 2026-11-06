#!/usr/bin/env python3
"""Archive tracked working-tree source, including patched submodules, for a release."""
import argparse
import json
import subprocess
import tarfile
from pathlib import Path

def archive(source, customization, output):
    source = source.resolve()
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--recurse-submodules"], cwd=source
    ).split(b"\0")
    paths = {Path(name.decode()) for name in names if name}
    build_config = Path("geodomdesk-build-config.json")
    generated = json.loads((source / build_config).read_text(encoding="utf-8"))[
        "generated_source_files"
    ]
    for name in map(Path, generated):
        path = source / name
        if name.is_absolute() or not path.resolve().is_relative_to(source) or not path.is_file():
            raise ValueError("Missing or invalid generated source: " + str(name))
        paths.add(name)
    paths.update(
        Path(name)
        for name in [
            "src/bridge_generated.rs",
            "src/bridge_generated.io.rs",
            "flutter/lib/generated_bridge.dart",
            "flutter/lib/generated_bridge.freezed.dart",
            build_config,
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as tar:
        for name in sorted(paths):
            path = source / name
            if not path.resolve().is_relative_to(source):
                raise ValueError("Source symlink escapes the source tree: " + str(name))
            if path.is_file() or path.is_symlink():
                tar.add(path, arcname=str(Path("geodomdesk-client") / name), recursive=False)
        for path in sorted(customization.rglob("*")):
            relative = path.relative_to(customization)
            if any(
                part in [".git", "__pycache__", "upstream-source", "ops"] for part in relative.parts
            ):
                continue
            if path.is_file() and path.suffix not in (".pyc", ".log"):
                tar.add(
                    path,
                    arcname=str(Path("geodomdesk-client/customization") / relative),
                    recursive=False,
                )
    print("Corresponding source archived to " + str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["source", "customization", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    archive(args.source, args.customization, args.output)
