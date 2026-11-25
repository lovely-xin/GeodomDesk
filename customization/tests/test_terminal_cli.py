"""Exercise the real launcher with inherited streams, without remote access."""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


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
        if os.name == "nt":
            cls.job_tests = cls.root / "job-tests.exe"
            subprocess.run(
                ["rustc", "--edition=2021", "--test", str(source), "-o", str(cls.job_tests)],
                check=True,
            )

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
    def test_inherited_job_policy(self):
        subprocess.run([str(self.job_tests)], check=True, timeout=10)

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

    def test_ssh_askpass_accepts_password_and_rejects_passphrase_or_host_key(self):
        env = {
            **os.environ,
            "GEODOMDESK_CLI_SSH_ASKPASS": "1",
            "GEODOMDESK_SSH_PASSWORD": "secret-pass",
        }
        ok = subprocess.run(
            [str(self.cli), "user@host's Password: "],
            env=env, capture_output=True, timeout=10,
        )
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(ok.stdout, b"secret-pass\n")

        for prompt in [
            "Enter passphrase for key '/home/user/.ssh/id_ed25519': ",
            "Are you sure you want to continue connecting (yes/no)?",
            "ECDSA key fingerprint is SHA256:abc.",
        ]:
            rejected = subprocess.run(
                [str(self.cli), prompt],
                env=env, capture_output=True, timeout=10,
            )
            self.assertNotEqual(rejected.returncode, 0, prompt)
            self.assertEqual(rejected.stdout, b"")

    @unittest.skipIf(os.name == "nt", "Bash worker script")
    def test_ssh_worker_allows_large_files_caps_streams_and_marks_timeouts(self):
        worker = (
            Path(__file__).resolve().parents[1]
            / "client/terminal_exec/ssh_worker.sh"
        ).read_text(encoding="utf-8").replace("__SESSION__", "sess1234", 1)

        def run_case(script_text: str, timeout_secs: int) -> str:
            payload = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
            digest = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
            stdin_data = f"RUN a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 {timeout_secs} {digest}\n{payload}\n"
            proc = subprocess.run(
                ["bash", "-c", worker],
                input=stdin_data, text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

        large_file_out = run_case(
            'tmp=$(mktemp) && dd if=/dev/zero bs=1024 count=1024 of="$tmp" 2>/dev/null && wc -c < "$tmp" && rm -f "$tmp"\n',
            10,
        )
        self.assertIn("sess1234:READY\n", large_file_out)
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:END:0:0\n", large_file_out)

        oversized_out = run_case(
            "dd if=/dev/zero bs=1024 count=300 2>/dev/null | tr '\\0' 'A'\n",
            10,
        )
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:END:0:1\n", oversized_out)

        timeout_out = run_case("sleep 4\n", 1)
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:END:124:2\n", timeout_out)

        shim_dir = self.root / "bsd-base64-shim"
        shim_dir.mkdir(exist_ok=True)
        shim = shim_dir / "base64"
        shim.write_text(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in -w|-b) exit 1 ;; esac\n"
            "done\n"
            "if [ \"${1-}\" = \"-d\" ]; then exec /usr/bin/base64 -d; fi\n"
            "exec /usr/bin/base64 -w 0\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        unwrapped_script = "dd if=/dev/zero bs=1024 count=20 2>/dev/null | tr '\\0' 'B'\n"
        payload = base64.b64encode(unwrapped_script.encode("utf-8")).decode("ascii")
        digest = hashlib.sha256(unwrapped_script.encode("utf-8")).hexdigest()
        stdin_data = f"RUN a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 10 {digest}\n{payload}\n"
        bsd_env = {**os.environ, "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}"}
        bsd_proc = subprocess.run(
            ["bash", "-c", worker],
            input=stdin_data, text=True, capture_output=True, timeout=15, env=bsd_env,
        )
        self.assertEqual(bsd_proc.returncode, 0, bsd_proc.stderr)
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:END:0:0\n", bsd_proc.stdout)
        o_lines = [
            line.split(":O:", 1)[1]
            for line in bsd_proc.stdout.splitlines()
            if ":O:" in line
        ]
        self.assertGreater(len(o_lines), 1)
        for chunk in o_lines:
            self.assertLessEqual(len(chunk), 3072)
        self.assertEqual(base64.b64decode("".join(o_lines)), b"B" * (20 * 1024))

        empty_out = run_case("true\n", 5)
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:START\n", empty_out)
        self.assertIn("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4:END:0:0\n", empty_out)
        self.assertNotIn(":O:", empty_out)
        self.assertNotIn(":E:", empty_out)

        batched_out = run_case("seq 1 150\n", 5)
        batched_o = [
            line.split(":O:", 1)[1]
            for line in batched_out.splitlines()
            if ":O:" in line
        ]
        self.assertEqual(
            base64.b64decode("".join(batched_o)).decode("utf-8"),
            "".join(f"{n}\n" for n in range(1, 151)),
        )
        self.assertIn("timeout -s TERM -k 2", worker)
        self.assertIn("trap 'rm -rf -- \"$gd_dir\"; exit 143' HUP INT TERM", worker)
        self.assertIn("sh -c ", worker)
        legacy_ps1 = (
            Path(__file__).resolve().parents[1] / "client/terminal_exec/legacy_bootstrap.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Flush-GdChunk", legacy_ps1)
        self.assertIn("if ($null -eq $LASTEXITCODE)", legacy_ps1)


class CustomizationContractTests(unittest.TestCase):
    def test_port_forward_mux_retains_kcp_close_reason_for_1_5_3(self):
        root = Path(__file__).resolve().parents[1]
        with mock.patch.object(sys, "path", [str(root), *sys.path]):
            from customizations import performance

        source_file = root / "reference/src/port_forward_mux.rs"
        lifetime_file = root / "reference/src/port_forward.rs"
        tunnel_src = (root / "client/terminal_exec/tunnel.rs").read_text(encoding="utf-8")
        perf_text = (root / "customizations/performance.py").read_text(encoding="utf-8")
        self.assertIn("send_kcp_close_reason(&mut stream).await;", perf_text)
        self.assertIn("kcp: Option<crate::kcp_stream::KcpStream>,", perf_text)
        self.assertIn("TunnelInterface::new(state.clone()),", tunnel_src)
        self.assertIn("TunnelInterface::new(status.clone()),", tunnel_src)
        if source_file.is_file() and lifetime_file.is_file():
            class MapChanges(dict):
                def replace(self, path, old, new):
                    self[path] = self[path].replace(old, new, 1)

            changes = MapChanges({
                "src/port_forward_mux.rs": source_file.read_text(encoding="utf-8"),
                "src/port_forward.rs": lifetime_file.read_text(encoding="utf-8"),
            })
            performance.apply_port_forward_mux(changes)
            performance.apply_port_forward_lifetime(changes)
            mux_out = changes["src/port_forward_mux.rs"]
            self.assertIn("if kcp.is_some() {\n                        send_kcp_close_reason(&mut stream).await;\n                    }", mux_out)
            self.assertIn("set_muxed_with_guard<G: Send + 'static>(\n            &self,\n            mut stream: Stream,\n            kcp: Option<crate::kcp_stream::KcpStream>,", mux_out)

    def test_standalone_cli_patches_usage_fingerprint_and_cli_tests(self):
        root = Path(__file__).resolve().parents[1]
        cli_mod = (root / "customizations/standalone_cli.py").read_text(encoding="utf-8")
        input_rs = (root / "client/terminal_exec/input.rs").read_text(encoding="utf-8")
        cli_rs = (root / "client/terminal_exec/cli.rs").read_text(encoding="utf-8")
        tunnel_rs = (root / "client/terminal_exec/tunnel.rs").read_text(encoding="utf-8")
        self.assertIn("embedded SSH over TCP, then GeodomDesk terminal", cli_mod)
        self.assertIn('"--ssh-host-fingerprint"', cli_mod)
        self.assertIn('"--ssh-host requires an explicit --ssh-user and --shell"', cli_mod)
        self.assertIn('"--ssh-known-hosts",\n', input_rs)
        self.assertIn('(vec!["exec","--id","1","--transport","ssh","--shell","powershell"],"SSH transport requires Bash"),', cli_rs)
        self.assertGreaterEqual(tunnel_rs.count('#[cfg(feature = "embedded-ssh")]'), 4)
