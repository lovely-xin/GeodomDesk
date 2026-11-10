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
    #[cfg(windows)]
    if args == ["--terminal-cli", "lifetime"] {
        use std::os::windows::process::CommandExt;
        let child = std::process::Command::new(r"C:\\Windows\\System32\\ping.exe")
            .args(["-t", "127.0.0.1"]).creation_flags(0x08000000)
            .stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null()).spawn().unwrap();
        println!("{}:{}", std::process::id(), child.id());
        std::io::stdout().flush().unwrap();
        loop { std::thread::sleep(std::time::Duration::from_secs(1)); }
    }
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

    @unittest.skipUnless(os.name == "nt", "Windows process job")
    def test_killed_launcher_closes_the_owned_process_tree(self):
        import ctypes
        process = subprocess.Popen([str(self.cli), "lifetime"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handles = []
        try:
            pids = [int(value) for value in process.stdout.readline().decode().strip().split(":")]
            self.assertEqual(len(pids), 2)
            handles = [kernel.OpenProcess(0x100001, False, pid) for pid in pids]
            self.assertTrue(all(handles))
            process.kill()
            process.wait(timeout=8)
            self.assertTrue(all(kernel.WaitForSingleObject(handle, 2500) == 0 for handle in handles))
        finally:
            if process.poll() is None:
                process.kill()
            for handle in handles:
                if handle:
                    if kernel.WaitForSingleObject(handle, 0) != 0:
                        kernel.TerminateProcess(handle, 1)
                    kernel.CloseHandle(handle)

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
