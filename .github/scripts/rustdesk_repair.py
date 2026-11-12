"""Resolve and verify the upstream inputs to the manual RustDesk repair workflow."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile


ROOT = Path.cwd().resolve()
CUSTOMIZATION = ROOT / "customization"
CHANNEL = "customization/config/client-channel.json"
REFERENCE = "customization/reference/"
LOCK = REFERENCE + "source-lock.json"
sys.path.insert(0, str(CUSTOMIZATION))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def action_output(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def resolve(requested_version):
    from plan_upstream import official_release, reviewed_version, version_tuple

    version, _, commit = official_release(requested_version)
    current = reviewed_version(CUSTOMIZATION)
    if version_tuple(version) < version_tuple(current):
        raise ValueError(f"Select RustDesk {current} or a later stable release")
    action_output(version=version, upstream_commit=commit)


def source_commit(source):
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


def prepare(source, version, customization=CUSTOMIZATION):
    subprocess.run(
        [sys.executable, str(customization / "prepare_client.py"), "--source", str(source), "--version", version],
        cwd=ROOT,
        check=True,
    )


def clean_upgrade(source, version, commit, channel):
    old_lock = json.loads((ROOT / LOCK).read_bytes())
    if any(not (source / name).is_file() for name in old_lock):
        return False
    with tempfile.TemporaryDirectory(prefix="geodomdesk-clean-upgrade-") as temporary:
        temporary = Path(temporary)
        original = temporary / "original"
        refreshed = temporary / "refreshed"
        customization = temporary / "customization"
        for checkout in (original, refreshed):
            shutil.copytree(source, checkout, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        shutil.copytree(CUSTOMIZATION, customization, ignore=shutil.ignore_patterns("__pycache__"))
        new_lock = {}
        for name in old_lock:
            data = (source / name).read_bytes().replace(b"\r\n", b"\n")
            (customization / "reference" / name).write_bytes(data)
            new_lock[name] = hashlib.sha256(data).hexdigest()
        channel["upstream_commit"] = commit
        for file, value in (("reference/source-lock.json", new_lock), ("config/client-channel.json", channel)):
            (customization / file).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        try:
            prepare(original, version)
            prepare(refreshed, version, customization)
        except subprocess.CalledProcessError:
            return False
        generated = set()
        for checkout in (original, refreshed):
            config = json.loads((checkout / "geodomdesk-build-config.json").read_bytes())
            generated.update(config["generated_source_files"])
        for name in set(old_lock) | generated:
            left, right = original / name, refreshed / name
            if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
                return False
        for file in [*("reference/" + name for name in old_lock), "reference/source-lock.json", "config/client-channel.json"]:
            candidate = (customization / file).read_bytes()
            destination = CUSTOMIZATION / file
            if destination.read_bytes().replace(b"\r\n", b"\n") != candidate.replace(b"\r\n", b"\n"):
                destination.write_bytes(candidate)
    return True


def preflight(source, version, commit):
    if source_commit(source) != commit:
        raise ValueError("The upstream checkout does not match the resolved release")
    channel = json.loads((ROOT / CHANNEL).read_bytes())
    if channel["upstream_commit"] != commit:
        clean = clean_upgrade(source, version, commit, channel)
        action_output(repair_needed="true", use_codex="false" if clean else "true")
        print("Prepared a clean upstream merge." if clean else "Preparing an upstream conflict repair.")
        return
    with tempfile.TemporaryDirectory(prefix="geodomdesk-repair-") as temporary:
        probe = Path(temporary) / "client"
        shutil.copytree(source, probe, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        try:
            prepare(probe, version)
        except subprocess.CalledProcessError:
            action_output(repair_needed="true", use_codex="true")
            return
    action_output(repair_needed="false")
    print(f"The reviewed customization already prepares RustDesk {version}. No repair is needed.")


def permitted_file(name):
    path = PurePosixPath(name)
    return (
        name == CHANNEL
        or name.startswith(REFERENCE)
        or (name.startswith("customization/client/") and path.suffix in {".rs", ".ps1", ".sh", ".in", ".h"})
        or (
            name.startswith("customization/customizations/")
            and path.suffix in {".py", ".patch"}
            and name != "customization/customizations/upstream.py"
        )
    )


def validate_scope():
    changed = set(filter(None, git("diff", "--cached", "--no-renames", "--name-only", "-z").decode().split("\0")))
    if not changed or any(not permitted_file(name) for name in changed):
        raise ValueError("Repair is empty or modifies files outside the client customization scope")
    for record in git("--literal-pathspecs", "ls-files", "--stage", "-z", "--", *changed).split(b"\0"):
        if record and record.split(b" ", 1)[0] not in {b"100644", b"100755"}:
            raise ValueError("Repair must contain regular files only")
    return changed


def validate_baseline(source, commit, changed):
    if source_commit(source) != commit:
        raise ValueError("The upstream checkout does not match the resolved release")
    original = json.loads(git("show", "HEAD:" + CHANNEL))
    candidate = json.loads((ROOT / CHANNEL).read_bytes())
    if candidate != {**original, "upstream_commit": commit}:
        raise ValueError("The channel may change only its upstream commit pin")
    previous = json.loads(git("show", "HEAD:" + LOCK))
    current = json.loads((ROOT / LOCK).read_bytes())
    reference_files = {
        path.removeprefix(REFERENCE)
        for path in git("ls-files", "-z", "--", REFERENCE).decode().split("\0")
        if path and path != LOCK
    }
    unlocked = reference_files - current.keys()
    if current.keys() - reference_files or unlocked & previous.keys() or any(REFERENCE + name in changed for name in unlocked):
        raise ValueError("New or modified reference files must be tracked in the lock")
    if any((source / name).exists() for name in previous.keys() - current.keys()):
        raise ValueError("The repair removed a reference file still present upstream")
    for name, digest in current.items():
        reference = (ROOT / REFERENCE / name).read_bytes().replace(b"\r\n", b"\n")
        upstream = source / name
        if not upstream.is_file() or reference != upstream.read_bytes().replace(b"\r\n", b"\n"):
            raise ValueError("The reference differs from the official upstream source: " + name)
        if hashlib.sha256(reference).hexdigest() != digest:
            raise ValueError("The reference hash does not match the lock: " + name)


def validate(source, version, commit):
    changed = validate_scope()
    validate_baseline(source, commit, changed)
    prepare(source, version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("resolve").add_argument("--version", default="")
    for name in ("preflight", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--version", required=True)
        command.add_argument("--commit", required=True)
    commands.add_parser("scope")
    args = parser.parse_args()
    if args.command == "resolve":
        resolve(args.version)
    elif args.command == "scope":
        validate_scope()
    else:
        (preflight if args.command == "preflight" else validate)(args.source, args.version, args.commit)


if __name__ == "__main__":
    main()
