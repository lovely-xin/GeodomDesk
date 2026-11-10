//! One-shot commands for peers that expose only an interactive terminal.
use super::{cli, MAX_OUTPUT};
use base::message_proto::{
    terminal_response::Union, CloseTerminal, ExecShell, OpenTerminal, TerminalAction, TerminalData,
    TerminalExecRequest, TerminalExecResult,
};
use hbb_common::{
    anyhow::Context, bail, sodiumoxide::crypto::hash::sha256, tokio, ResultType, Stream,
};
use std::{time::Duration, vec};

const LINE_BYTES: usize = 512;
const BATCH_LINES: usize = 16;
const PREPARE_TIMEOUT: Duration = Duration::from_secs(15);

fn encode_powershell(script: &str) -> String {
    crate::encode64(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    )
}

fn command(
    platform: &str,
    request: &TerminalExecRequest,
    marker: &str,
) -> ResultType<(String, String)> {
    let shell = request.shell.enum_value().ok().context("Unknown shell")?;
    let script = request
        .script
        .strip_prefix(b"\xef\xbb\xbf")
        .unwrap_or(&request.script);
    if script.contains(&0) {
        bail!("Legacy terminal scripts cannot contain NUL bytes");
    }
    let encoded = crate::encode64(script);
    let (template, payload) = match (platform, shell) {
        ("Linux", ExecShell::BASH) => (include_str!("legacy_bootstrap.sh"), encoded),
        ("Windows", ExecShell::WINDOWS_POWERSHELL | ExecShell::POWERSHELL_CORE) => (
            include_str!("legacy_bootstrap.ps1"),
            format!(
                "{}:{encoded}",
                encode_powershell(include_str!("exec_bootstrap.ps1"))
            ),
        ),
        _ => bail!("Requested shell is unavailable on this legacy {platform} device"),
    };
    let bootstrap = template
        .replace("__MARKER__", marker)
        .replace(
            "__DIGEST__",
            &hex::encode(sha256::hash(payload.as_bytes()).0),
        )
        .replace("__LINES__", &payload.len().div_ceil(LINE_BYTES).to_string())
        .replace("__BATCH__", &BATCH_LINES.to_string())
        .replace(
            "__CORE__",
            if shell == ExecShell::POWERSHELL_CORE {
                "$true"
            } else {
                "$false"
            },
        );
    let launcher = if platform == "Windows" {
        // The old Windows peer uses this system path even when PowerShell is absent from PATH.
        format!(
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {}",
            encode_powershell(&bootstrap)
        )
    } else {
        format!(
            "bash --noprofile --norc -c 'eval \"$(printf %s {} | base64 -d)\"'",
            crate::encode64(bootstrap)
        )
    };
    Ok((launcher, payload))
}

async fn send_input(stream: &mut Stream, terminal_id: i32, data: Vec<u8>) -> ResultType<()> {
    let mut action = TerminalAction::new();
    action.set_data(TerminalData {
        terminal_id,
        data: data.into(),
        ..Default::default()
    });
    cli::send_terminal_action(stream, action).await
}

pub(super) async fn exchange(
    stream: &mut Stream,
    platform: &str,
    request: TerminalExecRequest,
    terminal_id: i32,
) -> ResultType<TerminalExecResult> {
    let marker = format!("GD{}", uuid::Uuid::new_v4().simple());
    let (launcher, payload) = command(platform, &request, &marker)?;
    let mut terminal = Transcript {
        terminal_id,
        ..Default::default()
    };
    let result: ResultType<TerminalExecResult> = async {
        tokio::time::timeout(PREPARE_TIMEOUT, async {
            let mut open = TerminalAction::new();
            open.set_open(OpenTerminal {
                terminal_id,
                rows: 24,
                cols: 160,
                ..Default::default()
            });
            cli::send_terminal_action(stream, open).await?;
            loop {
                match cli::receive_terminal_response(stream).await?.union {
                    Some(Union::Opened(opened)) if opened.terminal_id == terminal_id => {
                        if !opened.success {
                            bail!("Cannot open legacy terminal: {}", opened.message);
                        }
                        break;
                    }
                    Some(Union::Error(error)) => bail!("{}", error.message),
                    _ => {}
                }
            }
            if platform == "Linux" {
                // Old PTY services drop output when echoed input fills their bounded queue.
                send_input(
                    stream,
                    terminal_id,
                    b"stty -echo && echo && echo GDINIT\r".to_vec(),
                )
                .await?;
                terminal.until(stream, "GDINIT", "", None).await?;
            }
            send_input(stream, terminal_id, format!("{launcher}\r").into_bytes()).await?;
            terminal.until(stream, &marker, ":READY", None).await?;
            Ok::<(), hbb_common::anyhow::Error>(())
        })
        .await
        .context("Timed out preparing legacy terminal")
        .and_then(|result| result)
        .map_err(|error| super::stage::wrap(error, "prepare", "not_started"))?;

        for batch in payload.as_bytes().chunks(LINE_BYTES * BATCH_LINES) {
            tokio::time::timeout(PREPARE_TIMEOUT, async {
                let mut input = Vec::with_capacity(batch.len() + BATCH_LINES);
                for line in batch.chunks(LINE_BYTES) {
                    input.extend_from_slice(line);
                    input.push(b'\r');
                }
                send_input(stream, terminal_id, input).await?;
                if batch.len() > LINE_BYTES * (BATCH_LINES - 1) {
                    terminal.until(stream, &marker, ":ACK", None).await?;
                }
                Ok::<(), hbb_common::anyhow::Error>(())
            })
            .await
            .context("Timed out uploading legacy script")
            .and_then(|result| result)
            .map_err(|error| super::stage::wrap(error, "upload", "unknown"))?;
        }
        tokio::time::timeout(
            PREPARE_TIMEOUT,
            terminal.until(stream, &marker, ":START", None),
        )
        .await
        .context("Timed out starting legacy command")
        .and_then(|result| result)
        .map_err(|error| super::stage::wrap(error, "start", "unknown"))?;

        tokio::time::timeout(Duration::from_secs(request.timeout_seconds.into()), async {
            let mut output = Vec::new();
            let status = terminal
                .until(stream, &marker, ":END:", Some(&mut output))
                .await?;
            let exit_code = std::str::from_utf8(&status)?
                .parse::<i32>()
                .context("Invalid legacy command exit code")?;
            Ok(TerminalExecResult {
                stdout: output.into(),
                exit_code,
                ..Default::default()
            })
        })
        .await
        .context("Timed out waiting for legacy command result")
        .and_then(|result| result)
        .map_err(|error| super::stage::wrap(error, "execution", "unknown"))
    }
    .await;
    let mut close = TerminalAction::new();
    close.set_close(CloseTerminal {
        terminal_id,
        ..Default::default()
    });
    let closed = tokio::time::timeout(
        Duration::from_secs(1),
        cli::send_terminal_action(stream, close),
    )
    .await
    .context("Timed out closing legacy terminal")
    .and_then(|result| result);
    let result = result?;
    // A cleanup failure must not erase an already received execution outcome.
    if let Err(error) = closed {
        eprintln!("Command completed; could not close legacy terminal: {error:#}");
    }
    Ok(result)
}

#[derive(Default)]
struct Transcript {
    terminal_id: i32,
    frame: vec::IntoIter<u8>,
    escape: Escape,
}

impl Transcript {
    async fn line(&mut self, stream: &mut Stream) -> ResultType<Vec<u8>> {
        let mut line = Vec::new();
        loop {
            for byte in self.frame.by_ref() {
                if let Some(byte) = self.escape.text(byte) {
                    if byte == b'\n' {
                        if line.last() == Some(&b'\r') {
                            line.pop();
                        }
                        return Ok(line);
                    }
                    if line.len() == MAX_OUTPUT {
                        bail!("Legacy terminal output exceeded {MAX_OUTPUT} bytes");
                    }
                    line.push(byte);
                }
            }
            match cli::receive_terminal_response(stream).await?.union {
                Some(Union::Data(data)) if data.terminal_id == self.terminal_id => {
                    let bytes = if data.compressed {
                        zstd::bulk::decompress(&data.data, MAX_OUTPUT)
                            .context("Invalid compressed legacy terminal output")?
                    } else {
                        data.data.to_vec()
                    };
                    self.frame = bytes.into_iter();
                }
                Some(Union::Closed(closed)) if closed.terminal_id == self.terminal_id => {
                    bail!("Legacy terminal closed before reporting the command result");
                }
                Some(Union::Error(error)) => bail!("{}", error.message),
                _ => {}
            }
        }
    }

    async fn until(
        &mut self,
        stream: &mut Stream,
        marker: &str,
        event: &str,
        mut output: Option<&mut Vec<u8>>,
    ) -> ResultType<Vec<u8>> {
        let mut size = 0;
        loop {
            let line = self.line(stream).await?;
            if let Some(record) = line.strip_prefix(marker.as_bytes()) {
                if let Some(error) = record.strip_prefix(b":ERROR:") {
                    bail!("{}", String::from_utf8_lossy(error));
                }
                if let Some(value) = record.strip_prefix(event.as_bytes()) {
                    if event == ":END:" || value.is_empty() {
                        return Ok(value.to_vec());
                    }
                }
                if record == b":WINDOW" {
                    send_input(stream, self.terminal_id, b"GDOUTPUT\r".to_vec()).await?;
                    continue;
                }
                if let (Some(encoded), Some(output)) =
                    (record.strip_prefix(b":DATA:"), output.as_deref_mut())
                {
                    let data = crate::decode64(encoded).context("Invalid legacy command output")?;
                    if output.len() + data.len() > MAX_OUTPUT {
                        bail!("Legacy terminal output exceeded {MAX_OUTPUT} bytes");
                    }
                    output.extend_from_slice(&data);
                    continue;
                }
            }
            size += line.len() + 1;
            if size > MAX_OUTPUT + 1 {
                bail!("Legacy terminal output exceeded {MAX_OUTPUT} bytes");
            }
        }
    }
}

// ConPTY inserts terminal display controls between visible bytes.
#[derive(Clone, Copy, Default)]
enum Escape {
    #[default]
    Text,
    Start,
    Sequence,
    String,
    StringEnd,
}

impl Escape {
    fn text(&mut self, byte: u8) -> Option<u8> {
        use Escape::*;
        *self = match (*self, byte) {
            (Text, 0x1b) => Start,
            (Text, _) => return Some(byte),
            (Start, b'[' | 0x20..=0x2f) => Sequence,
            (Start, b']' | b'P' | b'X' | b'^' | b'_') => String,
            (Sequence, 0x20..=0x3f) => Sequence,
            (String, 0x07) | (StringEnd, b'\\') => Text,
            (String, 0x1b) => StringEnd,
            (String | StringEnd, _) => String,
            _ => Text,
        };
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_os = "linux")]
    #[test]
    fn regression_strict_legacy_shell_initializes_its_output_window() {
        use std::{
            io::Write,
            process::{Command, Stdio},
        };
        let request = TerminalExecRequest {
            shell: ExecShell::BASH.into(),
            script: b"printf STRICT_OK".to_vec().into(),
            timeout_seconds: 15,
            ..Default::default()
        };
        let (launcher, payload) = command("Linux", &request, "GDSTRICT").unwrap();
        let mut child = Command::new("bash")
            .args(["--noprofile", "--norc", "-c", &launcher])
            .env("SHELLOPTS", "nounset")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(format!("{payload}\n").as_bytes())
            .unwrap();
        let result = child.wait_with_output().unwrap();
        assert!(
            String::from_utf8_lossy(&result.stdout).contains("GDSTRICT:END:0"),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }

    #[cfg(windows)]
    #[test]
    fn regression_legacy_windows_output_records_fit_terminal_width() {
        use std::{
            io::Write,
            process::{Command, Stdio},
        };
        let marker = format!("GD{}", uuid::Uuid::new_v4().simple());
        let request = TerminalExecRequest {
            shell: ExecShell::WINDOWS_POWERSHELL.into(),
            script: b"Write-Output ('W' * 200)".to_vec().into(),
            timeout_seconds: 15,
            ..Default::default()
        };
        let (launcher, payload) = command("Windows", &request, &marker).unwrap();
        let encoded = launcher.rsplit_once(' ').unwrap().1;
        let executable = launcher.split_once(" -NoLogo").unwrap().0;
        let mut child = Command::new(executable)
            .args(["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let data = payload
            .as_bytes()
            .chunks(LINE_BYTES)
            .flat_map(|chunk| chunk.iter().copied().chain(std::iter::once(b'\n')))
            .collect::<Vec<_>>();
        child.stdin.take().unwrap().write_all(&data).unwrap();
        let result = child.wait_with_output().unwrap();
        let output = String::from_utf8(result.stdout).unwrap();
        let records = output
            .lines()
            .filter(|line| line.starts_with(&format!("{marker}:DATA:")))
            .collect::<Vec<_>>();
        assert!(
            !records.is_empty(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert!(
            records.iter().all(|line| line.len() <= 160),
            "A protocol record must fit the actual ConPTY width"
        );
        assert!(output.contains(&format!("{marker}:END:0")));
    }

    const TERMINAL_ID: i32 = 1;
    use base::message_proto::{Message, TerminalResponse};
    use hbb_common::{sodiumoxide::crypto::secretbox, tcp::FramedStream};

    #[tokio::test]
    async fn legacy_transcript_decodes_split_compressed_output_and_ignores_echo() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (peer, address) = accepted.unwrap();
        let mut peer = Stream::Tcp(FramedStream::from(peer, address));
        let mut client = Stream::Tcp(FramedStream::from(client.unwrap(), endpoint));
        let key = secretbox::gen_key();
        peer.set_key(key.clone());
        client.set_key(key);
        let server = async {
            for raw in [
                "echoed script\r\n\x1b[",
                "1mGDtest:START\x1b[0m\r\nGDtest:DA",
                "TA:5L2g5aW9\r\nGDtest:END:7\r\n",
            ] {
                let mut response = TerminalResponse::new();
                response.set_data(TerminalData {
                    terminal_id: TERMINAL_ID,
                    data: hbb_common::compress::compress(raw.as_bytes()).into(),
                    compressed: true,
                    ..Default::default()
                });
                let mut message = Message::new();
                message.set_terminal_response(response);
                peer.send(&message).await.unwrap();
            }
        };
        let controller = async {
            let mut terminal = Transcript {
                terminal_id: TERMINAL_ID,
                ..Default::default()
            };
            terminal
                .until(&mut client, "GDtest", ":START", None)
                .await
                .unwrap();
            let mut output = Vec::new();
            let code = terminal
                .until(&mut client, "GDtest", ":END:", Some(&mut output))
                .await
                .unwrap();
            assert_eq!(output, "你好".as_bytes());
            assert_eq!(code, b"7");
        };
        tokio::time::timeout(Duration::from_secs(3), async {
            tokio::join!(server, controller);
        })
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn legacy_slow_upload_and_consecutive_commands_reuse_the_stream() {
        use base::message_proto::{TerminalClosed, TerminalOpened};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (peer, address) = accepted.unwrap();
        let mut peer = Stream::Tcp(FramedStream::from(peer, address));
        let mut client = Stream::Tcp(FramedStream::from(client.unwrap(), endpoint));
        let key = secretbox::gen_key();
        peer.set_key(key.clone());
        client.set_key(key);
        let requests: Vec<_> = [
            vec![b'#'; super::super::MAX_SCRIPT],
            b"printf second".to_vec(),
        ]
        .into_iter()
        .map(|script| TerminalExecRequest {
            shell: ExecShell::BASH.into(),
            script: script.into(),
            timeout_seconds: 1,
            ..Default::default()
        })
        .collect();
        let server = async {
            for (index, request) in requests.iter().enumerate() {
                let id = index as i32 + 17;
                let Some(base::message_proto::message::Union::TerminalAction(mut action)) =
                    cli::receive(&mut peer).await.unwrap().union
                else {
                    panic!("Expected terminal open")
                };
                assert_eq!(action.take_open().terminal_id, id);
                let mut response = TerminalResponse::new();
                response.set_opened(TerminalOpened {
                    terminal_id: id,
                    success: true,
                    ..Default::default()
                });
                let mut message = Message::new();
                message.set_terminal_response(response);
                peer.send(&message).await.unwrap();
                let Some(base::message_proto::message::Union::TerminalAction(mut action)) =
                    cli::receive(&mut peer).await.unwrap().union
                else {
                    panic!("Expected bootstrap")
                };
                assert!(String::from_utf8(action.take_data().data.to_vec())
                    .unwrap()
                    .starts_with("stty -echo"));
                let mut initialized = TerminalResponse::new();
                initialized.set_data(TerminalData {
                    terminal_id: id,
                    data: b"\nGDINIT\n".to_vec().into(),
                    ..Default::default()
                });
                let mut initialized_message = Message::new();
                initialized_message.set_terminal_response(initialized);
                peer.send(&initialized_message).await.unwrap();
                let Some(base::message_proto::message::Union::TerminalAction(mut action)) =
                    cli::receive(&mut peer).await.unwrap().union
                else {
                    panic!("Expected bootstrap");
                };
                let launcher = String::from_utf8(action.take_data().data.to_vec()).unwrap();
                let encoded = launcher
                    .split("printf %s ")
                    .nth(1)
                    .unwrap()
                    .split_whitespace()
                    .next()
                    .unwrap();
                let bootstrap = String::from_utf8(crate::decode64(encoded).unwrap()).unwrap();
                let marker = bootstrap
                    .lines()
                    .next()
                    .unwrap()
                    .strip_prefix("gd_marker=")
                    .unwrap();
                let send = |text: String| {
                    let mut response = TerminalResponse::new();
                    response.set_data(TerminalData {
                        terminal_id: id,
                        data: text.into_bytes().into(),
                        ..Default::default()
                    });
                    let mut message = Message::new();
                    message.set_terminal_response(response);
                    message
                };
                peer.send(&send(format!("{marker}:READY\n"))).await.unwrap();
                let (_, payload) = command("Linux", request, marker).unwrap();
                for batch in payload.as_bytes().chunks(LINE_BYTES * BATCH_LINES) {
                    let Some(base::message_proto::message::Union::TerminalAction(mut action)) =
                        cli::receive(&mut peer).await.unwrap().union
                    else {
                        panic!("Expected script batch")
                    };
                    let data = action.take_data();
                    assert_eq!(data.terminal_id, id);
                    assert_eq!(
                        data.data
                            .iter()
                            .copied()
                            .filter(|byte| *byte != b'\r')
                            .collect::<Vec<_>>(),
                        batch
                    );
                    if batch.len() > LINE_BYTES * (BATCH_LINES - 1) {
                        tokio::time::sleep(Duration::from_millis(1600)).await;
                        peer.send(&send(format!("{marker}:ACK\n"))).await.unwrap();
                    }
                }
                peer.send(&send(format!(
                    "{marker}:START\n{marker}:DATA:5L2g5aW9\n{marker}:END:7\n"
                )))
                .await
                .unwrap();
                let Some(base::message_proto::message::Union::TerminalAction(mut action)) =
                    cli::receive(&mut peer).await.unwrap().union
                else {
                    panic!("Expected terminal close")
                };
                assert_eq!(action.take_close().terminal_id, id);
                let mut response = TerminalResponse::new();
                response.set_closed(TerminalClosed {
                    terminal_id: id,
                    ..Default::default()
                });
                let mut message = Message::new();
                message.set_terminal_response(response);
                peer.send(&message).await.unwrap();
            }
        };
        let controller = async {
            for (index, request) in requests.iter().enumerate() {
                let result = exchange(&mut client, "Linux", request.clone(), index as i32 + 17)
                    .await
                    .unwrap();
                assert_eq!(result.stdout.as_ref(), "你好".as_bytes());
                assert_eq!(result.exit_code, 7);
            }
        };
        tokio::time::timeout(Duration::from_secs(25), async {
            tokio::join!(server, controller);
        })
        .await
        .unwrap();
    }
}
