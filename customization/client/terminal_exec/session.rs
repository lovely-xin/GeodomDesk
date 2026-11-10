//! JSON Lines commands over a single authenticated terminal connection.
use super::{cli, input, legacy, output, validate, MAX_SCRIPT};
use base::message_proto::{message, ExecShell, PeerInfo, TerminalExecRequest};
use hbb_common::{anyhow::Context, bail, tokio, ResultType, Stream};
use serde::Deserialize;
use serde_json::Value;
use std::{
    io::{BufRead, Read},
    time::{Duration, Instant},
};

const MAX_LINE: usize = MAX_SCRIPT * 6 + 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    id: Option<Value>,
    shell: Option<String>,
    script: Option<String>,
    script_file: Option<String>,
    timeout: Option<u32>,
}

impl Command {
    fn request(
        self,
        admin: bool,
        timeout: u32,
        shell: Option<ExecShell>,
    ) -> ResultType<TerminalExecRequest> {
        let shell = match self.shell.as_deref() {
            Some("bash") => ExecShell::BASH,
            Some("powershell") => ExecShell::WINDOWS_POWERSHELL,
            Some("pwsh") => ExecShell::POWERSHELL_CORE,
            Some(shell) => bail!("Unknown shell: {shell}"),
            None => shell.context("Missing shell; specify it in the command or session --shell")?,
        };
        if admin && shell == ExecShell::BASH {
            bail!("--admin-user requires powershell or pwsh");
        }
        let script = match (self.script, self.script_file) {
            (Some(script), None) => script.into_bytes(),
            (None, Some(path)) => input::read_script(&path, false)?,
            _ => bail!("Specify exactly one of script or script_file"),
        };
        let request = TerminalExecRequest {
            shell: shell.into(),
            script: script.into(),
            timeout_seconds: self.timeout.unwrap_or(timeout),
            ..Default::default()
        };
        validate(&request)?;
        Ok(request)
    }
}

pub(super) async fn run(
    stream: &mut Stream,
    peer: &PeerInfo,
    admin: bool,
    timeout: u32,
    shell: Option<ExecShell>,
    info: Value,
) -> ResultType<i32> {
    if !peer.features.terminal_exec && !peer.features.terminal {
        bail!("Remote device does not support terminal commands");
    }
    let (tx, mut rx) = tokio::sync::mpsc::channel(1);
    // A blocking stdin task in the runtime cannot be cancelled when the peer disconnects.
    std::thread::Builder::new()
        .name("terminal-cli-input".into())
        .spawn(move || {
            let mut input = std::io::stdin().lock();
            loop {
                let mut line = Vec::new();
                let result = input
                    .by_ref()
                    .take(MAX_LINE as u64 + 1)
                    .read_until(b'\n', &mut line);
                if matches!(result, Ok(0)) {
                    break;
                }
                let result = result
                    .map_err(hbb_common::anyhow::Error::from)
                    .and_then(|_| {
                        if line.len() > MAX_LINE {
                            bail!("Session command exceeds {MAX_LINE} bytes");
                        }
                        Ok(line)
                    });
                let failed = result.is_err();
                if tx.blocking_send(result).is_err() || failed {
                    break;
                }
            }
        })?;
    output::write(info)?;
    let result: ResultType<i32> = async {
        let mut terminal_id = 0i32;
        loop {
            let line = tokio::select! {
                line = rx.recv() => match line {
                    Some(line) => line?,
                    None => return Ok(0),
                },
                message = cli::receive(stream) => {
                    if let Some(message::Union::TestDelay(delay)) = message?.union {
                        crate::client::handle_test_delay(delay, stream).await;
                    }
                    continue;
                }
            };
            if line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            let started = Instant::now();
            let mut command: Command =
                match serde_json::from_slice(&line).context("Invalid session command") {
                    Ok(command) => command,
                    Err(error) => {
                        output::write(output::failure(None, &error, false, started.elapsed()))?;
                        continue;
                    }
                };
            let id = command.id.take();
            let request = match command.request(admin, timeout, shell) {
                Ok(request) => request,
                Err(error) => {
                    output::write(output::failure(id, &error, false, started.elapsed()))?;
                    continue;
                }
            };
            let result = if peer.features.terminal_exec {
                tokio::time::timeout(
                    Duration::from_secs(u64::from(request.timeout_seconds) + 25),
                    cli::exchange(stream, request),
                )
                .await
                .context("Timed out waiting for remote execution result")
                .and_then(|result| result)
            } else {
                terminal_id = terminal_id
                    .checked_add(1)
                    .context("Session command limit reached")?;
                legacy::exchange(stream, &peer.platform, request, terminal_id).await
            };
            match result {
                Ok(result) => output::write(output::result(id, &result, started.elapsed()))?,
                Err(error) => {
                    output::write(output::failure(id, &error, true, started.elapsed()))?;
                    return Ok(1);
                }
            }
        }
    }
    .await;
    if let Err(error) = &result {
        output::write(output::failure(None, error, true, Duration::ZERO))?;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn session_commands_preserve_limits_and_admin_scope() {
        let parse = |line: &[u8], admin, timeout| -> ResultType<TerminalExecRequest> {
            serde_json::from_slice::<Command>(line)?.request(admin, timeout, None)
        };
        let line = br#"{"shell":"bash","script":"printf ok"}"#;
        assert_eq!(parse(line, false, 120).unwrap().timeout_seconds, 120);
        assert!(parse(line, true, 30).is_err());
        for line in [
            br#"{"shell":"bash","script":"exit","timeout":301}"#.as_slice(),
            br#"{"shell":"bash","script":"exit","timeout":0}"#,
            br#"{"shell":"bash","script":"exit","typo":1}"#,
            br#"{"shell":"bash","script":""}"#,
        ] {
            assert!(parse(line, false, 30).is_err());
        }
        let line =
            serde_json::to_vec(&json!({"shell": "pwsh", "script": "x".repeat(MAX_SCRIPT + 1)}))
                .unwrap();
        assert!(parse(&line, true, 30).is_err());
        assert!(parse(
            r#"{"shell":"powershell","script":"Write-Output '你好'","timeout":5}"#.as_bytes(),
            true,
            30
        )
        .is_ok());
    }

    #[test]
    fn local_script_and_default_shell_are_validated_without_consuming_stdin() {
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), "printf '你好'").unwrap();
        let command: Command =
            serde_json::from_value(json!({"id": "check", "script_file": file.path()})).unwrap();
        assert_eq!(command.id.as_ref().unwrap(), "check");
        let request = command.request(false, 15, Some(ExecShell::BASH)).unwrap();
        assert_eq!(request.script.as_ref(), "printf '你好'".as_bytes());
        for value in [
            json!({"script_file": "-"}),
            json!({"script": "exit", "script_file": file.path()}),
        ] {
            let command: Command = serde_json::from_value(value).unwrap();
            assert!(command.request(false, 15, Some(ExecShell::BASH)).is_err());
        }
        assert!(!input::wants_json(
            &["exec", "--command", "--json"].map(String::from)
        ));
        assert!(input::wants_json(
            &["exec", "--command", "pwd", "--json"].map(String::from)
        ));
        let mut command: Command =
            serde_json::from_value(json!({"id": 7, "script": "exit"})).unwrap();
        let id = command.id.take();
        let result = output::result(id, &Default::default(), Duration::from_millis(23));
        assert_eq!(result["id"], 7);
        assert_eq!(result["elapsed_ms"], 23);
    }
}
