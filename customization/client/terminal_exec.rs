//! One-shot commands on an authenticated terminal connection.
mod auth;
mod cli;
mod input;
mod legacy;
mod output;
mod session;
mod stage;
mod tunnel;
mod ssh;
pub(crate) use tunnel::{list as list_tunnels, close as close_tunnel};
#[cfg(windows)]
mod windows;

pub use cli::run_cli;
#[cfg(windows)]
pub(crate) use windows::run_helper;

use base::message_proto::{
    ExecShell, Message, TerminalExecRequest, TerminalExecResult, TerminalResponse,
};
use hbb_common::{anyhow::Context, bail, tokio, ResultType, Stream};

const MAX_SCRIPT: usize = 64 * 1024;
const MAX_OUTPUT: usize = 256 * 1024;
const MAX_TIMEOUT: u32 = 300;

pub(crate) fn validate(request: &TerminalExecRequest) -> ResultType<()> {
    if request.script.is_empty() || request.script.len() > MAX_SCRIPT {
        bail!("Script must contain 1 to {MAX_SCRIPT} bytes");
    }
    std::str::from_utf8(&request.script).context("Script must be UTF-8")?;
    if !(1..=MAX_TIMEOUT).contains(&request.timeout_seconds) {
        bail!("Timeout must be between 1 and {MAX_TIMEOUT} seconds");
    }
    if let Err(shell) = request.shell.enum_value() {
        bail!("Unknown shell ({shell})");
    }
    Ok(())
}

pub(crate) async fn execute(
    request: TerminalExecRequest,
    #[cfg(windows)] token: Option<crate::server::terminal_helper::UserToken>,
) -> TerminalExecResult {
    let result = async {
        validate(&request)?;
        static SLOTS: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(8);
        let slot = SLOTS
            .try_acquire()
            .context("Too many running terminal commands")?;
        #[cfg(windows)]
        {
            use std::sync::{
                atomic::{AtomicBool, Ordering},
                Arc,
            };
            struct Cancel(Arc<AtomicBool>);
            impl Drop for Cancel {
                fn drop(&mut self) {
                    self.0.store(true, Ordering::Relaxed);
                }
            }
            let cancel = Cancel(Arc::new(AtomicBool::new(false)));
            let cancelled = cancel.0.clone();
            return tokio::task::spawn_blocking(move || {
                let _slot = slot;
                windows::execute(request, token, &cancelled)
            })
            .await?;
        }
        #[cfg(target_os = "linux")]
        {
            let _slot = slot;
            return bash(request).await;
        }
    }
    .await;
    result.unwrap_or_else(|error| TerminalExecResult {
        error: error.to_string(),
        ..Default::default()
    })
}

pub(crate) async fn serve(
    stream: &mut Stream,
    request: TerminalExecRequest,
    #[cfg(windows)] token: Option<crate::server::terminal_helper::UserToken>,
) -> ResultType<()> {
    let execute = execute(
        request,
        #[cfg(windows)]
        token,
    );
    tokio::pin!(execute);
    let interval = std::time::Duration::from_secs(5);
    let mut keepalive = tokio::time::interval_at(tokio::time::Instant::now() + interval, interval);
    let result = loop {
        tokio::select! {
            result = &mut execute => break result,
            incoming = stream.next() => {
                incoming.context("Command client disconnected")??;
            }
            _ = keepalive.tick() => {
                stream.send(&Message::new()).await?;
            }
        }
    };
    let mut response = TerminalResponse::new();
    response.set_exec_result(result);
    let mut msg = Message::new();
    msg.set_terminal_response(response);
    stream.send(&msg).await
}

#[cfg(target_os = "linux")]
async fn bash(request: TerminalExecRequest) -> ResultType<TerminalExecResult> {
    use hbb_common::tokio::{
        self,
        io::{AsyncRead, AsyncReadExt},
        process::Command,
    };
    use std::{
        io::Write,
        os::unix::process::{CommandExt, ExitStatusExt},
        process::Stdio,
        time::Duration,
    };

    async fn read_output(stream: impl AsyncRead + Unpin) -> ResultType<Vec<u8>> {
        let mut output = Vec::new();
        stream
            .take(MAX_OUTPUT as u64 + 1)
            .read_to_end(&mut output)
            .await?;
        if output.len() > MAX_OUTPUT {
            bail!("Output exceeded {MAX_OUTPUT} bytes");
        }
        Ok(output)
    }

    struct ProcessGroup(i32);
    impl Drop for ProcessGroup {
        fn drop(&mut self) {
            unsafe { nix::libc::kill(-self.0, nix::libc::SIGKILL) };
        }
    }

    if request.shell.enum_value() != Ok(ExecShell::BASH) {
        bail!("This Linux device supports Bash execution");
    }
    let mut script = tempfile::NamedTempFile::new().context("Cannot create private Bash script")?;
    script.write_all(
        request
            .script
            .strip_prefix(b"\xef\xbb\xbf")
            .unwrap_or(&request.script),
    )?;
    let mut command = Command::new("/bin/bash");
    command.as_std_mut().process_group(0);
    let mut child = command
        .arg(script.path())
        .env_remove("BASH_ENV")
        .env_remove("ENV")
        .env_remove("SHELLOPTS")
        .env_remove("BASHOPTS")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context("Cannot start Bash")?;
    let _group = ProcessGroup(child.id().context("Missing Bash process ID")? as i32);
    let stdout = child.stdout.take().context("Missing Bash output")?;
    let stderr = child.stderr.take().context("Missing Bash error output")?;
    let wait = async { Ok::<_, hbb_common::anyhow::Error>(child.wait().await?) };
    let (stdout, stderr, status) =
        tokio::time::timeout(Duration::from_secs(request.timeout_seconds.into()), async {
            tokio::try_join!(read_output(stdout), read_output(stderr), wait)
        })
        .await
        .context("Remote command timed out")??;
    Ok(TerminalExecResult {
        stdout: stdout.into(),
        stderr: stderr.into(),
        exit_code: status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or_default()),
        ..Default::default()
    })
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use hbb_common::tokio;

    fn request(script: &str, seconds: u32) -> TerminalExecRequest {
        TerminalExecRequest {
            shell: ExecShell::BASH.into(),
            script: script.as_bytes().to_vec().into(),
            timeout_seconds: seconds,
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn bash_keeps_output_channels_and_exit_code() {
        let result = execute(request("false; printf '你好'; printf '错误' >&2; exit 7", 3)).await;
        assert!(result.error.is_empty(), "{}", result.error);
        assert_eq!(result.stdout.as_ref(), "你好".as_bytes());
        assert_eq!(result.stderr.as_ref(), "错误".as_bytes());
        assert_eq!(result.exit_code, 7);
    }

    #[tokio::test]
    async fn encrypted_terminal_round_trip_survives_keepalive() {
        use base::message_proto::message;
        use hbb_common::{sodiumoxide::crypto::secretbox, tcp::FramedStream};
        use std::time::Duration;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (server, address) = accepted.unwrap();
        let mut server = Stream::Tcp(FramedStream::from(server, address));
        let mut client = Stream::Tcp(FramedStream::from(client.unwrap(), endpoint));
        let key = secretbox::gen_key();
        server.set_key(key.clone());
        client.set_key(key);
        let peer = async {
            for _ in 0..2 {
                let Some(message::Union::TerminalAction(mut action)) =
                    cli::receive(&mut server).await.unwrap().union
                else {
                    panic!("Expected terminal action");
                };
                assert!(action.has_exec());
                serve(&mut server, action.take_exec()).await.unwrap();
            }
        };
        let command = async {
            let first = cli::exchange(&mut client, request("sleep 5.2; printf 'first'", 8))
                .await
                .unwrap();
            assert_eq!(first.stdout.as_ref(), b"first");
            cli::exchange(
                &mut client,
                request("printf '你好'; printf '错误' >&2; exit 7", 2),
            )
            .await
        };
        let (result, ()) = tokio::time::timeout(Duration::from_secs(10), async {
            tokio::join!(command, peer)
        })
        .await
        .unwrap();
        let result = result.unwrap();
        assert!(result.error.is_empty(), "{}", result.error);
        assert_eq!(result.stdout.as_ref(), "你好".as_bytes());
        assert_eq!(result.stderr.as_ref(), "错误".as_bytes());
        assert_eq!(result.exit_code, 7);
    }

    #[tokio::test]
    async fn bash_timeout_cleans_up_descendants() {
        let pidfile = tempfile::NamedTempFile::new().unwrap();
        let script = format!(
            "sleep 30 & printf '%s' $! > '{}'; wait",
            pidfile.path().display()
        );
        let result = execute(request(&script, 1)).await;
        assert!(result.error.contains("timed out"), "{}", result.error);
        let pid: i32 = std::fs::read_to_string(pidfile.path())
            .unwrap()
            .parse()
            .unwrap();
        let proc = format!("/proc/{pid}/stat");
        let stopped = tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                match std::fs::read_to_string(&proc) {
                    Ok(status)
                        if !status
                            .rsplit_once(") ")
                            .is_some_and(|(_, status)| status.starts_with('Z')) =>
                    {
                        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                    }
                    _ => break,
                }
            }
        })
        .await;
        if stopped.is_err() {
            unsafe { nix::libc::kill(pid, nix::libc::SIGKILL) };
        }
        assert!(stopped.is_ok(), "timed-out child is still running");
    }

    #[tokio::test]
    async fn bash_rejects_output_over_the_limit() {
        let result = execute(request("head -c 270000 /dev/zero", 3)).await;
        assert!(result.error.contains("Output exceeded"), "{}", result.error);
    }
}
