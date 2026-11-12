#!/usr/bin/env python3
"""Plan a build from the reviewed RustDesk release and customization."""
import json
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channel import version_tuple


RUSTDESK_REPOSITORY = "/repos/rustdesk/rustdesk"


def customization_hash(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    paths = list(root.glob("*.py")) + list(root.glob("*.in"))
    paths.extend(root.glob("*.ps1"))
    for directory, pattern in [
        ("customizations", "*.py"),
        ("customizations", "*.patch"),
        ("tests", "*.py"),
        ("tests", "*.ps1"),
        ("client", "*"),
        ("assets", "*.png"),
        ("assets", "*.ico"),
    ]:
        paths.extend(
            p
            for p in (root / directory).rglob(pattern)
            if p.is_file() and (directory != "client" or p.suffix in (".rs", ".in", ".h", ".ps1", ".sh"))
        )
    paths.extend(
        root / name
        for name in [
            "config/client-channel.json",
            "config/printer-assets.json",
            "config/android-icons.yaml",
            "reference/source-lock.json",
            "requirements.txt",
            "tests/rust/Cargo.lock",
        ]
        if (root / name).is_file()
    )
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    workflow = root.parent / ".github/workflows/build.yml"
    if not workflow.is_file():
        workflow = root / "ci/build.yml"
    if workflow.is_file():
        digest.update(hashlib.sha256(workflow.read_bytes()).digest())
    return digest.hexdigest()


def get(path):
    headers = {"User-Agent": "GeodomDesk-Upstream-Sync", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    with urllib.request.urlopen(
        urllib.request.Request("https://api.github.com" + path, headers=headers), timeout=40
    ) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("GitHub response too large")
        return json.loads(raw)


def official_release(requested_version=""):
    requested_version = requested_version.strip()
    if requested_version:
        requested_version = requested_version.removeprefix("v")
        version_tuple(requested_version)
        tags = RUSTDESK_REPOSITORY + "/releases/tags/"
        try:
            release = get(tags + requested_version)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            release = get(tags + "v" + requested_version)
    else:
        release = get(RUSTDESK_REPOSITORY + "/releases/latest")
    tag = release["tag_name"]
    version = tag.removeprefix("v")
    version_tuple(version)
    if release["draft"] or release["prerelease"]:
        raise ValueError("Select an official stable RustDesk release")
    if requested_version and version != requested_version:
        raise ValueError(f"Official release tag {tag!r} differs from requested version {requested_version}")
    upstream = get(RUSTDESK_REPOSITORY + "/commits/" + urllib.parse.quote(tag, safe=""))["sha"]
    if not re.fullmatch("[0-9a-f]{40}", upstream):
        raise ValueError("Invalid upstream source commit")
    return version, tag, upstream


def reviewed_version(root):
    manifest = (root / "reference/Cargo.toml").read_text(encoding="utf-8")
    package = re.search(r"(?ms)^\[package\][ \t]*\n(.*?)(?=^\[|\Z)", manifest)
    version = re.search(r'(?m)^version[ \t]*=[ \t]*"([^"]+)"', package[1]) if package else None
    if version is None:
        raise ValueError("The reviewed RustDesk manifest must specify a package version")
    version_tuple(version[1])
    return version[1]


def reviewed_release(root):
    channel = json.loads((root / "config/client-channel.json").read_bytes())
    release = official_release(reviewed_version(root))
    if channel["upstream_commit"] != release[2]:
        raise ValueError("The reviewed upstream commit must match the official RustDesk release tag")
    return release


def require_publishable_version(repository, version):
    try:
        published = get("/repos/" + repository + "/releases/latest")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    if version_tuple(version) < version_tuple(published["tag_name"].removeprefix("v")):
        raise ValueError(f"The release channel already serves {published['tag_name']}")


def plan(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    version, tag, upstream = reviewed_release(root)
    repository = os.environ["GITHUB_REPOSITORY"]
    required = True
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        try:
            own = get("/repos/" + repository + "/releases/latest")
            asset = next(a for a in own["assets"] if a["name"] == "geodomdesk-build.json")
            with urllib.request.urlopen(
                urllib.request.Request(
                    asset["browser_download_url"],
                    headers={"User-Agent": "GeodomDesk-Upstream-Sync"},
                ),
                timeout=30,
            ) as response:
                current = json.load(response)
            required = (
                current.get("upstream_commit") != upstream
                or current.get("customization_hash") != customization_hash(root)
            )
        except (urllib.error.HTTPError, StopIteration, ValueError):
            required = True
    build_number = int(os.environ["GITHUB_RUN_NUMBER"]) * 100 + int(
        os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    )
    reuse_run = os.environ.get("GEODOMDESK_REUSE_DESKTOP_RUN", "")
    if reuse_run:
        from reuse_desktop import validate

        build_number = validate(
            reuse_run, os.environ["GEODOMDESK_REUSE_CONFIG"], version, upstream, get
        )
    result = {
        "version": version,
        "official_tag": tag,
        "upstream_commit": upstream,
        "build_number": str(build_number),
        "build": "true" if required else "false",
        "reuse_run": reuse_run,
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in result.items():
            output.write(key + "=" + value + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    plan()
