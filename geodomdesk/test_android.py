"""Check release identity and certificate rejection without installing an Android app."""

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import android_release


class AndroidReleaseTests(unittest.TestCase):
    def test_requires_own_package_version_certificate_and_arm64_payload(self):
        with tempfile.TemporaryDirectory(prefix="geodomdesk-android-test-") as tmp:
            root = Path(tmp)
            config = {
                "android_package_id": "cn.geodom.geodomdesk",
                "android_certificate_sha256": "a" * 64,
                "version": "1.5.1",
                "build_number": 701,
                "release_id": "1.5.1-r701",
            }
            (root / "geodomdesk-build-config.json").write_text(json.dumps(config))
            apk = root / "geodomdesk.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                for name in ["librustdesk.so", "libflutter.so", "libapp.so"]:
                    archive.writestr("lib/arm64-v8a/" + name, b"x" * (1024 * 1024))
            certificate = "Signer #1 certificate SHA-256 digest: " + "a" * 64
            badging = (
                "package: name='cn.geodom.geodomdesk' versionCode='701' versionName='1.5.1'\n"
                "launchable-activity: name='com.carriez.flutter_hbb.MainActivity'"
            )
            with mock.patch.dict(os.environ, ANDROID_HOME=tmp):
                with mock.patch.object(
                    subprocess, "check_output", side_effect=[certificate, badging]
                ):
                    android_release.verify(root, apk)
                for cert, package in [
                    (certificate.replace("a" * 64, "b" * 64), badging),
                    (
                        certificate,
                        badging.replace("cn.geodom.geodomdesk", "com.carriez.flutter_hbb"),
                    ),
                    (certificate, badging.replace("versionCode='701'", "versionCode='1'")),
                    (certificate, badging.replace("MainActivity", "MissingActivity")),
                ]:
                    with self.subTest(cert=cert, package=package), mock.patch.object(
                        subprocess, "check_output", side_effect=[cert, package]
                    ), self.assertRaises(ValueError):
                        android_release.verify(root, apk)
                with zipfile.ZipFile(apk, "a") as archive:
                    archive.writestr("lib/x86_64/libapp.so", b"unexpected ABI")
                with mock.patch.object(
                    subprocess, "check_output", side_effect=[certificate, badging]
                ), self.assertRaises(ValueError):
                    android_release.verify(root, apk)


if __name__ == "__main__":
    unittest.main()
