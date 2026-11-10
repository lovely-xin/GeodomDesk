//! JSON Lines commands over a single authenticated terminal connection.
use super::{cli, legacy, validate, MAX_SCRIPT};
use base::message_proto::{message, ExecShell, PeerInfo, TerminalExecRequest, TerminalExecResult};
use hbb_common::{anyhow::Context, bail, tokio, ResultType, Stream};
use serde::Deserialize;
use serde_json::json;
use std::{
    io::{BufRead, Read, Write},
    time::Duration,
};

const MAX_LINE: usize = MAX_SCRIPT * 6 + 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    shell: String,
    script: String,
    timeout: Option<u32>,
}

fn parse(line: &[u8], admin: bool, timeout: u32) -> ResultType<TerminalExecRequest> {
    let command: Command = serde_json::from_slice(line).context("Invalid session command")?;
    let shell = match command.shell.as_str() {
        "bash" if !admin => ExecShell::BASH,
        "powershell" => ExecShell::WINDOWS_POWERSHELL,
        "pwsh" => ExecShell::POWERSHELL_CORE,
        _ => bail!("Unavailable shell: {}", command.shell),
    };
    let request = TerminalExecRequest {
        shell: shell.into(),
        script: command.script.into_bytes().into(),
        timeout_seconds: command.timeout.unwrap_or(timeout),
        ..Default::default()
    };
    validate(&request)?;
    Ok(request)
}

fn write_record(value: serde_json::Value) -> ResultType<()> {
    let mut data = serde_json::to_vec(&value)?;
    data.push(b'\n');
    let mut output = std::io::stdout().lock();
    output.write_all(&data)?;
    output.flush()?;
    Ok(())
}

pub(super) async fn run(
    stream: &mut Stream,
    peer: &PeerInfo,
    admin: bool,
    timeout: u32,
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
    write_record(json!({"event": "ready", "platform": peer.platform,
        "protocol": if peer.features.terminal_exec { "exec" } else { "legacy" }}))?;
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
            let request = match parse(&line, admin, timeout) {
                Ok(request) => request,
                Err(error) => {
                    write_record(
                        json!({"event": "result", "error": format!("{error:#}"), "fatal": false}),
                    )?;
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
                Ok(TerminalExecResult {
                    stdout,
                    stderr,
                    exit_code,
                    error,
                    ..
                }) => {
                    write_record(json!({"event": "result", "encoding": "base64",
                    "stdout": crate::encode64(stdout), "stderr": crate::encode64(stderr),
                    "exit_code": exit_code, "error": error, "fatal": false}))?;
                }
                Err(error) => return Err(error),
            }
        }
    }
    .await;
    if let Err(error) = &result {
        write_record(json!({"event": "result", "error": format!("{error:#}"), "fatal": true}))?;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_commands_preserve_limits_and_admin_scope() {
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
}
