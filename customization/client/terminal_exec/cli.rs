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
  tunnel open --id ID --remote-port PORT [--remote-host HOST] [--local-port PORT]
                          Own a loopback TCP mapping (default: free local port, forced relay)
                          Keep stdin open; JSON controls status/close; EOF or exit cleans up
  tunnel list [--id ID]    List only live CLI-owned tunnels; includes owner and real status
  tunnel close --tunnel-id UUID [--id ID]
                          Close that CLI-owned mapping and all of its connections
  --transport MODE        auto (default): Bash uses SSH over a managed TCP relay
                          terminal: use GeodomDesk terminal; ssh: require SSH Bash
                          Session without --shell keeps the terminal transport
  --ssh-user USER         Target SSH login (default: authenticated peer username)
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
  session controls        {\"op\":\"status|reconnect|cancel|close\",\"id\":\"request-id\"}
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

impl Connection {
    pub(super) fn from_ssh(session:super::ssh::Session,elapsed:Duration)->Self {
        Self{id:uuid::Uuid::new_v4().to_string(),peer:session.tunnel.peer.clone(),
            setup_errors:session.tunnel.setup_errors.clone(),
            backend:Backend::Ssh(session),direct:false,transport:"Relay",_keep_alive:None,_kcp:None,
            connect_elapsed:elapsed}
    }
    pub(super) fn encrypted(&self)->bool {match &self.backend {Backend::Terminal(stream)=>stream.is_secured(),Backend::Ssh(_)=>true}}
    pub(super) fn terminal_stream(&mut self)->ResultType<&mut Stream> {
        match &mut self.backend {Backend::Terminal(stream)=>Ok(stream),Backend::Ssh(_)=>bail!("This connection uses SSH")}
    }
    pub(super) async fn receive_idle(&mut self)->ResultType<Message> {
        match &mut self.backend {
            Backend::Terminal(stream)=>receive(stream).await,
            Backend::Ssh(session)=>{session.idle().await?;bail!("SSH channel closed")},
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
    pub(super) ssh: Option<super::ssh::Options>,
}
impl ConnectOptions {
    fn credential_bound(&self) -> bool {
        self.admin.is_some() || !self.device_password.is_empty() || std::env::var_os("GEODOMDESK_DEVICE_CODE").is_some()
    }
    pub(super) async fn open_forward(&self, host:&str, port:u16)->ResultType<Connection> {
        let started=Instant::now();
        let (mut connection,history)=super::stage::connect_with_recovery(Duration::from_secs(self.timeout.into()), self.credential_bound(), || {
            connect_for(self.id.clone(),self.relay,self.token.clone(),self.device_password.clone(),None,Some((host,port)))
        }).await?;
        connection.connect_elapsed=started.elapsed();connection.setup_errors=history;
        for (expected,actual,label) in [(&self.expected_platform,&connection.peer.platform,"platform"),(&self.expected_hostname,&connection.peer.hostname,"hostname")] {
            if let Some(expected)=expected {if !expected.eq_ignore_ascii_case(actual) {
                return Err(connection.command_error(super::stage::wrap(anyhow!("Target {label} mismatch: expected {expected}, received {actual}"),"target","not_started")));
            }}
        }
        Ok(connection)
    }
    pub(super) async fn open(&self) -> ResultType<Connection> {
        if let Some(ssh)=&self.ssh {
            return tokio::time::timeout(Duration::from_secs(self.timeout.into()),super::ssh::open(self,ssh)).await
                .context("SSH setup timed out before script dispatch").and_then(|v|v)
                .map_err(|error|super::stage::wrap(error,"ssh","not_started"));
        }
        let started = Instant::now();
        let (mut connection, history) =
            super::stage::connect_with_recovery(Duration::from_secs(self.timeout.into()), self.credential_bound(), || {
                connect(
                    self.id.clone(),
                    self.relay,
                    self.token.clone(),
                    self.device_password.clone(),
                    self.admin.clone(),
                )
            })
            .await?;
        connection.connect_elapsed = started.elapsed();
        connection.setup_errors = history;
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
        "session_controls":["status","reconnect","cancel","close"],"connection_policy":["reuse","fresh"]
    });
    if let Backend::Ssh(session)=&connection.backend {
        info["protocol"]=serde_json::json!("ssh");
        info["command_transport"]=serde_json::json!("ssh-relay");
        info["tunnel"]=session.tunnel.snapshot();
        info["terminal"]=serde_json::json!(true);
    } else {info["command_transport"]=serde_json::json!("terminal");}
    if !connection.setup_errors.is_empty() {
        info["setup_errors"] = serde_json::json!(connection.setup_errors);
    }
    info
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

async fn connect(
    id: String,
    relay: bool,
    token: String,
    device_password: String,
    admin: Option<auth::Credentials>,
) -> ResultType<Connection> {
    connect_for(id, relay, token, device_password, admin, None).await
}

async fn connect_for(
    id: String, relay: bool, token: String, device_password: String,
    mut admin: Option<auth::Credentials>, forward: Option<(&str,u16)>,
) -> ResultType<Connection> {
    let conn_type=if forward.is_some(){ConnType::PORT_FORWARD}else{ConnType::TERMINAL};
    let mut login = LoginConfigHandler::default();
    login.initialize(id, conn_type, None, relay, None, None, None);
    if let Some((host,port))=forward {login.port_forward=(host.to_owned(),i32::from(port));}
    login.terminal_one_shot = true;
    login.is_terminal_admin = admin.is_some();
    let is_admin = login.is_terminal_admin;
    let interface = TerminalClient(Arc::new(RwLock::new(login)));
    let id = interface.get_id();
    let key = crate::get_key(false).await;
    let ((mut stream, direct, pk, kcp, transport), (feedback, rendezvous)) =
        Client::start(&id, &key, &token, conn_type, interface.clone()).await?;
    if !stream.is_secured() {
        return Err(super::stage::wrap(anyhow!("Remote command execution requires an encrypted peer connection"), "security", "not_started"));
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
                    return Err(super::stage::wrap(anyhow!("Device authentication could not be started"), "authentication", "not_started"));
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
    let mut local_port=0u16;
    let mut remote_port=None;
    let mut remote_host="127.0.0.1".to_owned();
    let mut command_transport="auto".to_owned();
    let mut ssh_user=None;
    let mut ssh_port=22u16;
    let mut ssh_identity=None;
    let mut ssh_known_hosts=None;
    let mut ssh_strict=false;
    let mut ask_ssh_password=false;
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
            "--ssh-strict-host-key"=>ssh_strict=true,
            "--ask-ssh-password"=>ask_ssh_password=true,
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
                    "--transport"=>{if !matches!(value.as_str(),"auto"|"terminal"|"ssh"){bail!("transport must be auto, terminal or ssh");}command_transport=value;},
                    "--ssh-user"=>{if ssh_user.is_some(){bail!("Specify --ssh-user only once");}ssh_user=Some(value);},
                    "--ssh-port"=>ssh_port=value.parse().context("Invalid SSH port")?,
                    "--ssh-identity"=>ssh_identity=Some(std::path::PathBuf::from(value)),
                    "--ssh-known-hosts"=>ssh_known_hosts=Some(std::path::PathBuf::from(value)),
                    "--local-port" if tunnel=>local_port=value.parse().context("Invalid local port")?,
                    "--remote-port" if tunnel=>{if remote_port.is_some(){bail!("Specify --remote-port only once");}remote_port=Some(value.parse::<u16>().context("Invalid remote port")?);},
                    "--remote-host" if tunnel=>remote_host=value,
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
    let id = id.map(|id|id.trim().to_owned())
        .filter(|id| !id.is_empty()).context("Missing --id")?;
    let id=if id.chars().all(|c|c.is_ascii_digit() || c.is_ascii_whitespace()) {
        id.chars().filter(|c|!c.is_ascii_whitespace()).collect()
    } else {id};
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
    if use_ssh && (ssh_port==0 || admin_user.is_some() || shell.is_some_and(|shell|shell!=ExecShell::BASH)) {bail!("SSH transport requires Bash, a valid SSH port and a Linux login");}
    if !use_ssh && (ssh_user.is_some() || ssh_identity.is_some() || ssh_known_hosts.is_some() || ssh_strict || ask_ssh_password || ssh_port!=22) {bail!("SSH options require --shell bash or --transport ssh");}
    if use_ssh && shell.is_none(){shell=Some(ExecShell::BASH);}
    let ssh=if use_ssh {
        let password=match std::env::var("GEODOMDESK_SSH_PASSWORD") {
            Ok(value)=>Some(value),Err(std::env::VarError::NotPresent) if ask_ssh_password=>{
                if non_interactive {bail!("Set GEODOMDESK_SSH_PASSWORD for non-interactive use");}
                Some(rpassword::prompt_password("Target SSH password: ")?)
            },Err(std::env::VarError::NotPresent)=>None,Err(std::env::VarError::NotUnicode(_))=>bail!("GEODOMDESK_SSH_PASSWORD must be valid Unicode"),
        };
        let mut askpass=std::env::current_exe()?;askpass.set_file_name(if cfg!(windows){"geodomdesk-cli.exe"}else{"geodomdesk-cli"});
        Some(super::ssh::Options{username:ssh_user,port:ssh_port,identity:ssh_identity,
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
        ssh,
    };
    if tunnel {
        return super::tunnel::Managed::start(&options,local_port,remote_host,remote_port.context("Missing --remote-port")?,"port proxy").await?.serve().await;
    }
    let mut connection = options.open().await?;
    let setup_elapsed = started.elapsed();
    let info = connection_info(&connection, timeout, connecting.elapsed(), setup_elapsed);
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
    let result = session::execute_with_setup_recovery(&mut connection, &options, request, 1)
        .await
        .map_err(|error| connection.command_error(error))?;
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
