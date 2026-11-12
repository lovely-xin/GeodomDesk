"""Resolve and verify the upstream inputs to the RustDesk sync workflow."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlencode


ROOT = Path.cwd().resolve()
CUSTOMIZATION = ROOT / "customization"
CHANNEL = "customization/config/client-channel.json"
REFERENCE = "customization/reference/"
LOCK = REFERENCE + "source-lock.json"
COMPATIBILITY_ERRORS = (
    "ValueError: Upstream changed",
    "ValueError: Upstream removed",
    "ValueError: Upstream added a conflicting",
    "ValueError: Upstream/customization conflict:",
    "ValueError: Upstream TCP proxy calls changed",
    "ValueError: Rendezvous attempt signatures changed",
    "ValueError: Transport race call sites changed",
    "ValueError: Peer handshake branch changed:",
    "ValueError: Trusted key block changed",
)
sys.path.insert(0, str(CUSTOMIZATION))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def action_output(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def resolve(requested_version, recheck=False):
    from plan_upstream import official_release, reviewed_version, version_tuple

    version, _, commit = official_release(requested_version)
    current = reviewed_version(CUSTOMIZATION)
    if version_tuple(version) < version_tuple(current):
        raise ValueError(f"Select RustDesk {current} or a later stable release")
    reviewed = json.loads((ROOT / CHANNEL).read_bytes())["upstream_commit"]
    check_required = recheck or commit != reviewed
    action_output(
        version=version,
        upstream_commit=commit,
        check_required="true" if check_required else "false",
        attempt_name=f"rustdesk-repair-attempt-{os.environ['GITHUB_SHA']}-{commit}",
    )
    if not check_required:
        print(f"RustDesk {version} is already reviewed. Source preparation and Codex were skipped.")


def check_attempt(name):
    from plan_upstream import get

    repository = os.environ["GITHUB_REPOSITORY"]
    repository_id = int(os.environ["GITHUB_REPOSITORY_ID"])
    head = os.environ["GITHUB_SHA"]
    endpoint = f"/repos/{repository}/actions/artifacts?" + urlencode({"name": name})
    for artifact in get(endpoint)["artifacts"]:
        run = artifact.get("workflow_run") or {}
        if (
            artifact["name"] == name
            and not artifact["expired"]
            and run.get("repository_id") == repository_id
            and run.get("head_repository_id") == repository_id
            and run.get("head_branch") == "main"
            and run.get("head_sha") == head
        ):
            action_output(already_attempted="true")
            print(f"Codex already attempted this source pair in run {run['id']}.")
            return
    action_output(already_attempted="false")


def source_commit(source):
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


def prepare(source, version, customization=CUSTOMIZATION, *, detect_conflict=False):
    result = subprocess.run(
        [sys.executable, str(customization / "prepare_client.py"), "--source", str(source), "--version", version],
        cwd=ROOT,
        capture_output=detect_conflict,
        text=detect_conflict,
        encoding="utf-8" if detect_conflict else None,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"} if detect_conflict else None,
    )
    if detect_conflict:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        lines = result.stderr.splitlines()
        if result.returncode and lines and (
            lines[-1].startswith(COMPATIBILITY_ERRORS)
            or (
                lines[-1].startswith("subprocess.CalledProcessError:")
                and any(line.startswith("error: patch failed:") for line in lines)
            )
        ):
            return False
    result.check_returncode()
    return True


def clean_upgrade(source, version, commit, channel):
    old_lock = json.loads((ROOT / LOCK).read_bytes())
    missing = sorted(name for name in old_lock if not (source / name).is_file())
    if missing:
        print("Upstream removed customized files: " + ", ".join(missing))
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
        if not prepare(original, version, detect_conflict=True) or not prepare(
            refreshed, version, customization, detect_conflict=True
        ):
            return False
        generated = set()
        for checkout in (original, refreshed):
            config = json.loads((checkout / "geodomdesk-build-config.json").read_bytes())
            generated.update(config["generated_source_files"])
        for name in set(old_lock) | generated:
            left, right = original / name, refreshed / name
            if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
                print(f"The refreshed customization changes the prepared output for {name}.")
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
        if not prepare(probe, version, detect_conflict=True):
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
    resolution = commands.add_parser("resolve")
    resolution.add_argument("--version", default="")
    resolution.add_argument("--recheck", action="store_true")
    commands.add_parser("check-attempt").add_argument("--name", required=True)
    for name in ("preflight", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--version", required=True)
        command.add_argument("--commit", required=True)
    commands.add_parser("scope")
    args = parser.parse_args()
    if args.command == "resolve":
        resolve(args.version, args.recheck)
    elif args.command == "check-attempt":
        check_attempt(args.name)
    elif args.command == "scope":
        validate_scope()
    else:
        (preflight if args.command == "preflight" else validate)(args.source, args.version, args.commit)


if __name__ == "__main__":
    main()
