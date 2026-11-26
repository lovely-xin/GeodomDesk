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

from packaging import prepare_client
from release import channel

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
        from release import publisher

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
            expected_ranges = (len(data) + publisher.RANGE_CHUNK_BYTES - 1) // publisher.RANGE_CHUNK_BYTES
            self.assertEqual(len(requests), 1 + expected_ranges)
            self.assertTrue(requests[0].get_header("Authorization"))
            self.assertTrue(all(not r.get_header("Authorization") for r in requests[1:]))
            with mock.patch.object(
                publisher.urllib.request,
                "build_opener",
                side_effect=AssertionError("Unexpected download"),
            ):
                self.assertEqual(publisher.download(asset, cache, {}), result)

    def test_range_retries_refresh_expired_redirect_and_prune_cache(self):
        from release import publisher

        data = b"R" * (publisher.RANGE_CHUNK_BYTES + 128)
        calls = []

        class Response(io.BytesIO):
            status = 206

            def __init__(self, start, end, redirect_url):
                super().__init__(data[start : end + 1])
                self.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
                self._url = redirect_url

            def geturl(self):
                return self._url

        class Opener:
            def __init__(self):
                self.version = 1
                self.failed_once = False

            def open(self, request, **kwargs):
                url = request.full_url
                calls.append(url)
                start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
                if url.startswith("https://api.github.com/"):
                    return Response(start, end, f"https://cdn.example/v{self.version}")
                if not self.failed_once and start > 0:
                    self.failed_once = True
                    self.version = 2
                    raise publisher.urllib.error.HTTPError(
                        url, 403, "Expired redirect", {}, None
                    )
                return Response(start, end, url)

        with tempfile.TemporaryDirectory(prefix="geodomdesk-retry-test-") as tmp:
            cache = Path(tmp)
            asset = {
                "name": "fixture.exe",
                "url": "https://api.github.com/repos/owner/repo/releases/assets/9",
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
            with mock.patch.object(publisher.urllib.request, "build_opener", return_value=Opener()):
                result = publisher.download(asset, cache, {"Authorization": "Bearer test-only"})
            self.assertEqual(result.read_bytes(), data)
            self.assertGreaterEqual(sum(u.startswith("https://api.github.com/") for u in calls), 2)
            stale = []
            for idx in range(6):
                p = cache / f"stale-{idx}.bin"
                p.write_bytes(b"old")
                os.utime(p, (1000 + idx, 1000 + idx))
                stale.append(p)
            interrupted = cache / ".download-test_42"
            interrupted.write_bytes(b"interrupted")
            publisher.prune_cache(cache, [result], max_extra=2)
            self.assertFalse(interrupted.exists())
            self.assertTrue(result.exists())
            self.assertEqual(sorted(p.name for p in cache.glob("stale-*.bin")), ["stale-4.bin", "stale-5.bin"])


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

    def test_changelog_normalization_and_signed_metadata(self):
        from release import release_bundle

        dirty = "\r\n\x00- 修复连接\r\n\r\n\r\n- 提升速度\x1b[0m   \n"
        self.assertEqual(channel.normalize_changelog(dirty), "- 修复连接\n\n- 提升速度[0m")

        cn_long = "更新说明测试" * 1000
        bounded = channel.normalize_changelog(cn_long)
        self.assertLessEqual(len(bounded.encode("utf-8")), channel.CHANGELOG_MAX_BYTES)
        self.assertTrue(bounded.endswith("..."))

        md = """# 更新日志

## 1.5.4
- 下一版本内容

## v1.5.3 (2026-11-26)
- 新增更新日志弹窗确认
- 优化端到端性能

## 1.5.2
- 历史版本
"""
        self.assertEqual(
            channel.extract_changelog_section(md, "1.5.3"),
            "- 新增更新日志弹窗确认\n- 优化端到端性能",
        )
        self.assertEqual(channel.extract_changelog_section(md), "- 下一版本内容")
        self.assertEqual(channel.extract_changelog_section(md, "1.6.0", "1.6.0-r1"), "")

        md_rev = """## 1.5.3-r99
- 仅 r99 内容

## 1.5.3-r12
- 构建 12 修复

## 1.5.3
- 通用 1.5.3 说明
"""
        self.assertEqual(
            channel.extract_changelog_section(md_rev, "1.5.3", release_id="1.5.3-r12"),
            "- 构建 12 修复",
        )
        self.assertEqual(
            channel.extract_changelog_section(md_rev, "1.5.3", release_id="1.5.3-r1"),
            "- 通用 1.5.3 说明",
        )

        seed = bytes(range(32))
        metadata_plain, _ = channel.signed_metadata(
            "1.5.3-r2", "2026-11-26T00:00:00Z", self.metadata_for("1.5.3-r2"), seed
        )
        self.assertNotIn("changelog", json.loads(metadata_plain))

        metadata_notes, signature_notes = channel.signed_metadata(
            "1.5.3-r2",
            "2026-11-26T00:00:00Z",
            self.metadata_for("1.5.3-r2"),
            seed,
            changelog="  - 新增更新日志\r\n- 优化速度  ",
        )
        parsed = json.loads(metadata_notes)
        self.assertEqual(parsed["changelog"], "- 新增更新日志\n- 优化速度")
        Ed25519PublicKey.from_public_bytes(channel.public_key(seed)).verify(
            base64.b64decode(json.loads(signature_notes)["signature"]),
            channel.SIGNATURE_CONTEXT + metadata_notes,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = []
            for platform, arch, fmt in channel.TARGETS:
                p = root / f"{platform}.{fmt}"
                p.write_bytes(b"X" * 1_100_000)
                artifacts.append(p)
            src = root / "src.tar.gz"
            src.write_bytes(b"source-tarball")
            cfg_path = root / "build-config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "app": "GeodomDesk",
                        "package_id": "geodomdesk",
                        "version": "1.5.3",
                        "build_number": 5,
                        "release_id": "1.5.3-r5",
                        "base_url": channel.BASE_URL,
                        "key_id": channel.KEY_ID,
                        "public_key": base64.b64encode(channel.public_key(seed)).decode(),
                        "upstream_commit": "a" * 40,
                        "native_config": True,
                    }
                ),
                encoding="utf-8",
            )
            cl = root / "CHANGELOG.md"
            cl.write_text(md, encoding="utf-8")
            out = root / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                release_bundle.bundle(
                    "1.5.3",
                    "owner/GeodomDesk",
                    artifacts[0],
                    artifacts[1],
                    artifacts[2],
                    src,
                    out,
                    cfg_path,
                    changelog_file=cl,
                )
            built = json.loads((out / "geodomdesk-build.json").read_text(encoding="utf-8"))
            self.assertEqual(built["changelog"], "- 新增更新日志弹窗确认\n- 优化端到端性能")

    def metadata_for(self, release_id):
        items = []
        for platform, arch, fmt in channel.TARGETS:
            name = channel.file_name(release_id, platform, fmt)
            items.append(
                {
                    "platform": platform,
                    "arch": arch,
                    "format": fmt,
                    "file_name": name,
                    "url": channel.release_url(release_id) + name,
                    "size": 1234,
                    "sha256": "a" * 64,
                }
            )
        return items

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
            self.assertIn('("enable-webrtc".to_owned(), "Y".to_owned())', default_settings)
            self.assertIn('("enable-tcp-punch".to_owned(), "N".to_owned())', default_settings)
            self.assertIn('("enable-ipv6-punch".to_owned(), "Y".to_owned())', default_settings)
            self.assertNotIn("https://rustdesk.com/docs/en/", config)
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
            gradle = (source / "flutter/android/app/build.gradle").read_text(encoding="utf-8")
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
            bad_text = bad.read_text(encoding="utf-8")
            self.assertIn(".get(crate::geodomdesk_update::LATEST_URL)", bad_text)
            self.assertIn('if response.url().scheme() != "https"', bad_text)
            self.assertNotIn("client.post(&url).json(&request)", bad_text)
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
            self.assertIn(
                '"GEODOMDESK_RUNNER_WIN32_WINDOW"',
                windows,
            )
            common_rs = bad.read_text(encoding="utf-8")
            self.assertLess(
                common_rs.index("*SOFTWARE_UPDATE_URL.lock().unwrap() = response_url.clone();"),
                common_rs.index("push_global_event(crate::flutter::APP_TYPE_MAIN, data)"),
            )
            self.assertIn(
                "let disable_nagle = true;",
                (source / "libs/hbb_common/src/websocket.rs").read_text(encoding="utf-8"),
            )
            mux_rs = (source / "src/port_forward_mux.rs").read_text(encoding="utf-8")
            self.assertIn("pub const CHANNEL_WINDOW: u32 = 1024 * 1024;", mux_rs)
            self.assertIn("pub const UPDATE_THRESHOLD: u32 = 128 * 1024;", mux_rs)
            login_dart = (source / "flutter/lib/common/widgets/login.dart").read_text(encoding="utf-8")
            self.assertNotIn("if (!isWeb && close != null) close(false);", login_dart)
            self.assertGreaterEqual(login_dart.count("if (res == true) {"), 2)
            self.assertGreaterEqual(login_dart.count("close(true);"), 2)
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
            self.assertIn("SOFTWARE_UPDATE_INFO", common_rs)
            self.assertIn("check_software_update_manual", common_rs)
            self.assertIn("MAX_POINTER_BYTES", common_rs)
            self.assertIn(
                "crate::geodomdesk_update::current_release_id(crate::VERSION)",
                (source / "src/ui_interface.rs").read_text(encoding="utf-8"),
            )
            self.assertIn('"software-update-info"', ffi)
            self.assertIn('"check-software-update"', ffi)
            self.assertIn('|| key == "check-software-update"', ffi)
            dialog_widget = (source / "flutter/lib/common/widgets/geodom_update_dialog.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("showGeodomUpdateDialog", dialog_widget)
            self.assertIn("checkGeodomUpdateManually", dialog_widget)
            self.assertIn("desktopType != DesktopType.main", dialog_widget)
            self.assertNotIn("kWindowType", dialog_widget)
            self.assertNotIn("multi_window_manager.dart", dialog_widget)
            common_dart = (source / "flutter/lib/common.dart").read_text(encoding="utf-8")
            self.assertIn("handleGeodomUpdateCheckEvent(evt)", common_dart)
            home_dart = (source / "flutter/lib/desktop/pages/desktop_home_page.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("showGeodomUpdateDialogFromCurrent(updateUrl)", home_dart)
            self.assertIn("checkGeodomUpdateManually()", desktop_settings)
            self.assertIn("infoRow(\n                translate('Version'),", desktop_settings)
            self.assertNotIn("OutlinedButton.icon", desktop_settings)
            self.assertIn("recentSessionsSync()", desktop_settings)
            self.assertIn("Sync recent devices and credentials", desktop_settings)
            recent_sync_dart = (source / "flutter/lib/models/geodom_recent_sync.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("/api/recent/peers", recent_sync_dart)
            self.assertIn("/api/recent/peer", recent_sync_dart)
            self.assertNotIn("/api/ab/peer/", recent_sync_dart)
            self.assertIn("http.HttpService(reuseConnection: true)", recent_sync_dart)
            self.assertIn("service.close();", recent_sync_dart)
            http_service = (source / "flutter/lib/utils/http_service.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("_client ??= http.Client()", http_service)
            self.assertIn("await _pollForResponse(url.toString())", http_service)
            ab_widget_dart = (source / "flutter/lib/common/widgets/address_book.dart").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("Sync recent devices and credentials", ab_widget_dart)
            ab_model = (source / "flutter/lib/models/ab_model.dart").read_text(encoding="utf-8")
            self.assertIn("recentDevices.supported.value || !shouldSyncAb()", ab_model)
            self.assertIn("recentDevices.supported.value || recents.isEmpty", ab_model)
            peer_card = (source / "flutter/lib/common/widgets/peer_card.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("!dedicated || tab == PeerTabIndex.ab", peer_card)
            self.assertIn('if _key == "geodom-recent-cache"', ffi)
            mobile_conn = (source / "flutter/lib/mobile/pages/connection_page.dart").read_text(
                encoding="utf-8"
            )
            self.assertIn("showGeodomUpdateDialogFromCurrent(url)", mobile_conn)
            self.assertIn("checkGeodomUpdateManually()", mobile_settings)
            self.assertIn("await bind.mainGetVersion()", mobile_settings)
            cn_rs = (source / "src/lang/cn.rs").read_text(encoding="utf-8")
            self.assertIn('("New version available", "发现新版本")', cn_rs)
            self.assertIn('("Check for updates", "检查更新")', cn_rs)
            self.assertIn('("New version available", "")', german.read_text(encoding="utf-8"))
            baseline_uuid = (ROOT / "reference/libs/hbb_common/src/lib.rs").read_text(
                encoding="utf-8"
            )
            prepared_uuid = (source / "libs/hbb_common/src/lib.rs").read_text(encoding="utf-8")
            start = baseline_uuid.index("pub fn get_uuid() -> Vec<u8> {")
            uuid_body = baseline_uuid[start : baseline_uuid.index("\n#[inline]", start)]
            self.assertIn(uuid_body, prepared_uuid)
            self.assertIn(
                "pub(crate) mod geodomdesk_relay_probe;",
                (source / "src/lib.rs").read_text(encoding="utf-8"),
            )
            self.assertTrue((source / "src/geodomdesk_relay_probe.rs").is_file())
            self.assertTrue((source / "src/geodomdesk_relay_probe/logic.rs").is_file())
            mediator = (source / "src/rendezvous_mediator.rs").read_text(encoding="utf-8")
            self.assertLess(
                mediator.index("crate::geodomdesk_relay_probe::start();"),
                mediator.index("if config::is_outgoing_only()"),
            )
            self.assertGreaterEqual(
                mediator.count("crate::geodomdesk_relay_probe::notify_network_change();"),
                2,
            )
            self.assertIn('/api/relay/probe")', common_rs)
            relay_rs = (source / "src/client/relay.rs").read_text(encoding="utf-8")
            self.assertIn("connect_relay_socket(&relay_server, ipv4, false)", relay_rs)
            self.assertIn("connect_relay_socket(&relay_server, ipv4, true)", relay_rs)
            artifact_rs = (source / "src/updater/artifact.rs").read_text(encoding="utf-8")
            self.assertIn('"windows-legacy"', artifact_rs)
            self.assertNotIn("windows-server-2008-r2", artifact_rs)
            self.assertIn(
                'if crate::updater::current_update_platform() == "windows-legacy"',
                common_rs,
            )

    def test_customizations_do_not_change_wire_protocol(self):
        from patches import upstream, identity, update_channel, account_login
        from patches.edits import SourceEdits

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
        from patches import upstream, terminal_exec

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
        from release import publisher

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

    def build_fixture(self, version="1.5.2", build_number=0, extra_targets=()):
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
        targets = channel.TARGETS + tuple(extra_targets)
        for platform, arch, fmt in targets:
            name = channel.file_name(release_id, platform, fmt)
            path = self.root / name
            if (platform, arch, fmt) == channel.WINDOWS_LEGACY:
                path.write_bytes(b"MZ-legacy-compatibility-pack")
            else:
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
        self.assertNotIn("<summary>更新日志</summary>", (self.public / "index.html").read_text(encoding="utf-8"))
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

    def test_mainline_release_excludes_legacy_windows_and_stops_legacy_updates(self):
        mainline = [{"platform": p, "arch": a, "format": f} for p, a, f in channel.TARGETS]
        self.assertEqual(channel.release_targets(mainline), channel.TARGETS)
        with self.assertRaises(ValueError):
            channel.release_targets(mainline + [{"platform": "windows-legacy", "arch": "x64", "format": "exe"}])
        with self.assertRaises(ValueError):
            channel.release_targets(
                mainline + [{"platform": "windows-server-2008-r2", "arch": "x64", "format": "exe"}]
            )
        legacy_release, _, legacy_local = self.build_fixture(
            extra_targets=(channel.WINDOWS_LEGACY,)
        )
        with self.mocks(legacy_release, legacy_local):
            with self.assertRaises(ValueError):
                self.p.run(self.config)

        release, _, local = self.build_fixture(version="1.5.3")
        with self.mocks(release, local):
            self.p.run(self.config)

        latest = json.loads((self.public / "latest.json").read_text())
        self.assertNotIn("windows_server_2008_r2_url", latest)
        self.assertNotIn("windows_legacy_url", latest)

        target = self.public / "releases/1.5.3"
        metadata = (target / channel.METADATA_NAME).read_bytes()
        signature = json.loads((target / (channel.METADATA_NAME + ".sig")).read_bytes())
        Ed25519PublicKey.from_public_bytes(channel.public_key(self.seed.read_bytes())).verify(
            base64.b64decode(signature["signature"]), channel.SIGNATURE_CONTEXT + metadata
        )
        packages = {item["platform"]: item for item in json.loads(metadata)["artifacts"]}
        self.assertEqual(set(packages), {"windows", "linux", "android"})

        release_index = (target / "index.html").read_text()
        self.assertIn("Windows 10 / 11 · Server 2016+", release_index)
        self.assertNotIn("windows-legacy", release_index)
        self.assertNotIn("已停止更新", release_index)

    def test_frozen_legacy_windows_link_stays_decoupled_from_update_pointer(self):
        with self.assertRaises(ValueError):
            self.p.validate_legacy_windows(
                {
                    "legacy_windows": {
                        "url": "https://evil.example/geodomdesk-1.5.1-r9-windows-legacy-x64.exe",
                        "version": "1.5.1-r9",
                    }
                }
            )

        legacy = {
            "url": channel.BASE_URL + "/releases/1.5.1-r9/geodomdesk-1.5.1-r9-windows-legacy-x64.exe",
            "version": "1.5.1-r9",
        }
        config = dict(self.config, legacy_windows=legacy)
        release, _, local = self.build_fixture(version="1.5.3")
        with self.mocks(release, local):
            self.p.run(config)

        latest_text = (self.public / "latest.json").read_text()
        latest = json.loads(latest_text)
        self.assertNotIn("windows_server_2008_r2_url", latest)
        self.assertNotIn("windows_legacy_url", latest)
        self.assertNotIn(legacy["url"], latest_text)

        target = self.public / "releases/1.5.3"
        metadata_text = (target / channel.METADATA_NAME).read_text()
        packages = {item["platform"]: item for item in json.loads(metadata_text)["artifacts"]}
        self.assertEqual(set(packages), {"windows", "linux", "android"})
        self.assertNotIn(legacy["url"], metadata_text)

        for index_path in (target / "index.html", self.public / "index.html"):
            page_html = index_path.read_text(encoding="utf-8")
            self.assertEqual(page_html.count("<strong>Windows</strong>"), 1)
            self.assertIn(f'href="{legacy["url"]}" download', page_html)
            self.assertIn(">标准版</a>", page_html)
            self.assertIn(">兼容版</a>", page_html)
            self.assertNotIn("已停止更新", page_html)

        self.p.portal_update(self.portal, "1.5.3", legacy_windows=legacy)
        portal_text = self.portal.read_text()
        self.assertIn(legacy["url"], portal_text)
        self.assertIn('aria-label="下载 Windows 标准版">标准版', portal_text)
        self.assertIn('aria-label="下载 Windows 旧系统兼容版">兼容版', portal_text)
        self.assertNotIn("Server 2008 R2–2012 R2", portal_text)
        self.assertNotIn("已停止更新", portal_text)

        config_updated = dict(
            self.config,
            activate_portal=True,
            legacy_windows={
                "url": channel.BASE_URL + "/releases/1.5.2-r1/geodomdesk-1.5.2-r1-windows-legacy-x64.exe",
                "version": "1.5.2-r1",
            },
        )
        with self.mocks(release, local):
            self.p.run(config_updated)
        self.assertIn("1.5.2-r1", self.portal.read_text())

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

    def test_unchanged_release_skips_pointer_rewrite_and_failure_writes_locked_status(self):
        release, _, local = self.build_fixture("1.5.2", 5)
        with self.mocks(release, local):
            self.p.run(self.config)
        with self.mocks(release, local), mock.patch.object(
            self.p, "publish_pointer", side_effect=AssertionError("Unexpected pointer rewrite")
        ):
            self.p.run(self.config)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["status"], "published")

        latest_path = self.public / "latest.json"
        latest_data = json.loads(latest_path.read_text())
        latest_data["build_number"] = None
        latest_path.write_text(json.dumps(latest_data))

        bad_release, _, bad_local = self.build_fixture("1.5.3", 6)
        with self.mocks(bad_release, bad_local), mock.patch.object(
            self.p, "check_deb", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.p.run(self.config)
        failed = json.loads((self.state / "status.json").read_text())
        self.assertEqual(failed["status"], "failed")

    def test_publish_changelog_in_metadata_and_collapsible_download_sections(self):
        release, build, local = self.build_fixture("1.5.3", 12)
        build["changelog"] = "- 新增更新日志弹窗确认\n- 修复问题 <script>alert(1)</script>"
        local["geodomdesk-build.json"].write_bytes(channel.stable_json(build))
        for asset in release["assets"]:
            if asset["name"] == "geodomdesk-build.json":
                asset["size"] = local["geodomdesk-build.json"].stat().st_size
                asset["digest"] = "sha256:" + channel.sha256(local["geodomdesk-build.json"])
        with self.mocks(release, local):
            self.p.run(self.config)
        latest = json.loads((self.public / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["changelog"], build["changelog"])
        self.assertIn("published_at", latest)
        target = self.public / "releases/1.5.3-r12"
        metadata_bytes = (target / channel.METADATA_NAME).read_bytes()
        signature = json.loads((target / (channel.METADATA_NAME + ".sig")).read_bytes())
        Ed25519PublicKey.from_public_bytes(channel.public_key(self.seed.read_bytes())).verify(
            base64.b64decode(signature["signature"]), channel.SIGNATURE_CONTEXT + metadata_bytes
        )
        self.assertEqual(json.loads(metadata_bytes)["changelog"], build["changelog"])
        release_html = (target / "index.html").read_text(encoding="utf-8")
        self.assertIn("1.5.3-r12", release_html)
        index_path = self.public / "index.html"
        landing = index_path.read_text(encoding="utf-8")
        for page_html in (landing, release_html):
            self.assertIn('<details class="changelog" open><summary>更新日志</summary>', page_html)
            self.assertIn("新增更新日志弹窗确认", page_html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page_html)
            self.assertNotIn("<script>", page_html)
        self.assertIn('href="releases/1.5.3-r12/geodomdesk-1.5.3-r12-windows-x64.exe"', landing)
        self.assertIn('src="geodomdesk.png"', landing)
        self.assertIn('src="../../geodomdesk.png"', release_html)
        self.assertIn('href="geodomdesk-1.5.3-r12-windows-x64.exe"', release_html)

        # Repair landing links for an already published revision without replacing its pointer or files.
        self.config["activate_portal"] = True
        latest_bytes = (self.public / "latest.json").read_bytes()
        index_path.write_text("Previous download redirect", encoding="utf-8")
        with self.mocks(release, local), mock.patch.object(
            self.p.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)
        ), mock.patch.object(self.p, "publish_pointer", side_effect=AssertionError("Pointer rewritten")):
            self.p.run(self.config)
        self.config["activate_portal"] = False
        portal_html = self.portal.read_text(encoding="utf-8")
        self.assertIn("GeodomDesk 1.5.3-r12", portal_html)
        self.assertNotIn("更新日志", portal_html)
        self.assertNotIn("#changelog-title", portal_html)
        self.assertEqual(index_path.read_text(encoding="utf-8"), landing)
        self.assertEqual((self.public / "latest.json").read_bytes(), latest_bytes)
        self.assertEqual((target / channel.METADATA_NAME).read_bytes(), metadata_bytes)
        self.assertEqual((target / "index.html").read_text(encoding="utf-8"), release_html)

        # Pointer changelog or published_at drift forces pointer refresh on subsequent run.
        latest_path = self.public / "latest.json"
        drifted = json.loads(latest_path.read_text(encoding="utf-8"))
        drifted.pop("changelog", None)
        drifted["published_at"] = "2020-01-01T00:00:00Z"
        latest_path.write_text(json.dumps(drifted), encoding="utf-8")
        with self.mocks(release, local):
            self.p.run(self.config)
        refreshed = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["changelog"], build["changelog"])
        self.assertEqual(refreshed["published_at"], release["published_at"])
        self.assertEqual(
            json.loads(metadata_bytes)["published_at"],
            refreshed["published_at"],
        )

    def test_publisher_ignores_default_release_boilerplate(self):
        release, _, local = self.build_fixture("1.5.4", 1)
        release["body"] = (
            "Follows the official RustDesk version with GeodomDesk customization. "
            "Immutable custom build assets are listed in geodomdesk-build.json."
        )
        with self.mocks(release, local):
            self.p.run(self.config)
        latest = json.loads((self.public / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("changelog", latest)
        target = self.public / "releases/1.5.4-r1"
        metadata = json.loads((target / channel.METADATA_NAME).read_text(encoding="utf-8"))
        self.assertNotIn("changelog", metadata)


if __name__ == "__main__":
    unittest.main()
