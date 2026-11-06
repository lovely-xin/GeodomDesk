"""Run native packaging tools against inert fixtures; never installs a client."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

import repack_deb


class PrinterBundleTests(unittest.TestCase):
    def test_required_components_are_verified_and_bad_archives_fail(self):
        import prepare_printer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            cache = root / "cache"
            cache.mkdir()
            assets = []
            for name, member in [
                ("rustdesk_printer_driver_v4-1.4.zip", "rustdesk_printer_driver_v4-1.4/RustDeskPrinterDriver.inf"),
                ("printer_driver_adapter.zip", "printer_driver_adapter.dll"),
            ]:
                path = cache / name
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(member, b"inert printer fixture")
                assets.append({"file_name": name, "url": "https://invalid.example/", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            (root / "config/printer-assets.json").write_text(json.dumps({"assets": assets}))
            with mock.patch.object(prepare_printer, "ROOT", root):
                prepare_printer.prepare(root / "bundle", cache)
                self.assertTrue((root / "bundle/printer_driver_adapter.dll").is_file())
                self.assertTrue((root / "bundle/drivers/RustDeskPrinterDriver/RustDeskPrinterDriver.inf").is_file())
                (cache / assets[0]["file_name"]).write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    prepare_printer.prepare(root / "bundle", cache)


@unittest.skipUnless(
    os.name == "posix" and shutil.which("dpkg-deb"), "Linux packaging tools required"
)
class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="geodomdesk-package-test-")
        self.root = Path(self.temp.name)
        self.config = {
            "host": "www.qcjcpt.com",
            "relay": "",
            "api": "",
            "websocket_port": 21114,
            "key": "AKJROlsG4WAI103ADqoVrBs4Nb7+6dpbHYVO+MVd81Y=",
        }

    def tearDown(self):
        self.temp.cleanup()

    def stub_deb(self):
        package = self.root / "input"
        (package / "DEBIAN").mkdir(parents=True)
        (package / "DEBIAN/control").write_text(
            "Package: rustdesk\nVersion: 1.5.2\nArchitecture: amd64\nMaintainer: Fixture <fixture@example.com>\nDescription: Inert packaging test\nConflicts: rustdesk-other\n"
        )
        original = package / "usr/share/rustdesk/rustdesk"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"#!/bin/sh\nexit 99\n")
        original.chmod(0o755)
        (package / "DEBIAN/preinst").write_text(
            "#!/bin/sh\n# Must be discarded; never executed.\nservice rustdesk stop\n"
        )
        (package / "DEBIAN/preinst").chmod(0o755)
        payload = self.root / "input.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "-b", str(package), str(payload)],
            check=True,
            capture_output=True,
        )
        return payload

    def test_deb_isolated_paths_and_service_scripts(self):
        from publisher import check_deb

        payload = self.stub_deb()
        output = self.root / "geodomdesk.deb"
        repack_deb.repack(payload, output, "1.5.2", 101)
        check_deb(output, "1.5.2", 101)
        extracted = self.root / "unpacked"
        subprocess.run(["dpkg-deb", "-R", str(output), str(extracted)], check=True)
        self.assertEqual(
            os.readlink(extracted / "usr/bin/geodomdesk"), "/usr/share/geodomdesk/geodomdesk"
        )
        self.assertFalse((extracted / "usr/bin/rustdesk").exists())
        self.assertFalse((extracted / "DEBIAN/preinst").exists())
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        log = self.root / "systemctl.log"
        fake = fakebin / "systemctl"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"\n')
        fake.chmod(0o755)
        env = {
            **os.environ,
            "PATH": str(fakebin) + ":" + os.environ["PATH"],
            "TEST_SYSTEMCTL_LOG": str(log),
        }
        subprocess.run(["sh", str(extracted / "DEBIAN/postinst"), "configure"], check=True, env=env)
        subprocess.run(["sh", str(extracted / "DEBIAN/prerm"), "remove"], check=True, env=env)
        self.assertEqual(
            log.read_text().splitlines(),
            [
                "daemon-reload",
                "enable --now geodomdesk.service",
                "stop geodomdesk.service",
                "disable geodomdesk.service",
            ],
        )

    def test_source_archive_includes_new_modules_and_baseline(self):
        import tarfile
        import archive_source

        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "Cargo.toml").write_text('[package]\nname="fixture"\n')
        subprocess.run(["git", "-C", str(source), "add", "Cargo.toml"], check=True)
        for name in ["geodomdesk_update.rs", "geodomdesk_auth.rs", "geodomdesk_identity.rs"]:
            (source / "src").mkdir(exist_ok=True)
            (source / "src" / name).write_text("// untracked generated module\n")
        (source / "src/geodomdesk_import").mkdir()
        (source / "src/geodomdesk_import.rs").write_text("mod data;\n")
        (source / "src/geodomdesk_import/data.rs").write_text("// import parser\n")
        terminal_sources = [
            "src/bin/geodomdesk-cli.rs",
            "src/terminal_exec.rs",
            "src/terminal_exec/cli.rs",
            "src/terminal_exec/exec_bootstrap.ps1",
            "src/terminal_exec/windows.rs",
        ]
        for name in terminal_sources:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("terminal fixture\n")
        binding = source / "libs/hbb_common/src/geodomdesk_clone_guard.rs"
        binding.parent.mkdir(parents=True)
        binding.write_text("// clone guard\n")
        (binding.parent / "geodomdesk_id_policy.rs").write_text("// default ID metadata\n")
        logo = source / "flutter/assets/geodom-logo.png"
        logo.parent.mkdir(parents=True)
        logo.write_bytes(b"fixture-logo")
        custom = self.root / "custom"
        (custom / "reference").mkdir(parents=True)
        (custom / "reference/Cargo.toml").write_text("reviewed baseline")
        output = self.root / "source.tar.gz"
        archive_source.archive(source, custom, output)
        with tarfile.open(output) as archive:
            names = archive.getnames()
        for name in [
            "src/geodomdesk_update.rs",
            "src/geodomdesk_auth.rs",
            "src/geodomdesk_identity.rs",
            "libs/hbb_common/src/geodomdesk_id_policy.rs",
            "customization/reference/Cargo.toml",
            "src/geodomdesk_import.rs",
            "src/geodomdesk_import/data.rs",
            "libs/hbb_common/src/geodomdesk_clone_guard.rs",
            "flutter/assets/geodom-logo.png",
            *terminal_sources,
        ]:
            self.assertIn("geodomdesk-client/" + name, names)


if __name__ == "__main__":
    unittest.main()
