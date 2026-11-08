"""Exercise the real launcher with inherited streams, without remote access."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("rustc"), "Rust compiler required")
class TerminalCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="terminal cli 中文 ")
        cls.root = Path(cls.temp.name)
        suffix = ".exe" if os.name == "nt" else ""
        cls.cli = cls.root / ("geodomdesk-cli" + suffix)
        cls.main = cls.root / ("GeodomDesk.exe" if os.name == "nt" else "geodomdesk")
        source = Path(__file__).resolve().parents[1] / "client/bin/geodomdesk-cli.rs"
        subprocess.run(["rustc", "--edition=2021", str(source), "-o", str(cls.cli)], check=True)
        fixture = cls.root / "main.rs"
        fixture.write_text('''#![cfg_attr(windows, windows_subsystem = "windows")]
use std::io::{Read, Write};
fn main() {
    #[cfg(windows)]
    unsafe {
        #[link(name = "kernel32")]
        extern "system" { fn AttachConsole(pid: u32) -> i32; }
        AttachConsole(u32::MAX);
    }
    let args: Vec<_> = std::env::args().skip(1).collect();
    assert_eq!(args, ["--terminal-cli", "exec", "--script", "脚本 with spaces.ps1"]);
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    std::io::stdout().write_all(input.as_bytes()).unwrap();
    std::io::stderr().write_all("stderr=中文\\n".as_bytes()).unwrap();
    std::process::exit(37);
}
''', encoding="utf-8")
        subprocess.run(["rustc", "--edition=2021", str(fixture), "-o", str(cls.main)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_streams_arguments_and_exit_code(self):
        result = subprocess.run(
            [str(self.cli), "exec", "--script", "脚本 with spaces.ps1"],
            input="stdout=中文\n".encode(), capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, "stdout=中文\n".encode())
        self.assertEqual(result.stderr, "stderr=中文\n".encode())

    def test_missing_main_fails_without_searching_path(self):
        isolated = self.root / "isolated"
        isolated.mkdir()
        cli = isolated / self.cli.name
        shutil.copy2(self.cli, cli)
        result = subprocess.run([str(cli), "--help"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"cannot run GeodomDesk", result.stderr)
        self.assertEqual(result.stdout, b"")

    @unittest.skipIf(os.name == "nt", "Unix package symlink")
    def test_package_symlink_locates_bundled_main(self):
        link = self.root / "bin" / self.cli.name
        link.parent.mkdir()
        link.symlink_to(self.cli)
        result = subprocess.run(
            [str(link), "exec", "--script", "脚本 with spaces.ps1"],
            input=b"package\n", capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, b"package\n")
