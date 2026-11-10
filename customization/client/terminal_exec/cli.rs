use super::{auth, input, legacy, output, session, validate, MAX_OUTPUT, MAX_SCRIPT};
use crate::{
    client::{self, Client, Data, Interface, LoginConfigHandler},
    trusted_devices::client::Session,
};
use base::message_proto::{
    login_response, message, misc, terminal_response, Auth2FA, ExecShell, Hash, Message, PeerInfo,
    TerminalAction, TerminalExecRequest, TerminalExecResult, TerminalResponse, TestDelay,
    WindowsSession,
};
use hbb_common::{
    anyhow::{anyhow, Context},
    bail,
    protobuf::Message as _,
    rendezvous_proto::ConnType,
    tokio, ResultType, Stream,
};
use std::{
    io::Write,
    sync::{Arc, RwLock},
    time::{Duration, Instant},
};

const USAGE: &str =
    "Usage: geodomdesk-cli exec --id ID --shell bash|powershell|pwsh --script PATH|-
       geodomdesk-cli exec --id ID --shell bash|powershell|pwsh --command TEXT
       geodomdesk-cli session --id ID [--shell SHELL] [authentication options]
       geodomdesk-cli probe --id ID [authentication options]
  session                 Read JSON Lines from stdin and reuse one authenticated connection
                          {\"shell\":\"bash\",\"script\":\"pwd\",\"timeout\":120}
                          Results: JSON Lines; stdout/stderr are Base64 encoded
  --timeout SECONDS       Command timeout (1-300, default 15)
  --connect-timeout SEC    Connection and device authentication timeout (1-300, default 60)
  --json                  Machine-readable exec results, including failures
  --expect-platform NAME  Refuse to execute if the authenticated target platform differs
  --expect-hostname NAME  Refuse to execute if the authenticated target hostname differs
  --continue-on-error     Session only: keep queued commands after a completed command fails
                          Default: cancel unsent queued commands on failure; never replay
  --command TEXT          Execute an inline script instead of --script
  session controls        {\"op\":\"status|reconnect|cancel|close\",\"id\":\"request-id\"}
                          Commands may set \"connection\":\"fresh\" to replace the transport
                          exec processes always create independent fresh connections
  probe                   Authenticate and report transport and terminal capabilities; no command is run
  --relay                 Use a relay instead of trying a direct connection
  --account-user USER     Sign in to GeodomDesk for this invocation
  --admin-user USER       Authenticate to the target Windows administrator terminal
  --ask-device-password   Prompt for a device access password if absent from the environment
  --non-interactive       Read credentials from the environment or saved session, never prompt

Environment: GEODOMDESK_ACCOUNT_PASSWORD, GEODOMDESK_DEVICE_PASSWORD,
             GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE";

pub(super) struct Connection {
    pub(super) id: String,
    pub(super) stream: Stream,
    pub(super) peer: PeerInfo,
    direct: bool,
    transport: &'static str,
    _keep_alive: Option<tokio::sync::mpsc::UnboundedSender<()>>,
    // The framed stream alone does not retain KCP's UDP pump.
    _kcp: Option<crate::kcp_stream::KcpStream>,
}

#[derive(Clone)]
pub(super) struct ConnectOptions {
    pub(super) id: String,
    pub(super) relay: bool,
    pub(super) token: String,
    pub(super) device_password: String,
    pub(super) admin: Option<auth::Credentials>,
    pub(super) timeout: u32,
    pub(super) expected_platform: Option<String>,
    pub(super) expected_hostname: Option<String>,
}
impl ConnectOptions {
    pub(super) async fn open(&self) -> ResultType<Connection> {
        let connection = tokio::time::timeout(
            Duration::from_secs(self.timeout.into()),
            connect(
                self.id.clone(),
                self.relay,
                self.token.clone(),
                self.device_password.clone(),
                self.admin.clone(),
            ),
        )
        .await
        .context("Timed out connecting or authenticating")
        .and_then(|result| result)
        .map_err(|error| super::stage::wrap(error, "connect", "not_started"))?;
        for (expected, actual, label) in [
            (
                &self.expected_platform,
                &connection.peer.platform,
                "platform",
            ),
            (
                &self.expected_hostname,
                &connection.peer.hostname,
                "hostname",
            ),
        ] {
            if let Some(expected) = expected {
                if !expected.eq_ignore_ascii_case(actual) {
                    return Err(super::stage::wrap(
                        anyhow!("Target {label} mismatch: expected {expected}, received {actual}"),
                        "target",
                        "not_started",
                    ));
                }
            }
        }
        Ok(connection)
    }
}

pub(super) fn connection_info(
    connection: &Connection,
    timeout: u32,
    connecting: Duration,
    setup: Duration,
) -> serde_json::Value {
    serde_json::json!({
        "event":"ready","connection_id":connection.id,"session_protocol":2,
        "platform":connection.peer.platform,"hostname":connection.peer.hostname,"version":connection.peer.version,
        "direct":connection.direct,"transport":connection.transport,"encrypted":connection.stream.is_secured(),
        "connect_ms":connecting.as_millis(),"setup_ms":setup.as_millis(),
        "terminal":connection.peer.features.terminal,"terminal_exec":connection.peer.features.terminal_exec,
        "protocol":if connection.peer.features.terminal_exec {"exec"} else if connection.peer.features.terminal {"legacy"} else {"none"},
        "max_script_bytes":MAX_SCRIPT,"max_output_bytes":MAX_OUTPUT,"default_timeout":timeout,
        "session_controls":["status","reconnect","cancel","close"],"connection_policy":["reuse","fresh"]
    })
}

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
        let message = Message::parse_from_bytes(&frame)?;
        if let Some(message::Union::Misc(misc)) = &message.union {
            if let Some(misc::Union::CloseReason(reason)) = &misc.union {
                bail!("Remote device closed the connection: {reason}");
            }
        }
        return Ok(message);
    }
}

async fn connect(
    id: String,
    relay: bool,
    token: String,
    device_password: String,
    mut admin: Option<auth::Credentials>,
) -> ResultType<Connection> {
    let mut login = LoginConfigHandler::default();
    login.initialize(id, ConnType::TERMINAL, None, relay, None, None, None);
    login.terminal_one_shot = true;
    login.is_terminal_admin = admin.is_some();
    let is_admin = login.is_terminal_admin;
    let interface = TerminalClient(Arc::new(RwLock::new(login)));
    let id = interface.get_id();
    let key = crate::get_key(false).await;
    let ((mut stream, direct, pk, kcp, transport), (feedback, rendezvous)) =
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
    let mut sent_device_code = false;
    loop {
        match receive(&mut stream).await?.union {
            Some(message::Union::Hash(hash)) => {
                if !interface
                    .handle_hash(&device_password, hash, &mut trust, &mut stream)
                    .await
                {
                    bail!("Device authentication could not be started");
                }
                if is_admin {
                    let credentials = admin.take().context("Administrator login was rejected")?;
                    auth::send_admin_login(&interface, credentials, &mut trust, &mut stream)
                        .await?;
                }
            }
            Some(message::Union::LoginResponse(response)) => {
                match trust.respond(response, &interface).await.union {
                    Some(login_response::Union::PeerInfo(info)) => {
                        if is_admin && (admin.is_some() || info.platform != "Windows") {
                            bail!("Administrator terminal login requires a Windows target");
                        }
                        stream.set_max_packet_length(1024 * 1024);
                        return Ok(Connection {
                            id: uuid::Uuid::new_v4().to_string(),
                            stream,
                            peer: info,
                            direct,
                            transport,
                            _keep_alive: keep_alive,
                            _kcp: kcp,
                        });
                    }
                    Some(login_response::Union::Error(error)) => match error.as_str() {
                        client::REQUIRE_2FA if !sent_device_code => {
                            let mut message = Message::new();
                            message.set_auth_2fa(Auth2FA {
                                code: auth::device_code()?,
                                ..Default::default()
                            });
                            stream.send(&message).await?;
                            sent_device_code = true;
                        }
                        client::LOGIN_MSG_PASSWORD_EMPTY | client::LOGIN_MSG_PASSWORD_WRONG => {
                            bail!("{error}. Use --ask-device-password or set GEODOMDESK_DEVICE_PASSWORD");
                        }
                        _ => bail!("{error}"),
                    },
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
    send_terminal_action(stream, action).await?;
    loop {
        match receive_terminal_response(stream).await?.union {
            Some(terminal_response::Union::ExecResult(result)) => return Ok(result),
            Some(terminal_response::Union::Error(error)) => bail!("{}", error.message),
            _ => {}
        }
    }
}

pub(super) async fn send_terminal_action(
    stream: &mut Stream,
    action: TerminalAction,
) -> ResultType<()> {
    let mut message = Message::new();
    message.set_terminal_action(action);
    stream.send(&message).await
}

pub(super) async fn receive_terminal_response(stream: &mut Stream) -> ResultType<TerminalResponse> {
    loop {
        match receive(stream).await?.union {
            Some(message::Union::TerminalResponse(response)) => return Ok(response),
            Some(message::Union::TestDelay(delay)) => {
                client::handle_test_delay(delay, stream).await
            }
            _ => {}
        }
    }
}

pub async fn run_cli(args: impl Iterator<Item = String>) -> ResultType<i32> {
    let args: Vec<_> = args.collect();
    let json = input::wants_json(&args)
        || matches!(args.first().map(String::as_str), Some("session" | "probe"));
    let started = Instant::now();
    let result = run(args.into_iter(), started).await;
    if json {
        if let Err(error) = &result {
            output::write(output::failure(None, error, true, started.elapsed()))?;
        }
    }
    result
}

async fn run(mut args: impl Iterator<Item = String>, started: Instant) -> ResultType<i32> {
    let first = args.next();
    if matches!(first.as_deref(), Some("--help" | "-h")) {
        println!("{USAGE}");
        return Ok(0);
    }
    if matches!(first.as_deref(), Some("--version" | "-V")) {
        println!("GeodomDesk {}", crate::VERSION);
        return Ok(0);
    }
    let session = first.as_deref() == Some("session");
    let probe = first.as_deref() == Some("probe");
    if !session && !probe && first.as_deref() != Some("exec") {
        bail!("{USAGE}");
    }
    let (mut id, mut shell, mut script) = (None, None, None);
    let mut command = None;
    let mut json = false;
    let mut connect_timeout = 60u32;
    let mut expected_platform = None;
    let mut expected_hostname = None;
    let mut continue_on_error = false;
    let (mut account_user, mut admin_user) = (None, None);
    let mut timeout = 15u32;
    let mut relay = false;
    let mut ask_device_password = false;
    let mut non_interactive = false;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(0);
            }
            "--relay" => relay = true,
            "--json" => json = true,
            "--continue-on-error" => continue_on_error = true,
            "--ask-device-password" => ask_device_password = true,
            "--non-interactive" => non_interactive = true,
            _ => {
                let value = args
                    .next()
                    .with_context(|| format!("Missing value for {flag}"))?;
                match flag.as_str() {
                    "--id" => {
                        if id.is_some() {
                            bail!("Specify --id exactly once; the target must not be silently replaced");
                        }
                        id = Some(value);
                    }
                    "--expect-platform" => {
                        if expected_platform.is_some() {
                            bail!("Specify --expect-platform only once");
                        }
                        expected_platform = Some(value);
                    }
                    "--expect-hostname" => {
                        if expected_hostname.is_some() {
                            bail!("Specify --expect-hostname only once");
                        }
                        expected_hostname = Some(value);
                    }
                    "--shell" => {
                        shell = Some(match value.as_str() {
                            "bash" => ExecShell::BASH,
                            "powershell" => ExecShell::WINDOWS_POWERSHELL,
                            "pwsh" => ExecShell::POWERSHELL_CORE,
                            _ => bail!("Unknown shell: {value}"),
                        })
                    }
                    "--script" => {
                        if script.is_some() {
                            bail!("Specify --script only once");
                        }
                        script = Some(value);
                    }
                    "--command" => {
                        if command.is_some() {
                            bail!("Specify --command only once");
                        }
                        command = Some(value);
                    }
                    "--connect-timeout" => {
                        connect_timeout = value.parse().context("Invalid connection timeout")?
                    }
                    "--timeout" => timeout = value.parse().context("Invalid timeout")?,
                    "--account-user" => account_user = Some(value),
                    "--admin-user" => admin_user = Some(value),
                    _ => bail!("Unknown option: {flag}"),
                }
            }
        }
    }
    let id = id
        .filter(|id| !id.trim().is_empty())
        .context("Missing --id")?;
    if !(1..=super::MAX_TIMEOUT).contains(&timeout) {
        bail!(
            "Timeout must be between 1 and {} seconds",
            super::MAX_TIMEOUT
        );
    }
    if !(1..=300).contains(&connect_timeout) {
        bail!("Connection timeout must be between 1 and 300 seconds");
    }
    if (session || probe) && (script.is_some() || command.is_some()) {
        bail!("Only exec accepts --script or --command");
    }
    if probe && shell.is_some() {
        bail!("probe does not execute a shell");
    }
    if script.is_some() && command.is_some() {
        bail!("Use either --script or --command");
    }
    if continue_on_error && !session {
        bail!("--continue-on-error applies only to session");
    }
    for expected in [&expected_platform, &expected_hostname]
        .into_iter()
        .flatten()
    {
        if expected.trim().is_empty() {
            bail!("Expected target properties must not be empty");
        }
    }
    let is_admin = admin_user.is_some();
    if is_admin && shell == Some(ExecShell::BASH) {
        bail!("--admin-user requires powershell or pwsh");
    }
    let request = if session || probe {
        None
    } else {
        let shell = shell.context("Missing --shell")?;
        let script = match (script, command) {
            (Some(path), None) => input::read_script(&path, true)?,
            (None, Some(command)) => command.into_bytes(),
            _ => bail!("Missing --script or --command"),
        };
        let request = TerminalExecRequest {
            shell: shell.into(),
            script: script.into(),
            timeout_seconds: timeout,
            ..Default::default()
        };
        validate(&request)?;
        Some(request)
    };
    if hbb_common::config::is_incoming_only() {
        bail!("Incoming-only mode");
    }
    let token = auth::account_token(account_user, non_interactive).await?;
    let device_password = auth::device_password(ask_device_password, non_interactive)?;
    let admin = auth::admin_credentials(admin_user, non_interactive)?;
    let connecting = Instant::now();
    let options = ConnectOptions {
        id,
        relay,
        token,
        device_password,
        admin,
        timeout: connect_timeout,
        expected_platform,
        expected_hostname,
    };
    let mut connection = options.open().await?;
    let info = connection_info(
        &connection,
        timeout,
        connecting.elapsed(),
        started.elapsed(),
    );
    if probe {
        output::write(info)?;
        return Ok(0);
    }
    if session {
        return session::run(
            connection,
            options,
            is_admin,
            timeout,
            shell,
            info,
            continue_on_error,
        )
        .await;
    }
    let execution = Instant::now();
    let request = request.context("Missing command")?;
    let result = session::execute(&mut connection, request, 1)
        .await
        .map_err(|error| error.context(super::stage::ConnectionId(connection.id.clone())))?;
    if json {
        let mut value = output::result(None, &result, execution.elapsed());
        value["connection"] = info;
        output::write(value)?;
        return Ok(if result.error.is_empty() {
            result.exit_code
        } else {
            1
        });
    }
    if !result.error.is_empty() {
        bail!("{}", result.error);
    }
    std::io::stdout().write_all(&result.stdout)?;
    std::io::stderr().write_all(&result.stderr)?;
    Ok(result.exit_code)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hbb_common::{
        sodiumoxide::crypto::secretbox,
        tcp::FramedStream,
        tokio::net::{TcpListener, TcpStream},
    };
    use sha2::{Digest, Sha256};

    #[tokio::test]
    async fn ambiguous_targets_and_payloads_are_rejected_before_authentication() {
        for (args, reason) in [
            (
                vec!["exec", "--id", "1", "--id", "2"],
                "Specify --id exactly once",
            ),
            (
                vec!["exec", "--id", "1", "--command", "a", "--command", "b"],
                "Specify --command only once",
            ),
            (
                vec!["exec", "--id", "1", "--script", "a", "--script", "b"],
                "Specify --script only once",
            ),
            (
                vec![
                    "probe",
                    "--id",
                    "1",
                    "--expect-platform",
                    "Linux",
                    "--expect-platform",
                    "Windows",
                ],
                "Specify --expect-platform only once",
            ),
            (
                vec![
                    "probe",
                    "--id",
                    "1",
                    "--expect-hostname",
                    "host",
                    "--expect-hostname",
                    "other",
                ],
                "Specify --expect-hostname only once",
            ),
            (
                vec!["probe", "--id", "1", "--expect-hostname", " "],
                "Expected target properties must not be empty",
            ),
            (
                vec!["exec", "--id", "1", "--continue-on-error"],
                "--continue-on-error applies only to session",
            ),
        ] {
            let error = run(args.into_iter().map(str::to_owned), Instant::now())
                .await
                .unwrap_err();
            assert!(
                format!("{error:#}").contains(reason),
                "Expected {reason}, received {error:#}"
            );
        }
    }

    #[tokio::test]
    async fn remote_close_reason_reaches_the_caller() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (client, accepted) = tokio::join!(TcpStream::connect(addr), listener.accept());
        let (server, client_addr) = accepted.unwrap();
        let mut client = Stream::Tcp(FramedStream::from(client.unwrap(), addr));
        let mut server = Stream::Tcp(FramedStream::from(server, client_addr));
        let mut message = Message::new();
        message
            .mut_misc()
            .set_close_reason("Terminal permission denied".into());
        server.send(&message).await.unwrap();
        let error = receive(&mut client).await.unwrap_err();
        assert!(error.to_string().contains("Terminal permission denied"));
    }

    #[tokio::test]
    async fn admin_challenge_sends_windows_login_and_device_hash() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let client = TcpStream::connect(addr).await.unwrap();
        let (server, client_addr) = listener.accept().await.unwrap();
        let mut client = Stream::Tcp(FramedStream::from(client, addr));
        let mut server = Stream::Tcp(FramedStream::from(server, client_addr));
        let key = secretbox::gen_key();
        client.set_key(key.clone());
        server.set_key(key);

        let mut login = LoginConfigHandler::default();
        login.initialize(
            "987654321".into(),
            ConnType::TERMINAL,
            None,
            false,
            None,
            None,
            None,
        );
        login.is_terminal_admin = true;
        login.terminal_one_shot = true;
        let interface = TerminalClient(Arc::new(RwLock::new(login)));
        let mut trust = Session::default();
        let hash = Hash {
            salt: "salt".into(),
            challenge: "challenge".into(),
            ..Default::default()
        };
        assert!(
            interface
                .handle_hash("device-secret", hash, &mut trust, &mut client)
                .await
        );
        let admin = auth::Credentials {
            username: "DOMAIN\\Admin".into(),
            password: "windows-secret".into(),
        };
        auth::send_admin_login(&interface, admin, &mut trust, &mut client)
            .await
            .unwrap();
        let response = tokio::time::timeout(Duration::from_secs(2), receive(&mut server))
            .await
            .unwrap()
            .unwrap();
        let Some(message::Union::LoginRequest(request)) = response.union else {
            panic!("Expected administrator login request");
        };
        let mut challenge = Sha256::new();
        challenge.update(Sha256::digest(b"device-secretsalt"));
        challenge.update(b"challenge");
        assert_eq!(&request.password[..], &challenge.finalize()[..]);
        assert_eq!(request.os_login.username, "DOMAIN\\Admin");
        assert_eq!(request.os_login.password, "windows-secret");
        assert!(request.has_terminal());
        assert!(request.terminal().service_id.is_empty());
    }
}
