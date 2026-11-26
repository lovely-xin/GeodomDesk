"""Reuse unpublished desktop artifacts only when their prepared source is unchanged."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def source_hash(source):
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        name = path.relative_to(source).as_posix()
        if (
            not path.is_file()
            or name.startswith("flutter/android/")
            or name == "geodomdesk-build-config.json"
        ):
            continue
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def desktop_recipe(workflow, root=None):
    root = Path(root) if root is not None else ROOT
    sections = []
    for key in ["RUST_VERSION", "FLUTTER_VERSION", "VCPKG_COMMIT"]:
        sections.append(re.search(r"(?m)^  " + key + r": (.+)$", workflow).group(0))
    for key in ["CARGO_PROFILE_RELEASE_LTO", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS"]:
        profile_match = re.search(r"(?m)^  " + key + r": (.+)$", workflow)
        if profile_match:
            sections.append(profile_match.group(0))
    for job in ["bridge", "windows", "ubuntu"]:
        match = re.search(r"(?ms)^  " + job + r":\n.*?(?=^  [a-zA-Z_]+:\n|\Z)", workflow)
        if match:
            sections.append(match.group(0).replace("    if: needs.plan.outputs.reuse_run == ''\n", ""))
        elif job in ("windows", "ubuntu"):
            raise ValueError(f"Missing required desktop job in workflow: {job}")
    for helper in [
        "packaging/repack_deb.py",
        "packaging/patch_flutter_sdk.py",
        "packaging/prepare_printer.py",
        "config/printer-assets.json",
    ]:
        path = root / helper
        if path.is_file():
            sections.append(f"{helper}:{hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    return "".join(sections)


def prepare_hash(customization, version, build_number, temporary):
    source = temporary / "client"
    lock = json.loads((customization / "reference/source-lock.json").read_text())
    for name in lock:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((customization / "reference" / name).read_bytes())
    (source / "res").mkdir(exist_ok=True)
    (source / "flutter/windows/runner/resources").mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            str(customization / "packaging/prepare_client.py"),
            "--source",
            str(source),
            "--version",
            version,
            "--build-number",
            str(build_number),
        ],
        check=True,
    )
    return source_hash(source), json.loads((source / "geodomdesk-build-config.json").read_text())


def validate(run_id, config_file, version, upstream, get):
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Expected a canonical workflow run ID")
    repository = os.environ["GITHUB_REPOSITORY"]
    run = get(f"/repos/{repository}/actions/runs/{run_id}")
    failed_main = (
        run["head_branch"] == "main"
        and run["event"] in ("push", "workflow_dispatch", "schedule")
        and run["conclusion"] == "failure"
    )
    validated_upstream = (
        run["head_branch"].startswith("upstream/rustdesk-")
        and run["event"] == "workflow_dispatch"
        and run["conclusion"] == "success"
    )
    if (
        run["head_repository"]["full_name"] != repository
        or not (failed_main or validated_upstream)
        or run["name"] != "Build GeodomDesk"
        or run["status"] != "completed"
        or not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
    ):
        raise ValueError(
            "Reuse requires a failed main build or a validated upstream candidate build from this repository"
        )
    config = json.loads(Path(config_file).read_text())
    if (
        config["app"] != "GeodomDesk"
        or config["version"] != version
        or config["upstream_commit"] != upstream
    ):
        raise ValueError("The reusable desktop build uses a different client or official source")
    root = ROOT
    subprocess.run(["git", "fetch", "--depth=1", "origin", run["head_sha"]], check=True)
    archive = subprocess.check_output(
        ["git", "archive", run["head_sha"], "customization", ".github/workflows/build-client.yml"]
    )
    with tempfile.TemporaryDirectory(prefix="geodomdesk-reuse-check-") as tmp:
        temporary = Path(tmp)
        previous = temporary / "previous"
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                target = (previous / member.name).resolve()
                if not target.is_relative_to(previous.resolve()):
                    raise ValueError("Invalid customization archive path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
        previous_recipe = (previous / ".github/workflows/build-client.yml").read_text()
        current_recipe = (root.parent / ".github/workflows/build-client.yml").read_text()
        if desktop_recipe(previous_recipe, previous / "customization") != desktop_recipe(current_recipe, root):
            raise ValueError("The desktop build recipe or toolchain changed; rebuild all platforms")
        before, previous_config = prepare_hash(
            previous / "customization", version, config["build_number"], temporary / "before"
        )
        after, current_config = prepare_hash(
            root, version, config["build_number"], temporary / "after"
        )
        if config != previous_config or config != current_config or before != after:
            raise ValueError("Desktop source or build configuration changed; rebuild all platforms")
    print("Verified unchanged desktop inputs; reusing build " + config["release_id"])
    return config["build_number"]
