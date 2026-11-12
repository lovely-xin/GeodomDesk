import base64
import contextlib
import copy
import errno
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import channel
import prepare_client

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "Publisher runs on Linux")
class DownloadTests(unittest.TestCase):
    def test_parallel_ranges_strip_credentials_and_verified_cache_skips_network(self):
        self.check_download()

    def test_corrupt_ranges_do_not_publish_partial_cache(self):
        for failure in ["short", "range", "hash"]:
            with self.subTest(failure=failure):
                self.check_download(failure)

    def check_download(self, failure=None):
        import publisher

        data = bytes(range(256)) * (5 * 1024 * 1024 // 256)
        requests = []

        class Response(io.BytesIO):
            status = 206

            def __init__(self, start, end):
                block = data[start : end + 1]
                if end > start:
                    if failure == "short":
                        block = block[:-1]
                    if failure == "hash":
                        block = b"x" + block[1:]
                super().__init__(block)
                self.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
                if failure == "range" and end > start:
                    self.headers["Content-Range"] = "bytes 0-1/2"

            def geturl(self):
                return "https://release-assets.githubusercontent.com/fixture"

        class Opener:
            def open(self, request, **kwargs):
                requests.append(request)
                start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
                return Response(start, end)

        with tempfile.TemporaryDirectory(prefix="geodomdesk-download-test-") as tmp:
            cache = Path(tmp)
            asset = {
                "name": "fixture.exe",
                "url": "https://api.github.com/repos/owner/repo/releases/assets/1",
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
            with mock.patch.object(publisher.urllib.request, "build_opener", return_value=Opener()):
                if failure:
                    with self.assertRaises(ValueError):
                        publisher.download(asset, cache, {"Authorization": "Bearer test-only"})
                    self.assertEqual(list(cache.iterdir()), [])
                    return
                result = publisher.download(asset, cache, {"Authorization": "Bearer test-only"})
                self.assertEqual(result.read_bytes(), data)
            self.assertEqual(len(requests), 6)
            self.assertTrue(requests[0].get_header("Authorization"))
            self.assertTrue(all(not r.get_header("Authorization") for r in requests[1:]))
            with mock.patch.object(
                publisher.urllib.request,
                "build_opener",
                side_effect=AssertionError("Unexpected download"),
            ):
                self.assertEqual(publisher.download(asset, cache, {}), result)


class ChannelTests(unittest.TestCase):
    def test_version_contract(self):
        self.assertLess(channel.version_tuple("1.5.2"), channel.version_tuple("1.5.10"))
        self.assertEqual(
            channel.release_parts(channel.make_release_id("1.5.1", 102)), ("1.5.1", 102)
        )
        for value in ["v1.5.2", "1.5.2-beta", "1.5.2/..", "1.05.2", "1.5", "1.5.65536", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                channel.version_tuple(value)

    def metadata(self):
        items = []
        for platform, arch, fmt in channel.TARGETS:
            name = channel.file_name("1.5.2", platform, fmt)
            items.append(
                {
                    "platform": platform,
                    "arch": arch,
                    "format": fmt,
                    "file_name": name,
                    "url": channel.release_url("1.5.2") + name,
                    "size": 1234,
                    "sha256": "a" * 64,
                }
            )
        return items

    def test_ed25519_signature_and_tamper(self):
        seed = bytes(range(32))
        metadata, signature = channel.signed_metadata(
            "1.5.2", "2026-11-04T00:00:00Z", self.metadata(), seed
        )
        sig = base64.b64decode(json.loads(signature)["signature"])
        key = Ed25519PublicKey.from_public_bytes(channel.public_key(seed))
        key.verify(sig, channel.SIGNATURE_CONTEXT + metadata)
        for context, data in [
            (channel.SIGNATURE_CONTEXT, metadata + b" "),
            (b"RustDesk update metadata v1\n", metadata),
        ]:
            with self.assertRaises(InvalidSignature):
                key.verify(sig, context + data)
        self.assertEqual(json.loads(metadata)["app"], "geodomdesk")

    def test_rejects_foreign_or_incomplete_artifacts(self):
        items = self.metadata()
        for changed in [
            items[:1],
            items * 2,
            [{**items[0], "url": "https://example.com/file"}, *items[1:]],
            [{**items[0], "sha256": "abc"}, *items[1:]],
            [{**items[0], "size": 0}, *items[1:]],
            [{**items[0], "file_name": "rustdesk.exe"}, *items[1:]],
        ]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                channel.signed_metadata("1.5.2", "2026-11-04T00:00:00Z", changed, bytes(32))

    @unittest.skipUnless(
        (ROOT / "reference").is_dir(), "upstream reference is a local review fixture"
    )
    def test_preparation_and_upstream_drift(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="test-source-") as tmp:
            source = Path(tmp)
            lock = json.loads((ROOT / "reference/source-lock.json").read_text())
            for name in lock:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / "reference" / name).read_bytes())
            (source / "res").mkdir(exist_ok=True)
            (source / "flutter/windows/runner/resources").mkdir(parents=True)
            german = source / "src/lang/de.rs"
            german.write_text('lazy_static! { pub static ref T = [\n        ("Restore default ID", "Bestehende Übersetzung"),\n    ].iter().cloned().collect();\n}\n', encoding="utf-8")
            cargo = source / "Cargo.toml"
            manifest = cargo.read_bytes()
            original_version = re.search(rb'(?m)^version\s*=\s*"([^"]+)"', manifest)[1]
            major, minor, patch = channel.version_tuple(original_version.decode())
            new_version = f"{major}.{minor}.{patch + 1}"
            version_line = b'version = "' + new_version.encode() + b'"'
            original_line = b'version = "' + original_version + b'"'
            cargo.write_bytes(manifest.replace(original_line, version_line, 1))
            cargo_lock = source / "Cargo.lock"
            rustdesk_package = b'name = "rustdesk"\n'
            cargo_lock.write_bytes(
                cargo_lock.read_bytes().replace(rustdesk_package + original_line, rustdesk_package + version_line, 1)
            )
            bad = source / "src/common.rs"
            original = bad.read_bytes()
            bad.write_bytes(
                original.replace(
                    b"if is_custom_client() {", b"if upstream_changed_condition() {", 1
                )
            )
            with self.assertRaises(ValueError):
                prepare_client.prepare(source, new_version)
            self.assertIn(version_line, cargo.read_bytes())
            self.assertNotIn(b"GeodomDesk", (source / "libs/hbb_common/src/config.rs").read_bytes())
            bad.write_bytes(original + b"// upstream change outside the customization\n")
            # Match a Windows checkout while retaining real upstream changes to merge.
            for name in lock:
                path = source / name
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            with contextlib.redirect_stdout(io.StringIO()):
                prepare_client.prepare(source, new_version)
            config = (source / "libs/hbb_common/src/config.rs").read_text(encoding="utf-8")
            for default_map, option in (
                ("DEFAULT_SETTINGS", '"allow-remote-config-modification"'),
                ("DEFAULT_LOCAL_SETTINGS", '"enable-udp-punch"'),
            ):
                default_settings = config.split(f"pub static ref {default_map}:", 1)[1].split("]));", 1)[0]
                self.assertIn(f'({option}.to_owned(), "Y".to_owned())', default_settings)
            self.assertNotIn('"enable-webrtc"', default_settings)
            self.assertIn('("enable-tcp-punch".to_owned(), "N".to_owned())', default_settings)
            self.assertIn('("Restore default ID", "Bestehende Übersetzung")', german.read_text(encoding="utf-8"))
            self.assertIn('("Data and permissions", "")', german.read_text(encoding="utf-8"))
            ffi = (source / "src/flutter_ffi.rs").read_text(encoding="utf-8")
            self.assertIn("pub fn main_get_default_id()", ffi)
            self.assertIn("pub fn main_restore_default_id()", ffi)
            web_bridge = (source / "flutter/lib/web/bridge.dart").read_text(encoding="utf-8")
            self.assertIn("mainImportOfficialUserData", web_bridge)
            self.assertIn("mainRestoreDefaultId", web_bridge)
            self.assertTrue(
                bad.read_bytes().endswith(b"// upstream change outside the customization\n")
            )
            self.assertIn(version_line, cargo.read_bytes())
            self.assertIn(
                'RwLock::new("GeodomDesk".to_owned())',
                (source / "libs/hbb_common/src/config.rs").read_text(),
            )
            gradle = (source / "flutter/android/app/build.gradle").read_text()
            self.assertIn('applicationId "cn.geodom.geodomdesk"', gradle)
            self.assertIn('namespace "com.carriez.flutter_hbb"', gradle)
            manifest = (source / "flutter/android/app/src/main/AndroidManifest.xml").read_text()
            self.assertIn('android:scheme="geodomdesk"', manifest)
            self.assertNotIn('android:label="RustDesk', manifest)
            self.assertIn("cn.geodom.geodomdesk.DEBUG_BOOT_COMPLETED", manifest)
            background = ET.parse(
                source / "flutter/android/app/src/main/res/values/ic_launcher_background.xml"
            )
            self.assertEqual(background.findall("color[@name='ic_launcher_background']"), [])
            self.assertIn(
                "dir.join(APP_PREFIX)", (source / "libs/portable/src/main.rs").read_text()
            )
            self.assertNotIn(
                "RuntimeBroker_rustdesk.exe",
                (source / "src/privacy_mode/win_topmost_window.rs").read_text(),
            )
            self.assertIn(
                "client.get(crate::geodomdesk_update::LATEST_URL)", bad.read_text(encoding="utf-8")
            )
            self.assertNotIn("client.post(&url).json(&request)", bad.read_text(encoding="utf-8"))
            self.assertIn(
                "GeodomDesk update metadata v1", (source / "src/update_metadata.rs").read_text()
            )
            windows = (source / "src/platform/windows.rs").read_text(encoding="utf-8")
            self.assertNotIn('file_name.starts_with("rustdesk-")', windows)
            self.assertIn('file_name.starts_with("geodomdesk-")', windows)
            self.assertIn(
                "geodomdesk-verified-",
                (source / "src/platform/windows/verified_update.rs").read_text(),
            )
            self.assertNotIn(
                "pipe\\rustdesk_term_",
                (source / "src/server/terminal_service.rs").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "port = 21128;", (source / "src/rendezvous_mediator.rs").read_text(encoding="utf-8")
            )
            self.assertIn("21129", (source / "src/lan.rs").read_text())
            self.assertIn(
                "GEODOMDESK_RUNNER_WIN32_WINDOW",
                (source / "flutter/windows/runner/win32_window.cpp").read_text(),
            )
            self.assertNotIn(
                "if (!bind.isCustomClient()) {",
                (source / "flutter/lib/common.dart").read_text(encoding="utf-8"),
            )
            ui = (source / "flutter/lib/models/model.dart").read_text(encoding="utf-8")
            self.assertIn("await loginDialog()", ui)
            self.assertIn("_accountLoginPending", ui)
            self.assertIn("'account-login-required'", ui)
            for name in [
                "flutter/lib/desktop/pages/desktop_setting_page.dart",
                "flutter/lib/desktop/pages/desktop_home_page.dart",
                "flutter/lib/desktop/pages/connection_page.dart",
                "flutter/lib/desktop/pages/install_page.dart",
                "flutter/lib/mobile/pages/settings_page.dart",
                "flutter/lib/mobile/pages/connection_page.dart",
            ]:
                self.assertNotRegex((source / name).read_text(encoding="utf-8"),
                                    r"https?://(?:[a-z0-9-]+\.)?rustdesk\.(?:com|org)(?:/|['\"])")
            mobile_settings = (source / "flutter/lib/mobile/pages/settings_page.dart").read_text(encoding="utf-8")
            self.assertNotIn("showDeployDialog", mobile_settings)
            desktop_settings = (source / "flutter/lib/desktop/pages/desktop_setting_page.dart").read_text(encoding="utf-8")
            self.assertIn("_Button('Restore', _restoreDefaultId", desktop_settings)
            tray = (source / "src/tray.rs").read_text(encoding="utf-8")
            self.assertIn('let path = path.join(r"data\\flutter_assets\\assets\\icon.ico");', tray)
            self.assertNotIn('let path = path.join(r"data\\flutter_assets\\assets\\icon.png");', tray)
            self.assertEqual(
                (source / "res/tray-icon.ico").read_bytes(),
                (source / "flutter/assets/icon.ico").read_bytes(),
            )
            window = (source / "flutter/windows/runner/win32_window.cpp").read_text(encoding="utf-8")
            self.assertIn("icons_.Update(window, dpi);", window)
            self.assertIn("icons_.Update(hwnd, HIWORD(wparam));", window)
            self.assertFalse("note-at-conn-end-tip" in desktop_settings + mobile_settings,
                             "An unsupported connection-note setting remained visible")
            baseline_config = (ROOT / "reference/libs/hbb_common/src/config.rs").read_text(
                encoding="utf-8"
            )
            prepared_config = (source / "libs/hbb_common/src/config.rs").read_text(encoding="utf-8")

            def auto_id(text):
                start = text.index("    fn get_auto_id() -> Option<String> {")
                return text[start : text.index("    pub fn get_auto_password(", start)]

            self.assertEqual(auto_id(baseline_config), auto_id(prepared_config))
            self.assertIn("geodomdesk_clone_guard::refresh(", prepared_config)
            baseline_uuid = (ROOT / "reference/libs/hbb_common/src/lib.rs").read_text(
                encoding="utf-8"
            )
            prepared_uuid = (source / "libs/hbb_common/src/lib.rs").read_text(encoding="utf-8")
            start = baseline_uuid.index("pub fn get_uuid() -> Vec<u8> {")
            uuid_body = baseline_uuid[start : baseline_uuid.index("\n#[inline]", start)]
            self.assertIn(uuid_body, prepared_uuid)

    def test_customizations_do_not_change_wire_protocol(self):
        from customizations import upstream, identity, update_channel, account_login
        from customizations.edits import SourceEdits

        baseline = upstream.load_baseline(ROOT)
        changes = SourceEdits({name: data.decode("utf-8") for name, data in baseline.items()})
        cfg = json.loads((ROOT / "config/client-channel.json").read_text(encoding="utf-8"))
        identity.apply(changes)
        account_login.apply(changes)
        update_channel.apply(changes, cfg)
        for name in changes:
            if name.endswith(".proto"):
                self.assertEqual(changes[name].encode("utf-8"), baseline[name])
        # These two local listener changes must not rewrite shared server port constants.
        for name, old, new in [
            ("src/lan.rs", "(RENDEZVOUS_PORT + 3) as _", "21129"),
            ("src/rendezvous_mediator.rs", "port = RENDEZVOUS_PORT + 2;", "port = 21128;"),
        ]:
            self.assertEqual(changes[name], baseline[name].decode("utf-8").replace(old, new, 1))

    def test_terminal_exec_extends_only_the_peer_message_protocol(self):
        from customizations import upstream, terminal_exec

        baseline = upstream.load_baseline(ROOT)
        changes = {name: data.decode("utf-8") for name, data in baseline.items()}
        terminal_exec.apply(changes, ROOT)
        protocol = "libs/base/protos/message.proto"
        self.assertEqual(
            [name for name in changes if name.endswith(".proto") and changes[name].encode() != baseline.get(name)],
            [protocol],
        )
        original = baseline[protocol].decode()
        extended = changes[protocol]
        for field in (
            "  bool terminal_exec = 4;\n",
            "    TerminalExecRequest exec = 5;\n",
            "    TerminalExecResult exec_result = 5;\n",
        ):
            self.assertEqual(extended.count(field), 1)
            extended = extended.replace(field, "")
        start = extended.index("enum ExecShell {")
        end = extended.index("message TerminalAction {", start)
        self.assertEqual(extended[:start] + extended[end:], original)


@unittest.skipUnless(os.name == "posix", "Linux publisher and packaging tests")
class PublisherTests(unittest.TestCase):
    def setUp(self):
        import publisher

        self.p = publisher
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.portal = self.root / "ROOT/index.html"
        self.portal.parent.mkdir()
        self.portal.write_text(publisher.START + "old client" + publisher.END)
        self.public = self.portal.parent / "downloads/geodomdesk"
        self.seed = self.root / "seed"
        self.seed.write_bytes(bytes(range(32)))
        self.config = {
            "state_dir": str(self.state),
            "public_dir": str(self.public),
            "portal_file": str(self.portal),
            "repository": "owner/GeodomDesk",
            "enabled": True,
            "public_key": base64.b64encode(channel.public_key(self.seed.read_bytes())).decode(),
            "signing_key": str(self.seed),
            "host": "www.qcjcpt.com",
            "relay": "",
            "api": "",
            "key": "AKJROlsG4WAI103ADqoVrBs4Nb7+6dpbHYVO+MVd81Y=",
            "websocket_port": 21114,
        }

    def tearDown(self):
        self.temp.cleanup()

    def build_fixture(self, version="1.5.2", build_number=0):
        release_id = channel.make_release_id(version, build_number) if build_number else version
        release = {"tag_name": version, "published_at": "2026-11-04T00:00:00Z", "assets": []}
        build = {
            "schema_version": 1,
            "app": "GeodomDesk",
            "native_config": True,
            "package_id": "geodomdesk",
            "repository": self.config["repository"],
            "version": version,
            "base_url": channel.BASE_URL,
            "key_id": channel.KEY_ID,
            "public_key": self.config["public_key"],
            "upstream_commit": "a" * 40,
            "artifacts": [],
        }
        build.update(release_id=release_id, build_number=build_number)
        local = {}
        for platform, arch, fmt in channel.TARGETS:
            name = channel.file_name(release_id, platform, fmt)
            path = self.root / name
            path.write_bytes((b"MZ" if fmt == "exe" else b"deb") * 1024)
            if fmt == "apk":
                with zipfile.ZipFile(path, "w") as archive:
                    for entry in [
                        "AndroidManifest.xml",
                        "classes.dex",
                        "lib/arm64-v8a/librustdesk.so",
                    ]:
                        archive.writestr(entry, b"APK fixture")
            build["artifacts"].append(
                {
                    "platform": platform,
                    "arch": arch,
                    "format": fmt,
                    "file_name": name,
                    "size": path.stat().st_size,
                    "sha256": channel.sha256(path),
                }
            )
            local[name] = path
        source = self.root / f"geodomdesk-{release_id}-source.tar.gz"
        source.write_bytes(b"source fixture")
        build["source"] = {
            "file_name": source.name,
            "size": source.stat().st_size,
            "sha256": channel.sha256(source),
        }
        descriptor = self.root / "geodomdesk-build.json"
        descriptor.write_bytes(channel.stable_json(build))
        local.update({source.name: source, descriptor.name: descriptor})
        for n, path in local.items():
            release["assets"].append(
                {
                    "name": n,
                    "size": path.stat().st_size,
                    "digest": "sha256:" + channel.sha256(path),
                    "url": f'https://api.github.com/repos/owner/GeodomDesk/releases/assets/{len(release["assets"])+1}',
                    "browser_download_url": f"https://github.com/owner/GeodomDesk/releases/download/{version}/{n}",
                }
            )
        return release, build, local

    def mocks(self, release, local):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(
                self.p,
                "release_info",
                return_value=(release, "https://api.github.com/repos/owner/GeodomDesk", {}),
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.p, "download", side_effect=lambda asset, *args: local[asset["name"]]
            )
        )
        stack.enter_context(mock.patch.object(self.p, "check_deb"))

        return stack

    def test_disabled_leaves_existing_downloads_and_portal(self):
        self.config["enabled"] = False
        self.p.run(self.config)
        self.assertFalse(self.public.exists())
        self.assertIn("old client", self.portal.read_text())

    def test_publish_signed_metadata_then_pointer(self):
        release, _, local = self.build_fixture()
        with self.mocks(release, local):
            self.p.run(self.config)
            self.p.run(self.config)
        latest = json.loads((self.public / "latest.json").read_text())
        self.assertEqual(latest["url"], channel.release_url("1.5.2"))
        self.assertTrue(latest["android_url"].endswith("geodomdesk-1.5.2-android-arm64.apk"))
        self.assertTrue(latest["windows_url"].endswith("geodomdesk-1.5.2-windows-x64.exe"))
        self.assertTrue(latest["ubuntu_url"].endswith("geodomdesk-1.5.2-ubuntu-amd64.deb"))
        self.assertFalse(
            any(
                "setup" in f.name or f.suffix == ".run"
                for f in (self.public / "releases/1.5.2").iterdir()
            )
        )
        target = self.public / "releases/1.5.2"
        metadata = (target / channel.METADATA_NAME).read_bytes()
        signature = json.loads((target / (channel.METADATA_NAME + ".sig")).read_bytes())
        Ed25519PublicKey.from_public_bytes(channel.public_key(self.seed.read_bytes())).verify(
            base64.b64decode(signature["signature"]), channel.SIGNATURE_CONTEXT + metadata
        )
        self.assertFalse((target / "install-windows.ps1").exists())
        self.assertIn("old client", self.portal.read_text())

    def test_failed_build_does_not_switch_current(self):
        release, _, local = self.build_fixture()
        with self.mocks(release, local):
            self.p.run(self.config)
        before = (self.public / "latest.json").read_bytes()
        release, _, local = self.build_fixture("1.5.3")
        with self.mocks(release, local), mock.patch.object(
            self.p, "check_deb", side_effect=RuntimeError("invalid native package")
        ):
            with self.assertRaises(RuntimeError):
                self.p.run(self.config)
        self.assertEqual(before, (self.public / "latest.json").read_bytes())
        self.assertFalse((self.public / "releases/1.5.3").exists())
        self.assertEqual([], list(self.state.glob("build-*")))

    def test_existing_legacy_links_survive_until_native_release(self):
        release, build, local = self.build_fixture()
        build.pop("native_config")
        descriptor = local["geodomdesk-build.json"]
        descriptor.write_bytes(channel.stable_json(build))
        target = self.public / "releases/1.5.2"
        target.mkdir(parents=True)
        files = []
        for name in self.p.download_names("1.5.2", False).values():
            path = target / name
            path.write_bytes(b"previously verified legacy download")
            files.append(
                {"name": name, "size": path.stat().st_size, "sha256": channel.sha256(path)}
            )
        manifest = {"version": "1.5.2", "build_sha256": channel.sha256(descriptor), "files": files}
        (target / "publication.json").write_bytes(channel.stable_json(manifest))
        with self.mocks(release, local):
            self.p.run(self.config)
        latest = json.loads((self.public / "latest.json").read_text())
        self.assertTrue(latest["windows_url"].endswith("windows-x64-setup.exe"))
        self.assertTrue(latest["ubuntu_url"].endswith("ubuntu-amd64.run"))
        self.assertFalse(latest["native_config"])
        release, _, local = self.build_fixture("1.5.3")
        native = json.loads(local["geodomdesk-build.json"].read_text())
        native.pop("native_config")
        local["geodomdesk-build.json"].write_bytes(channel.stable_json(native))
        with self.mocks(release, local), self.assertRaises(ValueError):
            self.p.run(self.config)
        self.assertEqual(latest, json.loads((self.public / "latest.json").read_text()))

    def test_cross_mount_publication_and_failed_copy_preserve_atomicity(self):
        real_replace = self.p.os.replace

        def across_mounts(source, destination):
            source = Path(source)
            if source.name == "release" and source.parent.parent == self.state:
                raise OSError(errno.EXDEV, "Separate systemd bind mounts")
            return real_replace(source, destination)

        release, _, local = self.build_fixture()
        with self.mocks(release, local), mock.patch.object(
            self.p.os, "replace", side_effect=across_mounts
        ):
            self.p.run(self.config)
        before = (self.public / "latest.json").read_bytes()
        target = self.public / "releases/1.5.2"
        manifest = json.loads((target / "publication.json").read_text())
        self.assertTrue(self.p.public_complete(target, manifest))
        self.assertEqual([], list(target.parent.glob(".publish-*")))

        release, _, local = self.build_fixture("1.5.3")
        with self.mocks(release, local), mock.patch.object(
            self.p.os, "replace", side_effect=across_mounts
        ), mock.patch.object(self.p.shutil, "copytree", side_effect=OSError(errno.ENOSPC, "Full")):
            with self.assertRaises(OSError):
                self.p.run(self.config)
        self.assertEqual(before, (self.public / "latest.json").read_bytes())
        self.assertFalse((self.public / "releases/1.5.3").exists())
        self.assertEqual([], list(target.parent.glob(".publish-*")))
        self.assertEqual([], list(self.state.glob("build-*")))

    def test_same_official_version_rebuild_and_revision_rollback(self):
        for revision in [101, 102]:
            release, _, local = self.build_fixture("1.5.1", revision)
            with self.mocks(release, local):
                self.p.run(self.config)
            latest = json.loads((self.public / "latest.json").read_text())
            self.assertEqual(latest["version"], "1.5.1")
            self.assertEqual(latest["build_number"], revision)
            self.assertEqual(latest["url"], channel.release_url(f"1.5.1-r{revision}"))
            metadata = json.loads(
                (self.public / f"releases/1.5.1-r{revision}" / channel.METADATA_NAME).read_text()
            )
            self.assertEqual(metadata["version"], "1.5.1")
            self.assertEqual(metadata["release_id"], f"1.5.1-r{revision}")
        before = (self.public / "latest.json").read_bytes()
        release, _, local = self.build_fixture("1.5.1", 101)
        with self.mocks(release, local), self.assertRaises(ValueError):
            self.p.run(self.config)
        self.assertEqual(before, (self.public / "latest.json").read_bytes())

    def test_rejects_stock_build_wrong_key_and_downgrade(self):
        release, build, local = self.build_fixture()
        for field, value in [
            ("app", "RustDesk"),
            ("public_key", "wrong"),
            ("version", "1.5.3"),
            ("repository", "rustdesk/rustdesk"),
        ]:
            changed = {**build, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.p.validate_build(changed, release, self.config)
        self.public.mkdir(parents=True)
        (self.public / "latest.json").write_text('{"version":"1.5.3"}')
        with self.mocks(release, local), self.assertRaises(ValueError):
            self.p.run(self.config)

    def test_asset_origin_digest_and_duplicate_validation(self):
        release, _, _ = self.build_fixture()
        original = copy.deepcopy(release)
        asset = release["assets"][0]
        for field, value in [
            ("digest", ""),
            ("url", "https://example.com/asset"),
            ("browser_download_url", "http://github.com/asset"),
            ("size", True),
        ]:
            release = copy.deepcopy(original)
            release["assets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.p.select_asset(
                    release, asset["name"], "https://api.github.com/repos/owner/GeodomDesk"
                )
        release = copy.deepcopy(original)
        release["assets"].append(release["assets"][0])
        with self.assertRaises(ValueError):
            self.p.select_asset(
                release, asset["name"], "https://api.github.com/repos/owner/GeodomDesk"
            )


if __name__ == "__main__":
    unittest.main()
