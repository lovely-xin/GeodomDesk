//! OpenSSH supplies authentication, host-key checking and encryption inside a managed TCP relay.
use super::{cli, stage, tunnel, MAX_OUTPUT};
use base::message_proto::{ExecShell, TerminalExecRequest, TerminalExecResult};
use hbb_common::{
    anyhow::{anyhow, Context},
    bail, base64, tokio, ResultType,
};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    process::Stdio,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout},
};

#[derive(Clone)]
pub(super) struct Options {
    pub username: Option<String>,
    pub port: u16,
    pub identity: Option<PathBuf>,
    pub known_hosts: PathBuf,
    pub strict_host_key: bool,
    pub password: Option<String>,
    pub askpass: PathBuf,
}

pub(super) struct Session {
    _child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    errors: Arc<Mutex<Vec<u8>>>,
    stderr_task: tokio::task::JoinHandle<()>,
    pub tunnel: tunnel::Managed,
}
impl Drop for Session {
    fn drop(&mut self) {
        self.stderr_task.abort();
    }
}

impl Session {
    fn diagnostic(&self) -> String {
        String::from_utf8_lossy(&self.errors.lock().unwrap_or_else(|poison| poison.into_inner()))
            .trim()
            .to_owned()
    }
    async fn line(&mut self) -> ResultType<String> {
        let mut bytes = Vec::new();
        let count = (&mut self.stdout)
            .take(8193)
            .read_until(b'\n', &mut bytes)
            .await?;
        if count == 0 {
            let _ = tokio::time::timeout(Duration::from_millis(250), &mut self.stderr_task).await;
            bail!("SSH channel closed: {}", self.diagnostic());
        }
        if count > 8192 {
            bail!("SSH worker protocol line exceeded 8192 bytes");
        }
        Ok(String::from_utf8(bytes)?
            .trim_end_matches(['\r', '\n'])
            .to_owned())
    }
    pub(super) async fn idle(&mut self) -> ResultType<()> {
        let bytes = self.stdout.fill_buf().await?;
        if bytes.is_empty() {
            bail!("SSH channel disconnected: {}", self.diagnostic());
        }
        bail!("Unexpected data from idle SSH worker")
    }
    pub(super) async fn execute(
        &mut self,
        request: TerminalExecRequest,
    ) -> ResultType<TerminalExecResult> {
        if request.shell.enum_value() != Ok(ExecShell::BASH) {
            return Err(stage::wrap(anyhow!("An SSH Bash session accepts only shell bash; use a separate terminal session for PowerShell"),"request","not_started"));
        }
        if self.tunnel.is_closed() {
            return Err(stage::wrap(
                anyhow!("CLI SSH tunnel was closed; reconnect or create a fresh connection"),
                "prepare",
                "not_started",
            ));
        }
        let timeout = request.timeout_seconds;
        tokio::time::timeout(
            Duration::from_secs(u64::from(timeout) + 10),
            self.exchange(request),
        )
        .await
        .context("Timed out waiting for SSH execution result")
        .and_then(|v| v)
        .map_err(|error| stage::wrap(error, "execution", "unknown"))
    }
    async fn exchange(&mut self, request: TerminalExecRequest) -> ResultType<TerminalExecResult> {
        let marker = uuid::Uuid::new_v4().simple().to_string();
        let script = request
            .script
            .strip_prefix(b"\xef\xbb\xbf")
            .unwrap_or(&request.script);
        let digest = format!("{:x}", Sha256::digest(script));
        let header = format!(
            "RUN {marker} {} {digest}\n{}\n",
            request.timeout_seconds,
            base64::encode(script)
        );
        self.stdin.write_all(header.as_bytes()).await?;
        self.stdin.flush().await?;
        let mut result = TerminalExecResult::new();
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let mut started = false;
        loop {
            let line = self.line().await?;
            let record = line
                .strip_prefix(&format!("{marker}:"))
                .context("SSH command response identity mismatch")?;
            if record == "START" && !started {
                started = true;
                continue;
            }
            if !started {
                bail!("SSH output received before execution acknowledgement");
            }
            if let Some(data) = record.strip_prefix("O:") {
                let bytes = base64::decode(data)?;
                if stdout.len() + bytes.len() > MAX_OUTPUT {
                    bail!("SSH stdout exceeded {MAX_OUTPUT} bytes");
                }
                stdout.extend_from_slice(&bytes);
            } else if let Some(data) = record.strip_prefix("E:") {
                let bytes = base64::decode(data)?;
                if stderr.len() + bytes.len() > MAX_OUTPUT {
                    bail!("SSH stderr exceeded {MAX_OUTPUT} bytes");
                }
                stderr.extend_from_slice(&bytes);
            } else if let Some(end) = record.strip_prefix("END:") {
                let (status, error) = end.split_once(':').context("Invalid SSH exit record")?;
                result.exit_code = status.parse()?;
                result.error = match error {
                    "0" => String::new(),
                    "1" => format!("SSH command output exceeded {MAX_OUTPUT} bytes"),
                    "2" => "SSH command ended abnormally (timeout or signal); check remote state before retrying".into(),
                    _ => bail!("Unknown SSH worker error"),
                };
                result.stdout = stdout.into();
                result.stderr = stderr.into();
                return Ok(result);
            } else {
                bail!("Unknown SSH worker record");
            }
        }
    }
}

fn ssh_program() -> ResultType<PathBuf> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStringExt;
        use windows::Win32::System::SystemInformation::GetSystemDirectoryW;
        let mut buffer = [0u16; 512];
        let len = unsafe { GetSystemDirectoryW(Some(&mut buffer)) } as usize;
        if len == 0 || len >= buffer.len() {
            bail!("Cannot locate Windows system directory");
        }
        let path =
            PathBuf::from(std::ffi::OsString::from_wide(&buffer[..len])).join(r"OpenSSH\ssh.exe");
        if !path.is_file() {
            bail!("Windows OpenSSH Client is unavailable; enable it or use --transport terminal");
        }
        return Ok(path);
    }
    #[cfg(not(windows))]
    Ok(PathBuf::from("ssh"))
}

pub(super) async fn open(
    options: &cli::ConnectOptions,
    ssh: &Options,
) -> ResultType<cli::Connection> {
    let started = Instant::now();
    let program = ssh_program()?;
    let identity = ssh.identity.as_ref().map(std::path::absolute).transpose()?;
    let known_hosts_path = std::path::absolute(&ssh.known_hosts)?;
    let tunnel =
        tunnel::Managed::start(options, 0, "127.0.0.1".into(), ssh.port, "Bash over SSH").await?;
    if tunnel.peer.platform != "Linux" {
        bail!("SSH Bash transport requires a Linux target");
    }
    let username = ssh
        .username
        .as_deref()
        .unwrap_or(&tunnel.peer.username)
        .trim();
    if username.is_empty() || username.chars().any(char::is_control) {
        bail!("Set --ssh-user to the target Linux login name");
    }
    if let Some(identity) = &identity {
        if !std::fs::metadata(identity)?.is_file() {
            bail!("SSH identity must be a regular file");
        }
    }
    if let Some(parent) = known_hosts_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let known_hosts = known_hosts_path
        .to_str()
        .context("SSH host-key path must be Unicode")?;
    if known_hosts.chars().any(char::is_control) {
        bail!("Invalid SSH host-key path");
    }
    let known_hosts = if cfg!(windows) {
        known_hosts.replace('\\', "/")
    } else {
        known_hosts.to_owned()
    };
    let known_hosts = known_hosts.replace('%', "%%").replace('"', "\\\"");
    let marker = uuid::Uuid::new_v4().simple().to_string();
    let worker = include_str!("ssh_worker.sh").replace("__SESSION__", &marker);
    let remote = format!(
        "env -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS bash --noprofile --norc -c \"$(printf %s {} | base64 -d)\"",
        base64::encode(worker)
    );
    let mut command = tokio::process::Command::new(program);
    command.args([
        "-T",
        "-F",
        if cfg!(windows) { "NUL" } else { "/dev/null" },
        "-l",
        username,
        "-p",
        &tunnel.port().to_string(),
    ]);
    for option in [
        "ServerAliveInterval=5".to_owned(),
        "ServerAliveCountMax=2".into(),
        "NumberOfPasswordPrompts=1".into(),
        "ClearAllForwardings=yes".into(),
        "ForwardAgent=no".into(),
        "EscapeChar=none".into(),
        "ConnectionAttempts=1".into(),
        format!("ConnectTimeout={}", options.timeout),
        "LogLevel=ERROR".into(),
        format!(
            "HostKeyAlias=geodomdesk-{:x}-ssh-{}",
            Sha256::digest(options.id.as_bytes()),
            ssh.port
        ),
        format!("UserKnownHostsFile=\"{known_hosts}\""),
        format!(
            "StrictHostKeyChecking={}",
            if ssh.strict_host_key {
                "yes"
            } else {
                "accept-new"
            }
        ),
        format!(
            "BatchMode={}",
            if ssh.password.is_some() { "no" } else { "yes" }
        ),
    ] {
        command.arg("-o").arg(option);
    }
    if let Some(identity) = &identity {
        command
            .arg("-i")
            .arg(identity)
            .args(["-o", "IdentitiesOnly=yes"]);
    }
    if let Some(password) = &ssh.password {
        command.args([
            "-o",
            if ssh.identity.is_some() {
                "PreferredAuthentications=publickey,password,keyboard-interactive"
            } else {
                "PreferredAuthentications=password,keyboard-interactive"
            },
        ]);
        if !ssh.askpass.is_file() {
            bail!("The matching geodomdesk-cli askpass helper is missing");
        }
        if password.contains(['\r', '\n']) {
            bail!("SSH password must not contain line breaks");
        }
        command
            .env("SSH_ASKPASS", &ssh.askpass)
            .env("SSH_ASKPASS_REQUIRE", "force")
            .env("DISPLAY", "geodomdesk:0")
            .env("GEODOMDESK_CLI_SSH_ASKPASS", "1")
            .env("GEODOMDESK_SSH_PASSWORD", password);
        #[cfg(windows)]
        {
            // Win32 OpenSSH's narrow environment lookup can corrupt a Unicode absolute path.
            // Resolve our fixed ASCII helper name from its Unicode-safe process working directory.
            command
                .current_dir(
                    ssh.askpass
                        .parent()
                        .context("Missing SSH helper directory")?,
                )
                .env(
                    "SSH_ASKPASS",
                    ssh.askpass.file_name().context("Missing SSH helper name")?,
                );
        }
    } else {
        command
            .env_remove("SSH_ASKPASS")
            .env_remove("SSH_ASKPASS_REQUIRE")
            .env_remove("GEODOMDESK_CLI_SSH_ASKPASS");
    }
    for name in [
        "GEODOMDESK_ACCOUNT_PASSWORD",
        "GEODOMDESK_DEVICE_PASSWORD",
        "GEODOMDESK_ADMIN_PASSWORD",
        "GEODOMDESK_DEVICE_CODE",
    ] {
        command.env_remove(name);
    }
    #[cfg(windows)]
    {
        command.creation_flags(0x08000000);
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        let parent = std::process::id();
        unsafe {
            command.as_std_mut().pre_exec(move || {
                if hbb_common::libc::prctl(
                    hbb_common::libc::PR_SET_PDEATHSIG,
                    hbb_common::libc::SIGKILL,
                ) < 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                if hbb_common::libc::getppid() as u32 != parent {
                    return Err(std::io::Error::from_raw_os_error(hbb_common::libc::EINTR));
                }
                Ok(())
            });
        }
    }
    let mut child = command
        .arg("127.0.0.1")
        .arg(remote)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context(
            "Cannot start OpenSSH; install the system OpenSSH client or use --transport terminal",
        )?;
    let stdin = child.stdin.take().context("Missing SSH input")?;
    let stdout = BufReader::new(child.stdout.take().context("Missing SSH output")?);
    let mut stderr = child.stderr.take().context("Missing SSH error output")?;
    let errors = Arc::new(Mutex::new(Vec::new()));
    let captured = errors.clone();
    let stderr_task = tokio::spawn(async move {
        let mut bytes = [0u8; 1024];
        loop {
            match stderr.read(&mut bytes).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let mut out = captured.lock().unwrap_or_else(|poison| poison.into_inner());
                    let remaining = 8192usize.saturating_sub(out.len());
                    out.extend_from_slice(&bytes[..n.min(remaining)]);
                }
            }
        }
    });
    let mut session = Session {
        _child: child,
        stdin,
        stdout,
        errors,
        stderr_task,
        tunnel,
    };
    let ready = session.line().await?;
    if ready != format!("{marker}:READY") {
        bail!("SSH worker did not become ready (requires Bash and coreutils)");
    }
    Ok(cli::Connection::from_ssh(session, started.elapsed()))
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    async fn worker() -> (Child, ChildStdin, BufReader<ChildStdout>) {
        let mut child = tokio::process::Command::new("bash")
            .args(["--noprofile", "--norc", "-c", include_str!("ssh_worker.sh")])
            .env_remove("BASH_ENV")
            .env("SHELLOPTS", "errexit:nounset:pipefail:posix")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let mut out = BufReader::new(child.stdout.take().unwrap());
        let mut ready = String::new();
        out.read_line(&mut ready).await.unwrap();
        assert_eq!(ready, "__SESSION__:READY\n");
        (child, input, out)
    }
    async fn invoke(
        input: &mut ChildStdin,
        out: &mut BufReader<ChildStdout>,
        script: &str,
        timeout: u32,
    ) -> (Vec<u8>, Vec<u8>, i32, u8) {
        let marker = uuid::Uuid::new_v4().simple().to_string();
        input
            .write_all(
                format!(
                    "RUN {marker} {timeout} {:x}\n{}\n",
                    Sha256::digest(script.as_bytes()),
                    base64::encode(script)
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        let read = async {
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            let mut started = false;
            loop {
                let mut line = String::new();
                assert!(out.read_line(&mut line).await.unwrap() > 0);
                let record = line.trim_end().strip_prefix(&format!("{marker}:")).unwrap();
                if record == "START" {
                    assert!(!started);
                    started = true;
                } else if let Some(data) = record.strip_prefix("O:") {
                    stdout.extend(base64::decode(data).unwrap());
                } else if let Some(data) = record.strip_prefix("E:") {
                    stderr.extend(base64::decode(data).unwrap());
                } else if let Some(end) = record.strip_prefix("END:") {
                    assert!(started);
                    let (code, error) = end.split_once(':').unwrap();
                    return (
                        stdout,
                        stderr,
                        code.parse().unwrap(),
                        error.parse().unwrap(),
                    );
                } else {
                    panic!("Unexpected worker record");
                }
            }
        };
        tokio::time::timeout(Duration::from_secs(u64::from(timeout) + 5), read)
            .await
            .unwrap()
    }
    #[tokio::test]
    async fn ssh_worker_reuses_transport_with_isolated_scripts_and_exact_exit_codes() {
        let (mut child, mut input, mut out) = worker().await;
        let value = invoke(
            &mut input,
            &mut out,
            "export GD_TEST_VALUE=leak; printf '中文😀'; printf '错误' >&2; exit 7",
            2,
        )
        .await;
        assert_eq!(
            value,
            (
                "中文😀".as_bytes().to_vec(),
                "错误".as_bytes().to_vec(),
                7,
                0
            )
        );
        let value = invoke(
            &mut input,
            &mut out,
            "printf '%s' \"${GD_TEST_VALUE-unset}\"; exit 124",
            2,
        )
        .await;
        assert_eq!(value, (b"unset".to_vec(), vec![], 124, 0));
        drop(input);
        assert!(child.wait().await.unwrap().success());
    }
    #[tokio::test]
    async fn ssh_worker_preserves_limits_and_recovers_without_truncation_success() {
        let (mut child, mut input, mut out) = worker().await;
        let script = format!(
            "#{}\nhead -c 262144 /dev/zero",
            "x".repeat(65536 - "#\nhead -c 262144 /dev/zero".len())
        );
        assert_eq!(script.len(), 65536);
        let value = invoke(&mut input, &mut out, &script, 3).await;
        assert_eq!(value.0.len(), 262144);
        assert_eq!((value.2, value.3), (0, 0));
        let value = invoke(&mut input, &mut out, "head -c 270000 /dev/zero", 3).await;
        assert_eq!(value.3, 1);
        let value = invoke(&mut input, &mut out, "printf AFTER_LIMIT", 2).await;
        assert_eq!(value, (b"AFTER_LIMIT".to_vec(), vec![], 0, 0));
        drop(input);
        assert!(child.wait().await.unwrap().success());
    }
    #[tokio::test]
    async fn ssh_worker_timeout_does_not_execute_the_command_again() {
        let (mut child, mut input, mut out) = worker().await;
        let start = Instant::now();
        let value = invoke(&mut input, &mut out, "sleep 4; printf LATE", 1).await;
        assert_eq!(value.3, 2);
        assert!(!String::from_utf8_lossy(&value.0).contains("LATE"));
        assert!(start.elapsed() < Duration::from_secs(4));
        let value = invoke(&mut input, &mut out, "printf AFTER_TIMEOUT", 2).await;
        assert_eq!(value, (b"AFTER_TIMEOUT".to_vec(), vec![], 0, 0));
        drop(input);
        assert!(child.wait().await.unwrap().success());
    }
}
