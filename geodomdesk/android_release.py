"""Prepare private Gradle signing input and validate the final Android release APK."""

import argparse
import base64
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path


def prepare(source, keystore):
    data = base64.b64decode(os.environ["GEODOMDESK_ANDROID_KEYSTORE"], validate=True)
    password = os.environ["GEODOMDESK_ANDROID_KEY_PASSWORD"]
    if not data or len(data) > 64 * 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", password):
        raise ValueError("Invalid Android signing input")
    keystore.write_bytes(data)
    keystore.chmod(0o600)
    properties = source / "flutter/android/key.properties"
    properties.write_text(
        f"storeFile={keystore.resolve().as_posix()}\nstorePassword={password}\n"
        f"keyPassword={password}\nkeyAlias=geodomdesk\n"
    )
    properties.chmod(0o600)
    print("Private Android release signing configured")


def verify(source, apk):
    config = json.loads((source / "geodomdesk-build-config.json").read_text())
    tools = Path(os.environ["ANDROID_HOME"]) / "build-tools/36.0.0"
    certificate = subprocess.check_output(
        [str(tools / "apksigner"), "verify", "--print-certs", str(apk)], text=True
    )
    fingerprints = re.findall(r"certificate SHA-256 digest: ([0-9a-f]+)", certificate)
    if set(fingerprints) != {config["android_certificate_sha256"]}:
        raise ValueError("The APK does not use the permanent GeodomDesk signing certificate")
    badging = subprocess.check_output([str(tools / "aapt"), "dump", "badging", str(apk)], text=True)
    package = re.search(
        r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging
    )
    expected = (config["android_package_id"], str(config["build_number"]), config["version"])
    if not package or package.groups() != expected:
        raise ValueError(
            f"Android identity differs: expected {expected}, got {package.groups() if package else None}"
        )
    if "launchable-activity: name='com.carriez.flutter_hbb.MainActivity'" not in badging:
        raise ValueError("The Android entry point does not match the native JNI namespace")
    with zipfile.ZipFile(apk) as archive:
        for name in ["librustdesk.so", "libflutter.so", "libapp.so"]:
            if archive.getinfo("lib/arm64-v8a/" + name).file_size < 1024 * 1024:
                raise ValueError("Incomplete Android ARM64 native payload")
        if any(name.startswith(("lib/armeabi-v7a/", "lib/x86_64/")) for name in archive.namelist()):
            raise ValueError("Unexpected ABI in the ARM64 APK")
    print("Verified signed GeodomDesk Android ARM64 " + config["release_id"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.source.resolve(), args.file.resolve())
    else:
        verify(args.source.resolve(), args.file.resolve())
