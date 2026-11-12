//! Embedded SSH over an authenticated, process-owned GeodomDesk relay.
use super::{auth, cli, stage, tunnel, MAX_OUTPUT};
use base::message_proto::{ExecShell, TerminalExecRequest, TerminalExecResult};
use hbb_common::{
    anyhow::{anyhow, Context},
    bail,
    base64::{engine::general_purpose::STANDARD, Engine},
    tokio, ResultType,
};
use russh::{
    client,
    keys::{self, known_hosts, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
    ChannelMsg,
};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::ErrorKind,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    task::{Context as TaskContext, Poll},
    time::{Duration, Instant},
};
use tokio::io::{AsyncRead, AsyncWrite, DuplexStream, ReadBuf};

#[derive(Clone)]
pub(super) struct Options {
    pub username: Option<String>,
    pub host: Option<String>,
    pub port: u16,
    pub identity: Option<PathBuf>,
    pub known_hosts: PathBuf,
    pub strict_host_key: bool,
    pub fingerprint: Option<String>,
    pub ask_password: bool,
    pub non_interactive: bool,
    pub allow_fallback: bool,
    pub shell: ExecShell,
}

#[derive(Debug)]
pub(super) enum Unavailable {
    Service,
    Credentials,
}

impl std::fmt::Display for Unavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Service => "No SSH service responded through the authenticated TCP relay",
            Self::Credentials => {
                "No SSH credentials are available. Use --ssh-identity or --ask-ssh-password"
            }
        })
    }
}
impl std::error::Error for Unavailable {}

struct HostTrust {
    alias: String,
    file: PathBuf,
    strict: bool,
    fingerprint: Option<String>,
}

impl client::Handler for HostTrust {
    type Error = hbb_common::anyhow::Error;

    async fn check_server_key(&mut self, received: &PublicKeyOrCertificate) -> ResultType<bool> {
        let PublicKeyOrCertificate::PublicKey { key, .. } = received else {
            bail!("This SSH host requires certificate-authority configuration");
        };
        let file = self.file.clone();
        let alias = self.alias.clone();
        let received = key.clone();
        let strict = self.strict;
        let fingerprint = self.fingerprint.clone();
        tokio::task::spawn_blocking(move || {
            check_host_key(&file, &alias, &received, strict, fingerprint.as_deref())
        })
        .await?
    }
}

fn check_host_key(
    file: &Path,
    alias: &str,
    received: &keys::PublicKey,
    strict: bool,
    expected_fingerprint: Option<&str>,
) -> ResultType<bool> {
    let fingerprint = received.fingerprint(keys::HashAlg::Sha256).to_string();
    if expected_fingerprint.is_some_and(|expected| expected != fingerprint) {
        bail!("SSH host key fingerprint mismatch for {alias} ({fingerprint})");
    }
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent)?;
    }
    // Serialize trust-on-first-use across processes, including concurrent fresh sessions.
    let mut lock_path = file.as_os_str().to_os_string();
    lock_path.push(".lock");
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let lock = options.open(lock_path)?;
    lock.lock()?;
    let recorded = known_hosts::known_host_keys_path(alias, 22, file)?;
    if !recorded.is_empty() {
        if recorded.iter().any(|(_, key)| key == received) {
            return Ok(true);
        }
        bail!("SSH host key changed for {alias} ({fingerprint})");
    }
    if strict && expected_fingerprint.is_none() {
        bail!("SSH host key is not trusted for {alias} ({fingerprint})");
    }
    options.open(file)?;
    known_hosts::learn_known_hosts_path(alias, 22, received, file)?;
    Ok(true)
}

pub(super) struct Session {
    handle: client::Handle<HostTrust>,
    pub tunnel: tunnel::Managed,
    token: String,
    verified_at: Instant,
    shell: ExecShell,
    alias: String,
}

impl Session {
    pub(super) fn destination(&self) -> serde_json::Value {
        serde_json::json!({
            "host": self.tunnel.info["remote_host"],
            "port": self.tunnel.info["remote_port"],
            "via_device": self.tunnel.info["device_id"],
            "shell": match self.shell {
                ExecShell::BASH => "bash",
                ExecShell::WINDOWS_POWERSHELL => "powershell",
                ExecShell::POWERSHELL_CORE => "pwsh",
            },
            "host_key_alias": self.alias,
        })
    }

    pub(super) async fn idle(&mut self) -> ResultType<()> {
        (&mut self.handle).await?;
        bail!("SSH connection disconnected");
    }

    pub(super) async fn execute(
        &mut self,
        request: TerminalExecRequest,
    ) -> ResultType<TerminalExecResult> {
        if request.shell.enum_value() != Ok(self.shell) {
            return Err(stage::wrap(
                anyhow!("SSH session uses a fixed shell"),
                "request",
                "not_started",
            ));
        }
        if self.tunnel.is_closed() || self.handle.is_closed() {
            return Err(stage::wrap(
                anyhow!("SSH relay closed; create a fresh connection"),
                "prepare",
                "not_started",
            ));
        }
        if self.verified_at.elapsed() >= Duration::from_secs(60) {
            auth::verify_ssh_account(&self.token).await?;
            self.verified_at = Instant::now();
        }
        let duration = Duration::from_secs(request.timeout_seconds.into());
        let deadline = tokio::time::Instant::now() + duration;
        let channel = tokio::time::timeout_at(deadline, self.handle.channel_open_session())
            .await
            .context("SSH channel setup timed out")
            .and_then(|result| result.context("SSH channel setup failed"))
            .map_err(|error| stage::wrap(error, "prepare", "not_started"))?;
        let launcher = launcher(self.shell);
        let script = request
            .script
            .strip_prefix(b"\xef\xbb\xbf")
            .unwrap_or(&request.script);
        let exchange = async {
            channel.exec(true, launcher).await?;
            let mut result = TerminalExecResult::new();
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            let mut status = None;
            let (mut output, input) = channel.split();
            let send = async {
                input.data_bytes(script.to_vec()).await?;
                input.eof().await
            };
            let read = async {
                let mut accepted = false;
                while let Some(message) = output.wait().await {
                    match message {
                        ChannelMsg::Success => accepted = true,
                        ChannelMsg::Failure if !accepted => {
                            bail!("SSH server rejected the remote shell")
                        }
                        ChannelMsg::Data { data } => append(&mut stdout, &data)?,
                        ChannelMsg::ExtendedData { data, ext: 1 } => append(&mut stderr, &data)?,
                        ChannelMsg::ExitStatus { exit_status } => status = Some(exit_status as i32),
                        ChannelMsg::ExitSignal { signal_name, .. } => {
                            bail!("SSH command ended with signal {signal_name:?}")
                        }
                        ChannelMsg::Close => break,
                        _ => {}
                    }
                }
                Ok::<(), hbb_common::anyhow::Error>(())
            };
            tokio::try_join!(
                async { send.await.map_err(hbb_common::anyhow::Error::from) },
                read
            )?;
            result.exit_code = status.context("SSH channel closed without an exit status")?;
            result.stdout = stdout.into();
            result.stderr = stderr.into();
            Ok::<_, hbb_common::anyhow::Error>(result)
        };
        let result = tokio::time::timeout_at(deadline, exchange)
            .await
            .context("SSH execution timed out. Verify remote state before retrying")
            .and_then(|result| result);
        if result.is_err() {
            // Close the authenticated transport even if the server ignores SSH signals.
            let _ = tokio::time::timeout(
                Duration::from_millis(250),
                self.handle.disconnect(
                    russh::Disconnect::ByApplication,
                    "Execution interrupted",
                    "",
                ),
            )
            .await;
        }
        result.map_err(|error| stage::wrap(error, "execution", "unknown"))
    }
}

fn append(output: &mut Vec<u8>, data: &[u8]) -> ResultType<()> {
    if data.len() > MAX_OUTPUT.saturating_sub(output.len()) {
        bail!("SSH output exceeded {MAX_OUTPUT} bytes per stream");
    }
    output.extend_from_slice(data);
    Ok(())
}

fn launcher(shell: ExecShell) -> String {
    match shell {
        ExecShell::BASH => r#"env -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS bash --noprofile --norc -c 'script=$(cat) || exit 1; exec bash --noprofile --norc -c "$script" </dev/null'"#.into(),
        ExecShell::WINDOWS_POWERSHELL | ExecShell::POWERSHELL_CORE => {
            let program = if shell == ExecShell::WINDOWS_POWERSHELL { "powershell.exe" } else { "pwsh.exe" };
            let code: Vec<u8> = include_str!("exec_bootstrap.ps1")
                .encode_utf16().flat_map(u16::to_le_bytes).collect();
            format!("{program} -NoLogo -NoProfile -NonInteractive -EncodedCommand {}", STANDARD.encode(code))
        }
    }
}

fn host_alias(id: &str, host: Option<&str>, port: u16) -> String {
    let mut hash = Sha256::new();
    hash.update(id.as_bytes());
    if let Some(host) = host {
        hash.update([0]);
        hash.update(host.to_ascii_lowercase().as_bytes());
    }
    format!("geodomdesk-{:x}-ssh-{port}", hash.finalize())
}

struct ObservedStream {
    stream: DuplexStream,
    received: Arc<AtomicBool>,
}

impl AsyncRead for ObservedStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let before = buffer.filled().len();
        let result = Pin::new(&mut self.stream).poll_read(context, buffer);
        if buffer.filled().len() > before {
            self.received.store(true, Ordering::Relaxed);
        }
        result
    }
}

impl AsyncWrite for ObservedStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
        bytes: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write(context, bytes)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(context)
    }
}

pub(super) async fn open(
    options: &cli::ConnectOptions,
    ssh: &Options,
) -> ResultType<cli::Connection> {
    let started = Instant::now();
    auth::verify_ssh_account(&options.token).await?;
    let verified_at = Instant::now();
    let host = ssh.host.as_deref().unwrap_or("127.0.0.1");
    let (tunnel, stream) = tunnel::Managed::start_stream(options, host.into(), ssh.port)
        .await
        .map_err(|error| {
            if error.is::<cli::ForwardUnavailable>() {
                return error.context(Unavailable::Service);
            }
            error
        })?;
    if ssh.host.is_none() {
        let required = match ssh.shell {
            ExecShell::BASH => "Linux",
            _ => "Windows",
        };
        if !tunnel.peer.platform.eq_ignore_ascii_case(required) {
            return Err(stage::wrap(
                anyhow!("Selected shell requires a {required} target"),
                "target",
                "not_started",
            ));
        }
    }
    let username = ssh
        .username
        .as_deref()
        .unwrap_or(&tunnel.peer.username)
        .trim();
    if username.is_empty() || username.chars().any(char::is_control) {
        bail!("Set --ssh-user to the destination operating system account");
    }
    let alias = host_alias(&options.id, ssh.host.as_deref(), ssh.port);
    let trust = HostTrust {
        alias: alias.clone(),
        file: std::path::absolute(&ssh.known_hosts)?,
        strict: ssh.strict_host_key,
        fingerprint: ssh.fingerprint.clone(),
    };
    let mut config = client::Config {
        keepalive_interval: Some(Duration::from_secs(5)),
        keepalive_max: 2,
        ..Default::default()
    };
    let recorded = known_hosts::known_host_keys_path(&trust.alias, 22, &trust.file)?;
    if !recorded.is_empty() {
        let algorithms = config
            .preferred
            .key
            .iter()
            .filter(|candidate| {
                recorded.iter().any(|(_, key)| {
                    let known = key.algorithm();
                    &known == *candidate
                        || matches!(
                            (&known, candidate),
                            (keys::Algorithm::Rsa { .. }, keys::Algorithm::Rsa { .. })
                        )
                })
            })
            .cloned()
            .collect::<Vec<_>>();
        if algorithms.is_empty() {
            bail!("No supported SSH algorithm for the trusted host key ({alias})");
        }
        config.preferred.key = algorithms.into();
    }
    let received = Arc::new(AtomicBool::new(false));
    let stream = ObservedStream {
        stream,
        received: received.clone(),
    };
    let handshake = client::connect_stream(Arc::new(config), stream, trust);
    tokio::pin!(handshake);
    let result = tokio::select! {
        result = &mut handshake => result,
        _ = tokio::time::sleep(Duration::from_secs(3)) => {
            if !received.load(Ordering::Relaxed) {
                return Err(Unavailable::Service.into());
            }
            handshake.await
        }
    };
    let mut handle = match result {
        Err(error) if !received.load(Ordering::Relaxed) && is_early_disconnect(&error) => {
            return Err(Unavailable::Service.into());
        }
        result => result.context("SSH handshake failed")?,
    };
    authenticate(&mut handle, username, ssh).await?;
    Ok(cli::Connection::from_ssh(
        Session {
            handle,
            tunnel,
            token: options.token.clone(),
            verified_at,
            shell: ssh.shell,
            alias,
        },
        started.elapsed(),
    ))
}

fn is_early_disconnect(error: &hbb_common::anyhow::Error) -> bool {
    let closed = |error: &std::io::Error| {
        matches!(
            error.kind(),
            ErrorKind::UnexpectedEof | ErrorKind::BrokenPipe | ErrorKind::ConnectionReset
        )
    };
    match error.downcast_ref::<russh::Error>() {
        Some(russh::Error::Disconnect) => true,
        Some(russh::Error::IO(error)) => closed(error),
        _ => error.downcast_ref::<std::io::Error>().is_some_and(closed),
    }
}

async fn authenticate(
    handle: &mut client::Handle<HostTrust>,
    username: &str,
    ssh: &Options,
) -> ResultType<()> {
    let password = match std::env::var("GEODOMDESK_SSH_PASSWORD") {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(std::env::VarError::NotUnicode(_)) => {
            bail!("GEODOMDESK_SSH_PASSWORD must be valid Unicode")
        }
    };
    let identity = ssh.identity.clone().or_else(|| {
        let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })?;
        ["id_ed25519", "id_ecdsa", "id_rsa"]
            .into_iter()
            .map(|file| PathBuf::from(&home).join(".ssh").join(file))
            .find(|file| file.is_file())
            .filter(|_| password.is_none() && !ssh.ask_password)
    });
    let used_identity = identity.is_some();
    if let Some(identity) = identity {
        if !fs::metadata(&identity)?.is_file() {
            bail!("SSH identity must be a regular file");
        }
        let mut key = keys::load_secret_key(&identity, None);
        if matches!(key, Err(keys::Error::KeyIsEncrypted)) {
            let passphrase = std::env::var("GEODOMDESK_SSH_KEY_PASSPHRASE")
                .ok()
                .map(Ok)
                .unwrap_or_else(|| {
                    if ssh.non_interactive {
                        return Err(std::io::Error::other("Set GEODOMDESK_SSH_KEY_PASSPHRASE"));
                    }
                    rpassword::prompt_password("SSH private-key passphrase: ")
                })?;
            key = keys::load_secret_key(&identity, Some(&passphrase));
        }
        let key = PrivateKeyWithHashAlg::new(
            Arc::new(key?),
            handle.best_supported_rsa_hash().await?.flatten(),
        );
        if handle
            .authenticate_publickey(username, key)
            .await?
            .success()
        {
            return Ok(());
        }
    }
    let password = match password {
        Some(password) => Some(password),
        None if ssh.ask_password && !ssh.non_interactive => {
            Some(rpassword::prompt_password("Target SSH password: ")?)
        }
        None if ssh.ask_password => bail!("Set GEODOMDESK_SSH_PASSWORD for non-interactive use"),
        None => None,
    };
    if let Some(password) = password {
        if handle
            .authenticate_password(username, password)
            .await?
            .success()
        {
            return Ok(());
        }
        bail!("SSH destination rejected the account credentials");
    }
    if used_identity {
        bail!("SSH destination rejected the private key");
    }
    Err(Unavailable::Credentials.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_key(byte: u8) -> keys::PublicKey {
        let mut wire = b"\0\0\0\x0bssh-ed25519\0\0\0\x20".to_vec();
        wire.extend([byte; 32]);
        keys::parse_public_key_base64(&STANDARD.encode(wire)).unwrap()
    }

    #[test]
    fn first_trust_requires_a_pin_in_strict_mode_and_keeps_the_recorded_key() {
        let root = std::env::temp_dir().join(format!("geodomdesk-cli-{}", uuid::Uuid::new_v4()));
        let file = root.join("known_hosts");
        let key = test_key(1);
        let changed = test_key(2);
        let fingerprint = key.fingerprint(keys::HashAlg::Sha256).to_string();
        let alias = host_alias("123", Some("host"), 22);
        assert!(check_host_key(&file, &alias, &key, true, None).is_err());
        assert!(!file.exists());
        assert!(check_host_key(&file, &alias, &key, true, Some(&fingerprint)).unwrap());
        assert!(check_host_key(&file, &alias, &key, true, None).unwrap());
        assert!(check_host_key(&file, &alias, &changed, false, None).is_err());
        assert!(check_host_key(&file, &alias, &changed, true, Some(&fingerprint)).is_err());
        assert!(check_host_key(&file, &alias, &key, true, None).unwrap());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn relay_identity_and_destination_scope_the_host_key() {
        assert_ne!(
            host_alias("123", None, 22),
            host_alias("123", Some("127.0.0.1"), 22)
        );
        assert_ne!(
            host_alias("123", Some("host"), 22),
            host_alias("456", Some("host"), 22)
        );
        assert_ne!(
            host_alias("123", Some("host"), 22),
            host_alias("123", Some("host"), 23)
        );
        assert_eq!(
            host_alias("123", Some("HOST"), 22),
            host_alias("123", Some("host"), 22)
        );
    }
}
