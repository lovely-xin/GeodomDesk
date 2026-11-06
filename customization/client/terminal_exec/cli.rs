use super::{validate, MAX_SCRIPT};
use crate::{
    client::{self, Client, Data, Interface, LoginConfigHandler},
    trusted_devices::client::Session,
};
use base::message_proto::{
    login_response, message, terminal_response, ExecShell, Hash, Message, PeerInfo, TerminalAction,
    TerminalExecRequest, TerminalExecResult, TestDelay, WindowsSession,
};
use hbb_common::{
    anyhow::{anyhow, Context},
    bail,
    config::LocalConfig,
    protobuf::Message as _,
    rendezvous_proto::ConnType,
    tokio, ResultType, Stream,
};
use std::{
    io::{Read, Write},
    sync::{Arc, RwLock},
    time::Duration,
};

const USAGE: &str = "geodomdesk-cli exec --id ID --shell bash|powershell|pwsh --script PATH|- [--timeout SECONDS] [--relay]";

#[derive(Clone)]
struct TerminalClient(Arc<RwLock<LoginConfigHandler>>);

#[async_trait::async_trait]
impl Interface for TerminalClient {
    fn send(&self, _: Data) {}
    fn msgbox(&self, kind: &str, title: &str, text: &str, _: &str) {
        if kind == "error" {
            eprintln!("{title}: {text}");
        }
    }
    fn handle_login_error(&self, _: &str) -> bool {
        false
    }
    fn handle_peer_info(&self, _: PeerInfo) {}
    fn set_multiple_windows_session(&self, _: Vec<WindowsSession>) {}
    fn get_lch(&self) -> Arc<RwLock<LoginConfigHandler>> {
        self.0.clone()
    }
    async fn handle_hash(
        &self,
        password: &str,
        hash: Hash,
        trust: &mut Session,
        stream: &mut Stream,
    ) -> bool {
        client::handle_hash(self.get_lch(), password, hash, self, trust, stream).await
    }
    async fn handle_login_from_ui(
        &self,
        _: String,
        _: String,
        _: String,
        _: bool,
        _: &mut Session,
        _: &mut Stream,
    ) {
    }
    async fn handle_test_delay(&self, delay: TestDelay, stream: &mut Stream) {
        client::handle_test_delay(delay, stream).await;
    }
}

pub(super) async fn receive(stream: &mut Stream) -> ResultType<Message> {
    loop {
        let frame = stream
            .next()
            .await
            .context("Remote device closed the connection")??;
        if frame.is_empty() {
            continue;
        }
        return Ok(Message::parse_from_bytes(&frame)?);
    }
}

async fn connect(
    id: String,
    relay: bool,
) -> ResultType<(Stream, Option<tokio::sync::mpsc::UnboundedSender<()>>)> {
    let token = LocalConfig::get_option("access_token");
    if token.is_empty() {
        bail!("Sign in to GeodomDesk on this computer first");
    }
    if hbb_common::config::is_incoming_only() {
        bail!("Incoming-only mode");
    }
    let mut login = LoginConfigHandler::default();
    login.initialize(id, ConnType::TERMINAL, None, relay, None, None, None);
    let interface = TerminalClient(Arc::new(RwLock::new(login)));
    let id = interface.get_id();
    let key = crate::get_key(false).await;
    let ((mut stream, direct, pk, _, _), (feedback, rendezvous)) =
        Client::start(&id, &key, &token, ConnType::TERMINAL, interface.clone()).await?;
    if !stream.is_secured() {
        bail!("Remote command execution requires an encrypted peer connection");
    }
    interface.update_direct(Some(direct));
    let mut trust = {
        let login = interface
            .0
            .read()
            .map_err(|_| anyhow!("Login state unavailable"))?;
        Session::new(&login, true, pk.as_deref())?
    };
    let keep_alive = client::hc_connection(feedback, rendezvous, &token).await;
    let password = std::env::var("GEODOMDESK_DEVICE_PASSWORD").unwrap_or_default();
    loop {
        match receive(&mut stream).await?.union {
            Some(message::Union::Hash(hash)) => {
                if !interface
                    .handle_hash(&password, hash, &mut trust, &mut stream)
                    .await
                {
                    bail!("Device authentication could not be started");
                }
            }
            Some(message::Union::LoginResponse(response)) => {
                match trust.respond(response, &interface).await.union {
                    Some(login_response::Union::PeerInfo(info)) if info.features.terminal_exec => {
                        return Ok((stream, keep_alive));
                    }
                    Some(login_response::Union::PeerInfo(_)) => {
                        bail!("The remote GeodomDesk version does not support exec");
                    }
                    Some(login_response::Union::Error(error)) => bail!("{error}"),
                    _ => {}
                }
            }
            Some(message::Union::TestDelay(delay)) => {
                interface.handle_test_delay(delay, &mut stream).await
            }
            _ => {}
        }
    }
}

pub(super) async fn exchange(
    stream: &mut Stream,
    request: TerminalExecRequest,
) -> ResultType<TerminalExecResult> {
    let mut action = TerminalAction::new();
    action.set_exec(request);
    let mut message = Message::new();
    message.set_terminal_action(action);
    stream.send(&message).await?;
    loop {
        match receive(stream).await?.union {
            Some(message::Union::TerminalResponse(response)) => match response.union {
                Some(terminal_response::Union::ExecResult(result)) => return Ok(result),
                Some(terminal_response::Union::Error(error)) => bail!("{}", error.message),
                _ => {}
            },
            Some(message::Union::TestDelay(delay)) => {
                client::handle_test_delay(delay, stream).await
            }
            _ => {}
        }
    }
}

pub async fn run_cli() -> ResultType<i32> {
    let mut args = std::env::args().skip(1);
    let first = args.next();
    if matches!(first.as_deref(), Some("--help" | "-h")) {
        println!("{USAGE}");
        return Ok(0);
    }
    if first.as_deref() != Some("exec") {
        bail!("{USAGE}");
    }
    let (mut id, mut shell, mut script) = (None, None, None);
    let mut timeout = 30u32;
    let mut relay = false;
    while let Some(flag) = args.next() {
        if flag == "--relay" {
            relay = true;
            continue;
        }
        let value = args
            .next()
            .with_context(|| format!("Missing value for {flag}"))?;
        match flag.as_str() {
            "--id" => id = Some(value),
            "--shell" => {
                shell = Some(match value.as_str() {
                    "bash" => ExecShell::BASH,
                    "powershell" => ExecShell::WINDOWS_POWERSHELL,
                    "pwsh" => ExecShell::POWERSHELL_CORE,
                    _ => bail!("Unknown shell: {value}"),
                })
            }
            "--script" => script = Some(value),
            "--timeout" => timeout = value.parse().context("Invalid timeout")?,
            _ => bail!("Unknown option: {flag}"),
        }
    }
    let id = id.filter(|id| !id.is_empty()).context("Missing --id")?;
    let shell = shell.context("Missing --shell")?;
    let path = script.context("Missing --script")?;
    let reader: Box<dyn Read> = if path == "-" {
        Box::new(std::io::stdin())
    } else {
        Box::new(std::fs::File::open(&path).context("Cannot open script")?)
    };
    let mut script = Vec::new();
    reader
        .take(MAX_SCRIPT as u64 + 1)
        .read_to_end(&mut script)?;
    let request = TerminalExecRequest {
        shell: shell.into(),
        script: script.into(),
        timeout_seconds: timeout,
        ..Default::default()
    };
    validate(&request)?;
    let (mut stream, _keep_alive) =
        tokio::time::timeout(Duration::from_secs(60), connect(id, relay))
            .await
            .context("Timed out connecting or authenticating")??;
    let result = tokio::time::timeout(
        Duration::from_secs(u64::from(timeout) + 25),
        exchange(&mut stream, request),
    )
    .await
    .context("Timed out waiting for remote execution result")??;
    if !result.error.is_empty() {
        bail!("{}", result.error);
    }
    std::io::stdout().write_all(&result.stdout)?;
    std::io::stderr().write_all(&result.stderr)?;
    Ok(result.exit_code)
}
