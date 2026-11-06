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

const TERMINAL_ID: i32 = 1;
const LINE_BYTES: usize = 512;
const BATCH_LINES: usize = 16;

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

async fn send_input(stream: &mut Stream, data: Vec<u8>) -> ResultType<()> {
    let mut action = TerminalAction::new();
    action.set_data(TerminalData {
        terminal_id: TERMINAL_ID,
        data: data.into(),
        ..Default::default()
    });
    cli::send_terminal_action(stream, action).await
}

pub(super) async fn exchange(
    stream: &mut Stream,
    platform: &str,
    request: TerminalExecRequest,
) -> ResultType<TerminalExecResult> {
    let marker = format!("GD{}", uuid::Uuid::new_v4().simple());
    let (launcher, payload) = command(platform, &request, &marker)?;
    let mut terminal = Transcript::default();
    let result = async {
        tokio::time::timeout(Duration::from_secs(15), async {
            let mut open = TerminalAction::new();
            open.set_open(OpenTerminal {
                terminal_id: TERMINAL_ID,
                rows: 24,
                cols: 160,
                ..Default::default()
            });
            cli::send_terminal_action(stream, open).await?;
            loop {
                match cli::receive_terminal_response(stream).await?.union {
                    Some(Union::Opened(opened)) if opened.terminal_id == TERMINAL_ID => {
                        if !opened.success {
                            bail!("Cannot open legacy terminal: {}", opened.message);
                        }
                        break;
                    }
                    Some(Union::Error(error)) => bail!("{}", error.message),
                    _ => {}
                }
            }
            send_input(stream, format!("{launcher}\r").into_bytes()).await?;
            terminal.until(stream, &marker, ":READY", None).await?;
            for batch in payload.as_bytes().chunks(LINE_BYTES * BATCH_LINES) {
                let mut input = Vec::with_capacity(batch.len() + BATCH_LINES);
                for line in batch.chunks(LINE_BYTES) {
                    input.extend_from_slice(line);
                    input.push(b'\r');
                }
                send_input(stream, input).await?;
                if batch.len() > LINE_BYTES * (BATCH_LINES - 1) {
                    terminal.until(stream, &marker, ":ACK", None).await?;
                }
            }
            Ok::<(), hbb_common::anyhow::Error>(())
        })
        .await
        .context("Timed out preparing legacy terminal")??;

        tokio::time::timeout(Duration::from_secs(request.timeout_seconds.into()), async {
            terminal.until(stream, &marker, ":START", None).await?;
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
        .context("Timed out waiting for legacy command result")?
    }
    .await;
    let mut close = TerminalAction::new();
    close.set_close(CloseTerminal {
        terminal_id: TERMINAL_ID,
        ..Default::default()
    });
    let _ = tokio::time::timeout(
        Duration::from_secs(1),
        cli::send_terminal_action(stream, close),
    )
    .await;
    result
}

#[derive(Default)]
struct Transcript {
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
                Some(Union::Data(data)) if data.terminal_id == TERMINAL_ID => {
                    let bytes = if data.compressed {
                        zstd::bulk::decompress(&data.data, MAX_OUTPUT)
                            .context("Invalid compressed legacy terminal output")?
                    } else {
                        data.data.to_vec()
                    };
                    self.frame = bytes.into_iter();
                }
                Some(Union::Closed(closed)) if closed.terminal_id == TERMINAL_ID => {
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
            let mut terminal = Transcript::default();
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
}
