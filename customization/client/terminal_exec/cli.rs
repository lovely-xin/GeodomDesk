use super::{auth, input, output, session, validate, MAX_OUTPUT, MAX_SCRIPT};
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
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, RwLock,
    },
    time::{Duration, Instant},
};

pub(super) const USAGE: &str =
    "Usage: geodomdesk-cli exec --id ID --shell bash|powershell|pwsh --script PATH|-
       geodomdesk-cli exec --id ID --shell bash|powershell|pwsh --command TEXT
       geodomdesk-cli session --id ID [--shell SHELL] [authentication options]
       geodomdesk-cli probe --id ID [authentication options]
  session                 Read JSON Lines from stdin and reuse one authenticated connection
                          {\"shell\":\"bash\",\"script\":\"pwd\",\"timeout\":120}
                          Results: JSON Lines; stdout/stderr are Base64 encoded
  tunnel open --id ID --remote-port PORT [--remote-host HOST] [--local-port PORT]
                          Own a loopback TCP mapping (default: free local port; use --relay to force relay)
                          Keep stdin open; JSON controls status/close; EOF or exit cleans up
  tunnel list [--id ID]    List only live CLI-owned tunnels; includes owner and real status
  tunnel close --tunnel-id UUID [--id ID]
                          Close that CLI-owned mapping and all of its connections
  --transport MODE        auto (default): Bash uses SSH over a managed TCP relay
                          terminal: use GeodomDesk terminal; ssh: require SSH Bash
                          Session without --shell keeps the terminal transport
  --ssh-user USER         Target SSH login (default: authenticated peer username)
  --ssh-host HOST         Target SSH host inside peer relay (default 127.0.0.1)
  --ssh-port PORT         Target SSH service port (default 22)
  --ssh-identity PATH     Use an SSH private key; otherwise agent/default keys or password
  --ask-ssh-password      Prompt without echo; automation: GEODOMDESK_SSH_PASSWORD
  --ssh-known-hosts PATH  Dedicated host-key file (default: client configuration)
  --ssh-strict-host-key   Require a previously trusted host key; otherwise trust first key
                          Changed keys always fail; no fallback or command replay
  --timeout SECONDS       Command timeout (1-300, default 15)
  --connect-timeout SEC    Connection and device authentication timeout (1-300, default 60)
  --json                  Machine-readable exec results, including failures
  --expect-platform NAME  Refuse to execute if the authenticated target platform differs
  --expect-hostname NAME  Refuse to execute if the authenticated target hostname differs
  --continue-on-error     Session only: keep queued commands after a completed command fails
                          Default: cancel unsent queued commands on failure; never replay
  --command TEXT          Execute an inline script instead of --script
  session controls        {\"op\":\"status|reconnect|cancel|close|exit\",\"id\":\"request-id\"}
                          Commands may set \"connection\":\"fresh\" to replace the transport
                          exec processes always create independent fresh connections
  probe                   Authenticate and report capabilities; no user script is run
  --relay                 Use a relay instead of trying a direct connection
  --account-user USER     Sign in to GeodomDesk for this invocation
  --admin-user USER       Authenticate to the target Windows administrator terminal
  --ask-device-password   Prompt for a device access password if absent from the environment
  --non-interactive       Read credentials from the environment or saved session, never prompt

Environment: GEODOMDESK_ACCOUNT_PASSWORD, GEODOMDESK_DEVICE_PASSWORD,
             GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE, GEODOMDESK_SSH_PASSWORD";

pub(super) enum Backend {
    Terminal(Stream),
    Ssh(super::ssh::Session),
    Detached,
}
pub(super) struct Connection {
    pub(super) id: String,
    pub(super) backend: Backend,
    pub(super) peer: PeerInfo,
    direct: bool,
    transport: &'static str,
    _keep_alive: Option<tokio::sync::mpsc::UnboundedSender<()>>,
    // The framed stream alone does not retain KCP's UDP pump.
    _kcp: Option<crate::kcp_stream::KcpStream>,
    pub(super) setup_errors: Vec<super::stage::SetupError>,
    connect_elapsed: Duration,
}
impl Drop for Connection {
    fn drop(&mut self) {
        if let Backend::Terminal(stream) = &self.backend {
            stream.close_webrtc();
        }
    }
}

impl Connection {
    pub(super) fn from_ssh(session:super::ssh::Session,elapsed:Duration)->Self {
        let direct = session.tunnel.direct();
        let transport = session.tunnel.transport();
        Self{id:uuid::Uuid::new_v4().to_string(),peer:session.tunnel.peer.clone(),
            setup_errors:session.tunnel.setup_errors.clone(),
            backend:Backend::Ssh(session),direct,transport,_keep_alive:None,_kcp:None,
            connect_elapsed:elapsed}
    }
    pub(super) fn direct(&self) -> bool {
        self.direct
    }
    pub(super) fn transport(&self) -> &'static str {
        self.transport
    }
    pub(super) fn into_forward_parts(
        mut self,
    ) -> ResultType<(
        Stream,
        Option<tokio::sync::mpsc::UnboundedSender<()>>,
        Option<crate::kcp_stream::KcpStream>,
        PeerInfo,
        bool,
        &'static str,
    )> {
        let stream = match std::mem::replace(&mut self.backend, Backend::Detached) {
            Backend::Terminal(stream) => stream,
            _ => bail!("Port forwarding requires a raw or multiplexed terminal stream"),
        };
        Ok((
            stream,
            self._keep_alive.take(),
            self._kcp.take(),
            std::mem::take(&mut self.peer),
            self.direct,
            self.transport,
        ))
    }
    pub(super) fn encrypted(&self)->bool {match &self.backend {Backend::Terminal(stream)=>stream.is_secured(),Backend::Ssh(_)=>true,Backend::Detached=>false}}
    pub(super) fn terminal_stream(&mut self)->ResultType<&mut Stream> {
        match &mut self.backend {Backend::Terminal(stream)=>Ok(stream),_=>bail!("This connection has no terminal stream")}
    }
    pub(super) async fn receive_idle(&mut self)->ResultType<Message> {
        match &mut self.backend {
            Backend::Terminal(stream)=>receive(stream).await,
            Backend::Ssh(session)=>{session.idle().await?;bail!("SSH channel closed")},
            Backend::Detached => bail!("Connection detached"),
        }
    }
    pub(super) fn command_error(
        &self,
        mut error: hbb_common::anyhow::Error,
    ) -> hbb_common::anyhow::Error {
        if error.downcast_ref::<super::stage::SetupHistory>().is_none()
            && !self.setup_errors.is_empty()
        {
            error = error.context(super::stage::SetupHistory(self.setup_errors.clone()));
        }
        if super::stage::describe(&error).1 != "connect"
            && error.downcast_ref::<super::stage::ConnectionId>().is_none()
        {
            error = error.context(super::stage::ConnectionId(self.id.clone()));
        }
        error
    }
}

pub(super) fn normalize_device_id(raw: &str) -> (String, String, bool) {
    let trimmed = raw.trim();
    let (peer_raw, server_opt) = match trimmed.split_once('@') {
        Some((peer, server)) => (peer.trim(), Some(server.trim())),
        None => (trimmed, None),
    };
    let (peer_stripped, peer_relay_suffix) = if let Some(prefix) = peer_raw.strip_suffix("/r") {
        (prefix.trim(), true)
    } else if let Some(prefix) = peer_raw.strip_suffix(r"\r") {
        (prefix.trim(), true)
    } else {
        (peer_raw, false)
    };
    let (server_opt, server_relay_suffix) = match server_opt {
        Some(server) if !server.contains('?') => {
            if let Some(prefix) = server.strip_suffix("/r") {
                (Some(prefix.trim()), true)
            } else if let Some(prefix) = server.strip_suffix(r"\r") {
                (Some(prefix.trim()), true)
            } else {
                (Some(server), false)
            }
        }
        other => (other, false),
    };
    let relay_suffix = peer_relay_suffix || server_relay_suffix;
    let peer = if !peer_stripped.is_empty()
        && peer_stripped
            .chars()
            .all(|c| c.is_ascii_digit() || c.is_whitespace())
    {
        peer_stripped
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect::<String>()
    } else {
        peer_stripped.to_owned()
    };
    let (connect_id, canonical_id) = match server_opt {
        Some(server) if !server.is_empty() => {
            let server_host = server.split('?').next().unwrap_or(server);
            (format!("{peer}@{server}"), format!("{peer}@{server_host}"))
        }
        _ => (peer.clone(), peer),
    };
    (connect_id, canonical_id, relay_suffix)
}

#[derive(Clone)]
pub(super) struct ConnectOptions {
    pub(super) connect_id: String,
    pub(super) id: String,
    pub(super) relay: bool,
    pub(super) token_override: Option<String>,
    pub(super) device_password: String,
    pub(super) admin: Option<auth::Credentials>,
    pub(super) timeout: u32,
    pub(super) expected_platform: Option<String>,
    pub(super) expected_hostname: Option<String>,
    pub(super) ssh: Option<super::ssh::Options>,
}
impl ConnectOptions {
    fn current_token(&self) -> ResultType<String> {
        if let Some(token) = &self.token_override {
            return Ok(token.clone());
        }
        let token = hbb_common::config::LocalConfig::get_option_from_file("access_token");
        if token.is_empty() {
            bail!("Sign in to GeodomDesk first or supply --account-user");
        }
        Ok(token)
    }
    pub(super) fn credential_bound(&self) -> bool {
        self.admin.is_some() || !self.device_password.is_empty() || std::env::var_os("GEODOMDESK_DEVICE_CODE").is_some()
    }
    fn verify_target(&self, connection: &Connection) -> ResultType<()> {
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
                    return Err(connection.command_error(super::stage::wrap(
                        anyhow!("Target {label} mismatch: expected {expected}, received {actual}"),
                        "target",
                        "not_started",
                    )));
                }
            }
        }
        Ok(())
    }
    pub(super) async fn open_forward(&self, host: &str, port: u16, allow_mux: bool) -> ResultType<Connection> {
        let started = Instant::now();
        let token = self.current_token()?;
        let (mut connection, history) = super::stage::connect_with_recovery(
            Duration::from_secs(self.timeout.into()),
            self.credential_bound(),
            || {
                connect_for(
                    self.connect_id.clone(),
                    self.relay,
                    token.clone(),
                    self.device_password.clone(),
                    None,
                    Some((host, port, allow_mux)),
                    true,
                )
            },
        )
        .await?;
        connection.connect_elapsed = started.elapsed();
        connection.setup_errors = history;
        self.verify_target(&connection)?;
        Ok(connection)
    }
    pub(super) async fn open(&self) -> ResultType<Connection> {
        self.open_with_keep_alive(true).await
    }
    pub(super) async fn open_with_keep_alive(&self, keep_alive: bool) -> ResultType<Connection> {
        if let Some(ssh) = &self.ssh {
            return tokio::time::timeout(
                Duration::from_secs(self.timeout.into()),
                super::ssh::open(self, ssh),
            )
            .await
            .context("SSH setup timed out before script dispatch")
            .and_then(|v| v)
            .map_err(|error| {
                if error.downcast_ref::<super::stage::Stage>().is_none() {
                    super::stage::wrap(error, "ssh", "not_started")
                } else {
                    error
                }
            });
        }
        let started = Instant::now();
        let token = self.current_token()?;
        let (mut connection, history) = super::stage::connect_with_recovery(
            Duration::from_secs(self.timeout.into()),
            self.credential_bound(),
            || {
                connect_for(
                    self.connect_id.clone(),
                    self.relay,
                    token.clone(),
                    self.device_password.clone(),
                    self.admin.clone(),
                    None,
                    keep_alive,
                )
            },
        )
        .await?;
        connection.connect_elapsed = started.elapsed();
        connection.setup_errors = history;
        self.verify_target(&connection)?;
        Ok(connection)
    }
}

pub(super) fn connection_info(
    connection: &Connection,
    timeout: u32,
    connecting: Duration,
    setup: Duration,
) -> serde_json::Value {
    let mut info = serde_json::json!({
        "event":"ready","connection_id":connection.id,"session_protocol":2,
        "cli_version":crate::VERSION,"cli_build":crate::geodomdesk_update::BUILD_NUMBER,
        "platform":connection.peer.platform,"hostname":connection.peer.hostname,"version":connection.peer.version,
        "direct":connection.direct,"transport":connection.transport,"encrypted":connection.encrypted(),
        "connect_ms":connecting.as_millis(),"setup_ms":setup.as_millis(),
        "terminal":connection.peer.features.terminal,"terminal_exec":connection.peer.features.terminal_exec,
        "protocol":if connection.peer.features.terminal_exec {"exec"} else if connection.peer.features.terminal {"legacy"} else {"none"},
        "max_script_bytes":MAX_SCRIPT,"max_output_bytes":MAX_OUTPUT,"default_timeout":timeout,
        "session_controls":["status","reconnect","cancel","close","exit"],"connection_policy":["reuse","fresh"]
    });
    if let Backend::Ssh(session)=&connection.backend {
        info["protocol"]=serde_json::json!("ssh");
        info["command_transport"]=serde_json::json!("ssh-relay");
        info["ssh_destination"]=serde_json::json!(session.destination);
        info["tunnel"]=session.tunnel.snapshot();
    } else {info["command_transport"]=serde_json::json!("terminal");}
    if !connection.setup_errors.is_empty() {
        info["setup_errors"] = serde_json::json!(connection.setup_errors);
    }
    info
}

#[derive(Clone)]
struct TerminalClient {
    lch: Arc<RwLock<LoginConfigHandler>>,
    prompted_password: Arc<AtomicBool>,
}

impl TerminalClient {
    fn new(login: LoginConfigHandler) -> Self {
        Self {
            lch: Arc::new(RwLock::new(login)),
            prompted_password: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[async_trait::async_trait]
impl Interface for TerminalClient {
    fn send(&self, _: Data) {}
    fn msgbox(&self, kind: &str, title: &str, text: &str, _: &str) {
        if kind == "input-password" {
            self.prompted_password.store(true, Ordering::Release);
        } else if kind == "error" {
            eprintln!("{title}: {text}");
        }
    }
    fn handle_login_error(&self, _: &str) -> bool {
        false
    }
    fn handle_peer_info(&self, _: PeerInfo) {}
    fn set_multiple_windows_session(&self, _: Vec<WindowsSession>) {}
    fn get_lch(&self) -> Arc<RwLock<LoginConfigHandler>> {
        self.lch.clone()
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
            .context(super::stage::PeerDisconnected)??;
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

async fn connect_for(
    id: String,
    relay: bool,
    token: String,
    device_password: String,
    mut admin: Option<auth::Credentials>,
    forward: Option<(&str, u16, bool)>,
    keep_alive: bool,
) -> ResultType<Connection> {
    let conn_type = if forward.is_some() {
        ConnType::PORT_FORWARD
    } else {
        ConnType::TERMINAL
    };
    let mut login = LoginConfigHandler::default();
    login.initialize(id, conn_type, None, relay, None, None, None, "");
    if let Some((host, port, allow_mux)) = forward {
        login.port_forward = (host.to_owned(), i32::from(port));
        login.port_forward_multiplex = allow_mux;
    }
    login.terminal_one_shot = true;
    login.is_terminal_admin = admin.is_some();
    let is_admin = login.is_terminal_admin;
    let interface = TerminalClient::new(login);
    let id = interface.get_id();
    let key = crate::get_key(true).await;
    let ((mut stream, direct, pk, kcp, transport), (feedback, rendezvous)) =
        Client::start(&id, &key, &token, conn_type, interface.clone())
            .await
            .map_err(|err| super::stage::wrap(err, "connect", "not_started"))?;
    if !stream.is_secured() {
        return Err(super::stage::wrap(anyhow!("Remote command execution requires an encrypted peer connection"), "security", "not_started"));
    }
    interface.update_direct(Some(direct));
    let mut trust = {
        let login = interface
            .lch
            .read()
            .map_err(|_| anyhow!("Login state unavailable"))?;
        Session::new(&login, true, pk.as_deref())?
    };
    let keep_alive = if keep_alive {
        client::hc_connection(feedback, rendezvous, &token).await
    } else {
        None
    };
    let mut sent_device_code = false;
    let mut credentials_sent = false;
    loop {
        let msg = if interface.prompted_password.load(Ordering::Acquire)
            && !is_admin
            && device_password.is_empty()
        {
            match tokio::time::timeout(Duration::from_secs(10), receive(&mut stream)).await {
                Ok(res) => res.map_err(|err| {
                    super::stage::wrap(
                        anyhow!("{err} (No saved device password; use --ask-device-password or set GEODOMDESK_DEVICE_PASSWORD)"),
                        "authentication",
                        "not_started",
                    )
                })?,
                Err(_) => {
                    return Err(super::stage::wrap(
                        anyhow!("No saved device password; use --ask-device-password or set GEODOMDESK_DEVICE_PASSWORD"),
                        "authentication",
                        "not_started",
                    ));
                }
            }
        } else {
            receive(&mut stream).await.map_err(|err| {
                let phase = if credentials_sent { "authentication" } else { "connect" };
                super::stage::wrap(err, phase, "not_started")
            })?
        };
        match msg.union {
            Some(message::Union::Hash(hash)) => {
                if !device_password.is_empty() || is_admin {
                    credentials_sent = true;
                }
                if !interface
                    .handle_hash(&device_password, hash, &mut trust, &mut stream)
                    .await
                {
                    return Err(super::stage::wrap(anyhow!("Device authentication could not be started"), "authentication", "not_started"));
                }
                if !interface.prompted_password.load(Ordering::Acquire) {
                    credentials_sent = true;
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
                            return Err(super::stage::wrap(
                                anyhow!("Administrator terminal login requires a Windows target"),
                                "capability",
                                "not_started",
                            ));
                        }
                        stream.set_max_packet_length(1024 * 1024);
                        return Ok(Connection {
                            id: uuid::Uuid::new_v4().to_string(),
                            backend:Backend::Terminal(stream),
                            peer: info,
                            direct,
                            transport,
                            _keep_alive: keep_alive,
                            _kcp: kcp,
                            setup_errors: Vec::new(),
                            connect_elapsed: Duration::ZERO,
                        });
                    }
                    Some(login_response::Union::Error(error)) => match error.as_str() {
                        client::REQUIRE_2FA if !sent_device_code => {
                            credentials_sent = true;
                            let mut message = Message::new();
                            message.set_auth_2fa(Auth2FA {
                                code: auth::device_code().map_err(|error| super::stage::wrap(error,"authentication","not_started"))?,
                                ..Default::default()
                            });
                            stream.send(&message).await?;
                            sent_device_code = true;
                        }
                        client::LOGIN_MSG_PASSWORD_EMPTY | client::LOGIN_MSG_PASSWORD_WRONG => {
                            return Err(super::stage::wrap(anyhow!("{error}. Use --ask-device-password or set GEODOMDESK_DEVICE_PASSWORD"),"authentication","not_started"));
                        }
                        _ if error.starts_with("Failed to access remote ") => {
                            return Err(super::stage::wrap(anyhow!("{error}"), "connect", "not_started"));
                        }
                        _ if error.contains("not allowed")
                            || error.contains("disabled")
                            || error.contains("permission")
                            || error.contains("Unsupported") =>
                        {
                            return Err(super::stage::wrap(anyhow!("{error}"), "capability", "not_started"));
                        }
                        _ => return Err(super::stage::wrap(anyhow!("{error}"),"authentication","not_started")),
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

pub fn run_cli_sync(args: &[String]) -> Option<i32> {
    let first = args.first().map(String::as_str);
    if matches!(first, Some("--help" | "-h")) {
        println!("{USAGE}");
        return Some(0);
    }
    if matches!(first, Some("--version" | "-V")) {
        println!(
            "GeodomDesk {}-r{}",
            crate::VERSION,
            crate::geodomdesk_update::BUILD_NUMBER
        );
        return Some(0);
    }
    if first == Some("tunnel")
        && matches!(args.get(1).map(String::as_str), Some("--help" | "-h"))
    {
        println!("{USAGE}");
        return Some(0);
    }
    None
}

pub async fn run_cli(args: impl Iterator<Item = String>) -> ResultType<i32> {
    let args: Vec<_> = args.collect();
    let json = input::wants_json(&args)
        || matches!(args.first().map(String::as_str), Some("session" | "probe" | "tunnel"));
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
        println!("GeodomDesk {}-r{}", crate::VERSION,crate::geodomdesk_update::BUILD_NUMBER);
        return Ok(0);
    }
    let tunnel=first.as_deref()==Some("tunnel");
    if tunnel {
        let op=args.next().context("Use tunnel open, list or close")?;
        if matches!(op.as_str(),"--help"|"-h"){println!("{USAGE}");return Ok(0);}
        if op!="open" {return super::tunnel::manage(&op,args).await;}
    }
    let session = first.as_deref() == Some("session");
    let probe = first.as_deref() == Some("probe");
    if !session && !probe && !tunnel && first.as_deref() != Some("exec") {
        bail!("{USAGE}");
    }
    let (mut id, mut shell, mut script) = (None, None, None);
    let mut command = None;
    let mut ssh_user = None;
    let mut ssh_host = None;
    let mut ssh_port = None;
    let mut ssh_identity = None;
    let mut ssh_known_hosts = None;
    let mut ssh_strict=false;
    let mut ask_ssh_password=false;
    let mut json = false;
    let mut connect_timeout = None;
    let mut expected_platform = None;
    let mut expected_hostname = None;
    let mut continue_on_error = false;
    let (mut account_user, mut admin_user) = (None, None);
    let mut timeout = None;
    let mut local_port = None;
    let mut remote_port = None;
    let mut remote_host = None;
    let mut command_transport = None;
    let mut relay = false;
    let mut ask_device_password = false;
    let mut non_interactive = false;
    while let Some(arg) = args.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((name, val)) => (name, Some(val.to_owned())),
            None => (arg.as_str(), None),
        };
        let reject_inline = || -> ResultType<()> {
            if inline.is_some() {
                bail!("Option {flag} does not take a value");
            }
            Ok(())
        };
        match flag {
            "--help" | "-h" => {
                println!("{USAGE}");
                return Ok(0);
            }
            "--relay" => {
                reject_inline()?;
                relay = true;
            }
            "--ssh-strict-host-key" => {
                reject_inline()?;
                ssh_strict = true;
            }
            "--ask-ssh-password" => {
                reject_inline()?;
                ask_ssh_password = true;
            }
            "--json" => {
                reject_inline()?;
                json = true;
            }
            "--continue-on-error" => {
                reject_inline()?;
                continue_on_error = true;
            }
            "--ask-device-password" => {
                reject_inline()?;
                ask_device_password = true;
            }
            "--non-interactive" => {
                reject_inline()?;
                non_interactive = true;
            }
            _ => {
                if !input::takes_value(flag) {
                    bail!("Unknown option: {flag}");
                }
                let value = match inline {
                    Some(v) => v,
                    None => args.next().with_context(|| format!("Missing value for {flag}"))?,
                };
                match flag {
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
                        if shell.is_some() {
                            bail!("Specify --shell only once");
                        }
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
                    "--transport" => {
                        if command_transport.is_some() {
                            bail!("Specify --transport only once");
                        }
                        if !matches!(value.as_str(), "auto" | "terminal" | "ssh") {
                            bail!("transport must be auto, terminal or ssh");
                        }
                        command_transport = Some(value);
                    }
                    "--ssh-user" => {
                        if ssh_user.is_some() {
                            bail!("Specify --ssh-user only once");
                        }
                        ssh_user = Some(value);
                    }
                    "--ssh-host" => {
                        if ssh_host.is_some() {
                            bail!("Specify --ssh-host only once");
                        }
                        ssh_host = Some(value);
                    }
                    "--ssh-port" => {
                        if ssh_port.is_some() {
                            bail!("Specify --ssh-port only once");
                        }
                        ssh_port = Some(value.parse().context("Invalid SSH port")?);
                    }
                    "--ssh-identity" => {
                        if ssh_identity.is_some() {
                            bail!("Specify --ssh-identity only once");
                        }
                        ssh_identity = Some(std::path::PathBuf::from(value));
                    }
                    "--ssh-known-hosts" => {
                        if ssh_known_hosts.is_some() {
                            bail!("Specify --ssh-known-hosts only once");
                        }
                        ssh_known_hosts = Some(std::path::PathBuf::from(value));
                    }
                    "--local-port" if tunnel => {
                        if local_port.is_some() {
                            bail!("Specify --local-port only once");
                        }
                        local_port = Some(value.parse().context("Invalid local port")?);
                    }
                    "--remote-port" if tunnel => {
                        if remote_port.is_some() {
                            bail!("Specify --remote-port only once");
                        }
                        remote_port = Some(value.parse::<u16>().context("Invalid remote port")?);
                    }
                    "--remote-host" if tunnel => {
                        if remote_host.is_some() {
                            bail!("Specify --remote-host only once");
                        }
                        remote_host = Some(value);
                    }
                    "--connect-timeout" => {
                        if connect_timeout.is_some() {
                            bail!("Specify --connect-timeout only once");
                        }
                        connect_timeout = Some(value.parse().context("Invalid connection timeout")?);
                    }
                    "--timeout" => {
                        if timeout.is_some() {
                            bail!("Specify --timeout only once");
                        }
                        timeout = Some(value.parse().context("Invalid timeout")?);
                    }
                    "--account-user" => {
                        if account_user.is_some() {
                            bail!("Specify --account-user only once");
                        }
                        account_user = Some(value);
                    }
                    "--admin-user" => {
                        if admin_user.is_some() {
                            bail!("Specify --admin-user only once");
                        }
                        admin_user = Some(value);
                    }
                    _ => bail!("Unknown option: {flag}"),
                }
            }
        }
    }
    if tunnel && timeout.is_some() {
        bail!("tunnel open uses --connect-timeout, not --timeout");
    }
    let timeout = timeout.unwrap_or(15u32);
    let connect_timeout = connect_timeout.unwrap_or(60u32);
    let local_port = local_port.unwrap_or(0u16);
    let remote_host = remote_host
        .map(|h| h.trim().to_owned())
        .unwrap_or_else(|| "127.0.0.1".to_owned());
    let command_transport = command_transport.unwrap_or_else(|| "auto".to_owned());
    let ssh_host_set = ssh_host.is_some();
    let ssh_host = ssh_host
        .map(|h| h.trim().to_owned())
        .unwrap_or_else(|| "127.0.0.1".to_owned());
    let ssh_port_set = ssh_port.is_some();
    let ssh_port = ssh_port.unwrap_or(22u16);
    let raw_id = id.map(|id| id.trim().to_owned())
        .filter(|id| !id.is_empty()).context("Missing --id")?;
    let (connect_id, id, relay_suffix) = normalize_device_id(&raw_id);
    if id.is_empty() {
        bail!("Missing --id");
    }
    relay |= relay_suffix;
    if !(1..=super::MAX_TIMEOUT).contains(&timeout) {
        bail!(
            "Timeout must be between 1 and {} seconds",
            super::MAX_TIMEOUT
        );
    }
    if !(1..=300).contains(&connect_timeout) {
        bail!("Connection timeout must be between 1 and 300 seconds");
    }
    if tunnel && (remote_port.unwrap_or(0)==0 || remote_host.trim().is_empty() || remote_host.len()>253 || remote_host.chars().any(char::is_control)) {bail!("Specify a valid --remote-host and --remote-port (1-65535)");}
    if tunnel && command_transport!="auto" {bail!("tunnel open maps TCP ports; select SSH with exec or session --transport ssh");}
    if tunnel && (shell.is_some() || admin_user.is_some() || continue_on_error) {bail!("tunnel open accepts port mappings, not shell options");}
    if (session || probe || tunnel) && (script.is_some() || command.is_some()) {
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
    let use_ssh=!tunnel && (command_transport=="ssh" || (command_transport=="auto" && shell==Some(ExecShell::BASH)));
    if use_ssh && (ssh_port==0 || admin_user.is_some() || shell.is_some_and(|shell|shell!=ExecShell::BASH) || ssh_host.trim().is_empty() || ssh_host.len()>253 || ssh_host.chars().any(char::is_control)) {bail!("SSH transport requires Bash, a valid SSH host/port and a Linux login");}
    if !use_ssh && (ssh_user.is_some() || ssh_host_set || ssh_identity.is_some() || ssh_known_hosts.is_some() || ssh_strict || ask_ssh_password || ssh_port_set) {bail!("SSH options require --shell bash or --transport ssh");}
    if use_ssh && shell.is_none(){shell=Some(ExecShell::BASH);}
    let ssh=if use_ssh {
        let password=match std::env::var("GEODOMDESK_SSH_PASSWORD") {
            Ok(value)=>Some(value),Err(std::env::VarError::NotPresent) if ask_ssh_password=>{
                if non_interactive {bail!("Set GEODOMDESK_SSH_PASSWORD for non-interactive use");}
                Some(rpassword::prompt_password("Target SSH password: ")?)
            },Err(std::env::VarError::NotPresent)=>None,Err(std::env::VarError::NotUnicode(_))=>bail!("GEODOMDESK_SSH_PASSWORD must be valid Unicode"),
        };
        let mut askpass=std::env::current_exe()?;askpass.set_file_name(if cfg!(windows){"geodomdesk-cli.exe"}else{"geodomdesk-cli"});
        Some(super::ssh::Options{host:ssh_host,username:ssh_user,port:ssh_port,identity:ssh_identity,
            known_hosts:ssh_known_hosts.unwrap_or_else(||hbb_common::config::Config::path("cli-ssh-known_hosts")),strict_host_key:ssh_strict,password,askpass})
    } else {None};
    let is_admin = admin_user.is_some();
    if is_admin && shell == Some(ExecShell::BASH) {
        bail!("--admin-user requires powershell or pwsh");
    }
    let request = if session || probe || tunnel {
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
    sync_local_service_config().await;
    if hbb_common::config::is_incoming_only() {
        bail!("Incoming-only mode");
    }
    let token_override = if account_user.is_some() {
        Some(auth::account_token(account_user, non_interactive).await?)
    } else {
        let _ = auth::account_token(None, non_interactive).await?;
        None
    };
    let device_password = auth::device_password(ask_device_password, non_interactive)?;
    let admin = auth::admin_credentials(admin_user, non_interactive)?;
    let connecting = Instant::now();
    let options = ConnectOptions {
        connect_id,
        id,
        relay,
        token_override,
        device_password,
        admin,
        timeout: connect_timeout,
        expected_platform,
        expected_hostname,
        ssh,
    };
    if tunnel {
        let port = remote_port.context("Missing --remote-port")?;
        return Box::pin(async move {
            super::tunnel::Managed::start(&options, local_port, remote_host, port, "port proxy")
                .await?
                .serve()
                .await
        })
        .await;
    }
    let keep_alive = session || options.ssh.is_some();
    let mut connection =
        wait_or_interrupt(Box::pin(options.open_with_keep_alive(keep_alive))).await?;
    let setup_elapsed = started.elapsed();
    let info = connection_info(&connection, timeout, connecting.elapsed(), setup_elapsed);
    connection.setup_errors.clear();
    if probe {
        output::write(info)?;
        return Ok(0);
    }
    if session {
        return Box::pin(session::run(
            connection,
            options,
            is_admin,
            timeout,
            shell,
            info,
            continue_on_error,
        ))
        .await;
    }
    let execution = Instant::now();
    let request = request.context("Missing command")?;
    let result = match wait_or_interrupt(Box::pin(async {
        session::execute_with_setup_recovery(&mut connection, &options, request, 1)
            .await
            .map_err(|error| connection.command_error(error))
    }))
    .await
    {
        Ok(res) => res,
        Err(error) => {
            drop(connection);
            return Err(error);
        }
    };
    if json {
        let mut value = output::result(None, &result, execution.elapsed());
        value["connection"] = connection_info(
            &connection,
            timeout,
            connection.connect_elapsed,
            setup_elapsed,
        );
        output::write(value)?;
        return Ok(if result.error.is_empty() {
            process_exit_code(result.exit_code)
        } else {
            1
        });
    }
    if !result.error.is_empty() {
        bail!("{}", result.error);
    }
    std::io::stdout().write_all(&result.stdout)?;
    std::io::stderr().write_all(&result.stderr)?;
    std::io::stdout().flush()?;
    std::io::stderr().flush()?;
    Ok(process_exit_code(result.exit_code))
}

async fn wait_or_interrupt<T>(
    fut: std::pin::Pin<Box<dyn std::future::Future<Output = ResultType<T>> + Send + '_>>,
) -> ResultType<T> {
    #[cfg(unix)]
    let mut sigterm =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        res = fut => res,
        sig = tokio::signal::ctrl_c() => {
            sig?;
            bail!("Interrupted");
        }
        _ = async {
            #[cfg(unix)]
            { sigterm.recv().await; }
            #[cfg(not(unix))]
            { std::future::pending::<()>().await; }
        } => {
            bail!("Terminated");
        }
    }
}

fn process_exit_code(code: i32) -> i32 {
    #[cfg(unix)]
    if code != 0 && (code & 0xff) == 0 {
        return 1;
    }
    code
}

async fn sync_local_service_config() {
    if let Ok(mut conn) = crate::ipc::connect(150, "").await {
        if conn.send(&crate::ipc::Data::SyncConfig(None)).await.is_ok() {
            if let Ok(Some(crate::ipc::Data::SyncConfig(Some(configs)))) =
                conn.next_timeout(150).await
            {
                let (mut config, config2) = *configs;
                hbb_common::config::Config::preserve_host_identity(&mut config);
                hbb_common::config::Config::set(config);
                hbb_common::config::Config2::set(config2);
            }
        }
    }
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
            (vec!["exec","--id","1","--transport","invalid"],"transport must be auto, terminal or ssh"),
            (vec!["exec","--id","1","--transport","ssh","--shell","powershell"],"SSH transport requires Bash"),
            (vec!["tunnel","open","--id","1","--remote-port","0"],"Specify a valid --remote-host and --remote-port"),
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
            "",
        );
        login.is_terminal_admin = true;
        login.terminal_one_shot = true;
        let interface = TerminalClient::new(login);
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
