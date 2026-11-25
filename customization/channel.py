"""GeodomDesk's public update contract, compatible with the RustDesk updater."""

import base64
import hashlib
import json
import re
from pathlib import Path

_CHANNEL_CONFIG = json.loads(
    (Path(__file__).resolve().parent / "config/client-channel.json").read_text(encoding="utf-8")
)
BASE_URL = _CHANNEL_CONFIG["base_url"].rstrip("/")
KEY_ID = _CHANNEL_CONFIG["key_id"]
SIGNATURE_CONTEXT = b"GeodomDesk update metadata v1\n"
METADATA_NAME = "geodomdesk-update.json"
APP = "geodomdesk"
TARGETS = (("windows", "x86_64", "exe"), ("linux", "x86_64", "deb"), ("android", "aarch64", "apk"))
WINDOWS_SERVER_2008_R2 = ("windows-server-2008-r2", "x86_64", "exe")


def release_targets(artifacts):
    selectors = {(item.get("platform"), item.get("arch"), item.get("format")) for item in artifacts}
    for targets in (TARGETS, (*TARGETS, WINDOWS_SERVER_2008_R2)):
        if len(artifacts) == len(targets) and selectors == set(targets):
            return targets
    raise ValueError("Release requires Windows x64 EXE, Ubuntu amd64 DEB and Android ARM64 APK")


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", version):
        raise ValueError("Use the canonical three-part official version, e.g. 1.5.1")
    parts = tuple(map(int, version.split(".")))
    if any(part > 65535 for part in parts) or version != ".".join(map(str, parts)):
        raise ValueError("Noncanonical or out-of-range version")
    return parts


def release_parts(release_id):
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)(?:-r([1-9][0-9]*))?", release_id or "")
    if not match:
        raise ValueError("Invalid GeodomDesk release id")
    version_tuple(match[1])
    revision = int(match[2] or 0)
    if revision >= 2**64:
        raise ValueError("Build number exceeds the client range")
    return match[1], revision


def make_release_id(version, build_number):
    version_tuple(version)
    if type(build_number) is not int or not 0 < build_number < 2**64:
        raise ValueError("Invalid GeodomDesk build number")
    return f"{version}-r{build_number}"


def stable_json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_url(version):
    release_parts(version)
    return f"{BASE_URL}/releases/{version}/"


def file_name(version, platform, fmt):
    release_parts(version)
    suffix = {
        "windows": "windows-x64",
        "linux": "ubuntu-amd64",
        "android": "android-arm64",
        "windows-server-2008-r2": "windows-server-2008-r2-x64",
    }[platform]
    return f"geodomdesk-{version}-{suffix}.{fmt}"


def public_key(seed):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def signed_metadata(version, published_at, artifacts, seed):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    official_version, build_number = release_parts(version)
    release_targets(artifacts)
    for item in artifacts:
        name = file_name(version, item["platform"], item["format"])
        if (
            item["file_name"] != name
            or item["url"] != release_url(version) + name
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or not 0 < item["size"] <= 512 * 1024 * 1024
        ):
            raise ValueError("Invalid update artifact")
    metadata = stable_json(
        {
            "schema_version": 1,
            "app": APP,
            "package_id": APP,
            "version": official_version,
            "release_id": version,
            "published_at": published_at,
            "signature_key_id": KEY_ID,
            "artifacts": artifacts,
        }
    )
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(SIGNATURE_CONTEXT + metadata)
    sidecar = stable_json(
        {
            "schema_version": 1,
            "algorithm": "ed25519",
            "key_id": KEY_ID,
            "signature": base64.b64encode(signature).decode(),
        }
    )
    return metadata, sidecar


def artifact(path, version, platform, arch, fmt, digest=None):
    path = Path(path)
    if path.name != file_name(version, platform, fmt):
        raise ValueError("Unexpected artifact name")
    return {
        "platform": platform,
        "arch": arch,
        "format": fmt,
        "file_name": path.name,
        "size": path.stat().st_size,
        "sha256": digest or sha256(path),
        "url": release_url(version) + path.name,
    }
