//! Process-owned loopback mappings. The control socket, rather than a PID, proves liveness.
use super::{cli, output};
use crate::{
    client::{self, Data, Interface, LoginConfigHandler},
    port_forward_mux::{self, Claim, Tunnel as MuxTunnel, TunnelHandle},
};
use base::message_proto::{Hash, PeerInfo, TestDelay, WindowsSession};
use hbb_common::{
    anyhow::Context,
    bail,
    config::Config,
    futures::{stream, StreamExt},
    tokio, ResultType, Stream,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex, RwLock as StdRwLock,
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{mpsc, Mutex as AsyncMutex, Notify, watch},
    task::JoinSet,
};

const MAX_CHANNELS: usize = 16;
const CONTROL_LIMIT: usize = 2048;

#[derive(Default, Clone, Serialize, Deserialize)]
struct Record {
    tunnel_id: String,
    device_id: String,
    local_port: u16,
    remote_host: String,
    remote_port: u16,
    owner_pid: u32,
    purpose: String,
    control_port: u16,
    secret: String,
    #[serde(default)]
    direct: bool,
    #[serde(default)]
    transport: String,
    #[serde(default)]
    relay: bool,
    #[serde(default)]
    multiplexed: bool,
}

#[derive(Default)]
struct State {
    active: AtomicUsize,
    closed: AtomicBool,
    closed_notify: Notify,
    last_error: Mutex<String>,
    mux_handle: Mutex<Option<Arc<TunnelHandle>>>,
    direct: AtomicBool,
    transport: Mutex<Option<&'static str>>,
    multiplexed: AtomicBool,
}

impl State {
    fn active_connections(&self) -> usize {
        let mux = self
            .mux_handle
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .as_ref()
            .map_or(0, |handle| handle.live_channels());
        mux + self.active.load(Ordering::Relaxed)
    }

    fn mark_closed(&self) {
        self.closed.store(true, Ordering::Relaxed);
        self.closed_notify.notify_waiters();
    }

    fn set_error(&self, message: String) {
        *self
            .last_error
            .lock()
            .unwrap_or_else(|poison| poison.into_inner()) = message.chars().take(512).collect();
    }

    fn set_transport(&self, direct: bool, transport: &'static str, multiplexed: bool) {
        self.direct.store(direct, Ordering::Relaxed);
        *self
            .transport
            .lock()
            .unwrap_or_else(|poison| poison.into_inner()) = Some(transport);
        self.multiplexed.store(multiplexed, Ordering::Relaxed);
    }

    fn store_mux(&self, handle: Option<Arc<TunnelHandle>>) {
        *self
            .mux_handle
            .lock()
            .unwrap_or_else(|poison| poison.into_inner()) = handle;
    }
}

#[derive(Clone)]
struct TunnelInterface {
    state: Arc<State>,
    lch: Arc<StdRwLock<LoginConfigHandler>>,
}

impl TunnelInterface {
    fn new(state: Arc<State>) -> Self {
        let mut lch = LoginConfigHandler::default();
        lch.terminal_one_shot = true;
        Self {
            state,
            lch: Arc::new(StdRwLock::new(lch)),
        }
    }
}

#[async_trait::async_trait]
impl Interface for TunnelInterface {
    fn send(&self, _: Data) {}
    fn msgbox(&self, kind: &str, _: &str, text: &str, _: &str) {
        if kind == "error" && !text.is_empty() {
            self.state.set_error(text.to_owned());
        }
    }
    fn on_error(&self, err: &str) {
        if !err.is_empty() {
            self.state.set_error(err.to_owned());
        }
    }
    fn handle_login_error(&self, _: &str) -> bool {
        false
    }
    fn handle_peer_info(&self, _: PeerInfo) {}
    fn set_multiple_windows_session(&self, _: Vec<WindowsSession>) {}
    fn get_lch(&self) -> Arc<StdRwLock<LoginConfigHandler>> {
        self.lch.clone()
    }
    async fn handle_hash(
        &self,
        _: &str,
        _: Hash,
        _: &mut crate::trusted_devices::client::Session,
        _: &mut Stream,
    ) -> bool {
        false
    }
    async fn handle_login_from_ui(
        &self,
        _: String,
        _: String,
        _: String,
        _: bool,
        _: &mut crate::trusted_devices::client::Session,
        _: &mut Stream,
    ) {
    }
    async fn handle_test_delay(&self, delay: TestDelay, stream: &mut Stream) {
        client::handle_test_delay(delay, stream).await;
    }
}

struct Registration(PathBuf);
impl Drop for Registration {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_file(&self.0) {
            if error.kind() != std::io::ErrorKind::NotFound {
                hbb_common::log::warn!("Cannot remove CLI tunnel record: {error}");
            }
        }
    }
}

fn directory() -> ResultType<PathBuf> {
    let path = Config::path("cli-tunnels");
    if !path.is_absolute() {
        bail!("No private client configuration directory");
    }
    if !path.exists() {
        fs::create_dir_all(&path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        }
    }
    Ok(path)
}

fn record_path(id: &str) -> ResultType<PathBuf> {
    let id = uuid::Uuid::parse_str(id).context("Invalid tunnel ID")?;
    Ok(directory()?.join(format!("{id}.json")))
}

impl Record {
    fn public(&self, state: &State) -> Value {
        let active = state.active_connections();
        let live_transport = *state
            .transport
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let direct = match live_transport {
            Some(_) => state.direct.load(Ordering::Relaxed),
            None => self.direct,
        };
        let transport = live_transport.unwrap_or({
            if self.transport.is_empty() {
                "unknown"
            } else {
                self.transport.as_str()
            }
        });
        let multiplexed = match live_transport {
            Some(_) => state.multiplexed.load(Ordering::Relaxed),
            None => self.multiplexed,
        };
        let max_connections = if multiplexed {
            port_forward_mux::MAX_CHANNELS
        } else {
            MAX_CHANNELS
        };
        json!({"tunnel_id":self.tunnel_id,"device_id":self.device_id,
            "local_host":"127.0.0.1","local_port":self.local_port,"private_stream":self.local_port == 0,
            "remote_host":self.remote_host,"remote_port":self.remote_port,
            "owner":"cli","owner_pid":self.owner_pid,"purpose":self.purpose,
            "state":if state.closed.load(Ordering::Relaxed){"closed"}else if active>0{"active"}else{"listening"},
            "active_connections":active,"max_connections":max_connections,
            "last_error":*state.last_error.lock().unwrap_or_else(|poison| poison.into_inner()),
            "direct":direct,"transport":transport,"relay":self.relay || !direct,"multiplexed":multiplexed,
            "payload_encryption":if multiplexed {
                "end-to-end encrypted GeodomDesk multiplexed tunnel (plus inner protocol TLS/SSH)"
            } else {
                "end-to-end encrypted GeodomDesk stream (plus inner protocol TLS/SSH)"
            }})
    }
}

fn is_transient_accept_error(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::ConnectionAborted
            | std::io::ErrorKind::ConnectionReset
            | std::io::ErrorKind::WouldBlock
    ) || matches!(error.raw_os_error(), Some(24 | 23 | 10024))
}

fn sort_listed_tunnels(values: &mut [Value]) {
    values.sort_by(|a, b| {
        let a_port = a["local_port"].as_u64().unwrap_or(0);
        let b_port = b["local_port"].as_u64().unwrap_or(0);
        a_port.cmp(&b_port).then_with(|| {
            a["tunnel_id"]
                .as_str()
                .unwrap_or("")
                .cmp(b["tunnel_id"].as_str().unwrap_or(""))
        })
    });
}

async fn read_line(
    reader: &mut (impl tokio::io::AsyncBufRead + Unpin),
    limit: usize,
) -> ResultType<Vec<u8>> {
    let mut bytes = Vec::new();
    reader
        .take(limit as u64 + 1)
        .read_until(b'\n', &mut bytes)
        .await?;
    if bytes.len() > limit {
        bail!("Tunnel control request too large");
    }
    if bytes.is_empty() {
        bail!("Tunnel control disconnected");
    }
    Ok(bytes)
}

const CONTROL_PROTOCOL: &str = "geodomdesk-cli-tunnel-v1";

#[derive(Debug)]
struct NotTunnelOwner(&'static str);
impl std::fmt::Display for NotTunnelOwner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for NotTunnelOwner {}

fn stale_owner_error(error: &hbb_common::anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause.is::<NotTunnelOwner>()
            || cause
                .downcast_ref::<std::io::Error>()
                .is_some_and(|io| io.kind() == std::io::ErrorKind::ConnectionRefused)
    })
}

fn owner_key(record: &Record) -> ResultType<hbb_common::sodiumoxide::crypto::auth::Key> {
    let digest = hbb_common::sodiumoxide::crypto::hash::sha256::hash(record.secret.as_bytes());
    hbb_common::sodiumoxide::crypto::auth::Key::from_slice(&digest.0)
        .context("Invalid tunnel owner key")
}
fn owner_message(record: &Record, nonce: &str) -> String {
    format!(
        "GeodomDesk CLI tunnel owner v1\0{}\0{nonce}",
        record.tunnel_id
    )
}
fn owner_proof(record: &Record, nonce: &str) -> ResultType<String> {
    let proof = hbb_common::sodiumoxide::crypto::auth::authenticate(
        owner_message(record, nonce).as_bytes(),
        &owner_key(record)?,
    );
    Ok(hex::encode(proof.0))
}
fn verify_owner(record: &Record, nonce: &str, proof: &str) -> ResultType<()> {
    let bytes = hex::decode(proof)
        .map_err(|_| hbb_common::anyhow::Error::new(NotTunnelOwner("Invalid tunnel owner proof")))?;
    let tag = hbb_common::sodiumoxide::crypto::auth::Tag::from_slice(&bytes)
        .ok_or_else(|| hbb_common::anyhow::Error::new(NotTunnelOwner("Invalid tunnel owner proof")))?;
    if !hbb_common::sodiumoxide::crypto::auth::verify(
        &tag,
        owner_message(record, nonce).as_bytes(),
        &owner_key(record)?,
    ) {
        return Err(hbb_common::anyhow::Error::new(NotTunnelOwner(
            "Control endpoint does not prove ownership of this tunnel",
        )));
    }
    Ok(())
}

async fn rpc(record: &Record, op: &str) -> ResultType<Value> {
    // Windows can take about two seconds to report a refused loopback connection.
    // Keep this separate from the short response budget of a live owner.
    let socket = tokio::time::timeout(Duration::from_secs(3),
        TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, record.control_port)))
        .await.context("Tunnel control connection timed out")??;
    tokio::time::timeout(Duration::from_millis(400), async {
        let mut socket = BufReader::new(socket);
        socket.get_mut().set_nodelay(true)?;
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        socket
            .get_mut()
            .write_all(&serde_json::to_vec(
                &json!({"protocol":CONTROL_PROTOCOL,"tunnel_id":record.tunnel_id,"nonce":nonce}),
            )?)
            .await?;
        socket.get_mut().write_all(b"\n").await?;
        let hello: Value = serde_json::from_slice(&read_line(&mut socket, CONTROL_LIMIT).await?)
            .map_err(|_| hbb_common::anyhow::Error::new(NotTunnelOwner("Tunnel owner identity changed")))?;
        if hello["protocol"] != CONTROL_PROTOCOL
            || hello["tunnel_id"] != record.tunnel_id
            || hello["nonce"] != nonce
        {
            return Err(hbb_common::anyhow::Error::new(NotTunnelOwner("Tunnel owner identity changed")));
        }
        verify_owner(
            record,
            &nonce,
            hello["proof"]
                .as_str()
                .ok_or_else(|| hbb_common::anyhow::Error::new(NotTunnelOwner("Missing tunnel owner proof")))?,
        )?;
        let bytes = serde_json::to_vec(
            &json!({"tunnel_id":record.tunnel_id,"secret":record.secret,"op":op}),
        )?;
        socket.get_mut().write_all(&bytes).await?;
        socket.get_mut().write_all(b"\n").await?;
        let response = read_line(&mut socket, 8192).await?;
        let value: Value = serde_json::from_slice(&response)
            .map_err(|_| hbb_common::anyhow::Error::new(NotTunnelOwner("Tunnel owner identity changed")))?;
        if value["tunnel_id"] != record.tunnel_id {
            return Err(hbb_common::anyhow::Error::new(NotTunnelOwner("Tunnel owner identity changed")));
        }
        Ok(value)
    })
    .await
    .context("Tunnel owner did not respond")?
}

fn records() -> ResultType<Vec<(PathBuf, Record)>> {
    let mut result = Vec::new();
    for entry in fs::read_dir(directory()?)? {
        let entry = entry?;
        let kind = match entry.file_type() {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.into()),
        };
        if !kind.is_file() || entry.path().extension().and_then(|v| v.to_str()) != Some("json") {
            continue;
        }
        let meta = match entry.metadata() {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.into()),
        };
        if meta.len() > 8192 {
            continue;
        }
        let bytes = match fs::read(entry.path()) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.into()),
        };
        if bytes.len() > 8192 {
            continue;
        }
        if let Ok(record) = serde_json::from_slice::<Record>(&bytes) {
            result.push((entry.path(), record));
        }
    }
    Ok(result)
}

pub(crate) async fn list(device: &str) -> ResultType<Value> {
    let device = cli::normalize_device_id(device).1;
    let records = records()?;
    let requests = records
        .into_iter()
        .filter(|(_, r)| device.is_empty() || r.device_id == device)
        .map(|(path, r)| async move {
            match rpc(&r, "status").await {
                Ok(value) => Some(value),
                Err(error) => {
                    if fs::symlink_metadata(&path).is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound) {
                        return None;
                    }
                    // A dead process cannot own an OS listener. Never kill a PID or another mapping.
                    if stale_owner_error(&error) {
                        if let Err(error) = fs::remove_file(path) {
                            if error.kind() != std::io::ErrorKind::NotFound {
                                hbb_common::log::warn!("Cannot prune CLI tunnel record: {error}");
                            }
                        }
                        return None;
                    }
                    let mut value = r.public(&State::default());
                    value["state"] = json!("unresponsive");
                    value["active_connections"] = Value::Null;
                    value["last_error"] = json!(format!("{error:#}"));
                    Some(value)
                }
            }
        });
    let mut values = stream::iter(requests)
        .buffer_unordered(16)
        .filter_map(hbb_common::futures::future::ready)
        .collect::<Vec<_>>()
        .await;
    sort_listed_tunnels(&mut values);
    Ok(json!(values))
}

pub(crate) async fn close(device: &str, id: &str) -> ResultType<Value> {
    let device = cli::normalize_device_id(device).1;
    let path = record_path(id)?;
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({"tunnel_id":id,"state":"closed","already_closed":true}))
        }
        Err(error) => return Err(error.into()),
    };
    let record: Record = serde_json::from_slice(&bytes)?;
    if !device.is_empty() && record.device_id != device {
        bail!("Tunnel belongs to a different device");
    }
    match rpc(&record, "close").await {
        Err(error) if stale_owner_error(&error) => {
            if let Err(error) = fs::remove_file(&path) {
                if error.kind() != std::io::ErrorKind::NotFound { return Err(error.into()); }
            }
            Ok(json!({"tunnel_id":record.tunnel_id,"state":"closed","already_closed":true}))
        }
        Err(_) if fs::symlink_metadata(&path).is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound) => {
            Ok(json!({"tunnel_id":record.tunnel_id,"state":"closed","already_closed":true}))
        }
        result => result,
    }
}

async fn send_control_response(
    socket: &mut (impl tokio::io::AsyncWrite + Unpin),
    record: &Record, state: &State, close: bool,
    close_tx: mpsc::Sender<()>,
    mut released: watch::Receiver<bool>,
) -> ResultType<()> {
    if close {
        // Request shutdown independently of delivery and acknowledge after the data plane stops.
        let _ = close_tx.try_send(());
        released.wait_for(|released| *released).await?;
    }
    socket.write_all(&serde_json::to_vec(&record.public(state))?).await?;
    socket.write_all(b"\n").await?;
    Ok(())
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub(super) struct Managed {
    pub(super) info: Value,
    pub(super) peer: PeerInfo,
    pub(super) setup_errors: Vec<super::stage::SetupError>,
    record: Record,
    state: Arc<State>,
    task: tokio::task::JoinHandle<()>,
    close_tx: mpsc::Sender<()>,
    path: PathBuf,
}
impl Drop for Managed {
    fn drop(&mut self) {
        self.state.mark_closed();
        self.task.abort();
        self.state.store_mux(None);
        // Task abortion drops its JoinSet and every forwarded socket.
        if let Err(error) = fs::remove_file(&self.path) {
            if error.kind() != std::io::ErrorKind::NotFound {
                hbb_common::log::warn!("Cannot remove CLI tunnel record: {error}");
            }
        }
    }
}
impl Managed {
    pub(super) fn snapshot(&self) -> Value {
        let mut info = self.record.public(&self.state);
        if self.is_closed() {
            info["state"] = json!("closed");
        }
        info
    }
    pub(super) fn port(&self) -> u16 {
        self.record.local_port
    }
    pub(super) fn direct(&self) -> bool {
        self.state.direct.load(Ordering::Relaxed)
    }
    pub(super) fn transport(&self) -> &'static str {
        self.state
            .transport
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .unwrap_or("Relay")
    }
    pub(super) fn is_closed(&self) -> bool {
        self.state.closed.load(Ordering::Relaxed) || self.task.is_finished()
    }
    pub(super) async fn start(
        options: &cli::ConnectOptions,
        port: u16,
        host: String,
        remote_port: u16,
        purpose: &str,
    ) -> ResultType<Self> {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port))
            .await
            .context(
                "Local tunnel port is unavailable; use --local-port 0 to allocate a free port",
            )?;
        Self::start_inner(options, Some(listener), None, host, remote_port, purpose).await
    }

    #[cfg(feature = "embedded-ssh")]
    pub(super) async fn start_stream(
        options: &cli::ConnectOptions,
        host: String,
        remote_port: u16,
    ) -> ResultType<(Self, tokio::io::DuplexStream)> {
        let (client, relay) = tokio::io::duplex(128 * 1024);
        let owner = Self::start_inner(options, None, Some(relay), host, remote_port, "Embedded SSH").await?;
        Ok((owner, client))
    }

    async fn start_inner(
        options: &cli::ConnectOptions,
        listener: Option<TcpListener>,
        private_stream: Option<tokio::io::DuplexStream>,
        host: String,
        remote_port: u16,
        purpose: &str,
    ) -> ResultType<Self> {
        if remote_port == 0
            || host.trim().is_empty()
            || host.len() > 253
            || host.chars().any(char::is_control)
        {
            bail!("Specify a valid remote host and port (1-65535)");
        }
        let control = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let mut options = options.clone();
        options.ssh = None;
        options.admin = None;
        let allow_mux = private_stream.is_none();
        let first = options.open_forward(&host, remote_port, allow_mux).await?;
        let peer = first.peer.clone();
        let setup_errors = first.setup_errors.clone();
        let initial_direct = first.direct();
        let initial_transport = first.transport();
        let initial_mux = allow_mux && peer.features.port_forward_mux;
        let state = Arc::new(State::default());
        state.set_transport(initial_direct, initial_transport, initial_mux);
        let record = Record {
            tunnel_id: uuid::Uuid::new_v4().to_string(),
            device_id: options.id.clone(),
            local_port: listener.as_ref().map(TcpListener::local_addr).transpose()?.map_or(0, |address| address.port()),
            remote_host: host,
            remote_port,
            owner_pid: std::process::id(),
            purpose: purpose.to_owned(),
            control_port: control.local_addr()?.port(),
            secret: uuid::Uuid::new_v4().to_string(),
            direct: initial_direct,
            transport: initial_transport.to_owned(),
            relay: options.relay,
            multiplexed: initial_mux,
        };
        let path = record_path(&record.tunnel_id)?;
        let mut file = fs::OpenOptions::new();
        file.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            file.mode(0o600);
        }
        let mut file = file.open(&path)?;
        let registration = Registration(path.clone());
        file.write_all(&serde_json::to_vec(&record)?)?;
        drop(file);
        let mut info = record.public(&state);
        if !setup_errors.is_empty() {
            info["setup_errors"] = json!(setup_errors);
        }
        let mux_tunnel = Arc::new(MuxTunnel::new());
        let connect_epoch = Arc::new(AtomicU64::new(0));
        let mux_connect_lock: Arc<AsyncMutex<Option<(u64, String)>>> =
            Arc::new(AsyncMutex::new(None));
        let initial_legacy = if initial_mux {
            let (stream, keep_alive, kcp, _, _, _) = first.into_forward_parts()?;
            if matches!(mux_tunnel.claim(), Claim::Claimed) {
                let handle = mux_tunnel.set_muxed_with_guard(
                    stream,
                    TunnelInterface::new(state.clone()),
                    (keep_alive, kcp),
                );
                state.store_mux(Some(handle));
            }
            None
        } else {
            mux_tunnel.set_legacy();
            Some(first)
        };
        let status = state.clone();
        let (close_tx, mut close_rx) = mpsc::channel(1);
        let control_close = close_tx.clone();
        let (released, wait_released) = watch::channel(false);
        let managed_record = record.clone();
        let task = tokio::spawn(async move {
            struct MarkClosed(Arc<State>);
            impl Drop for MarkClosed {
                fn drop(&mut self) {
                    self.0.mark_closed();
                    self.0.store_mux(None);
                }
            }
            let _close_guard = MarkClosed(status.clone());
            let _registration = registration;
            let _mux_tunnel = mux_tunnel.clone();
            let mut first = initial_legacy.map(|conn| (conn, Instant::now()));
            let mut channels = JoinSet::new();
            let mut controls = JoinSet::new();
            let private = private_stream.is_some();
            let private_connection = if private { first.take() } else { None };
            if let (Some(stream), Some((connection, _))) = (private_stream, private_connection) {
                status.active.store(1, Ordering::Relaxed);
                let status = status.clone();
                channels.spawn(async move {
                    if let Err(error) = forward(stream, connection).await {
                        *status.last_error.lock().unwrap_or_else(|poison| poison.into_inner()) =
                            format!("{error:#}").chars().take(512).collect();
                    }
                    status.active.store(0, Ordering::Relaxed);
                });
            }
            loop {
                tokio::select! {
                    _=async {
                        let deadline=first.as_ref().map(|(_,at)|*at+Duration::from_secs(5));
                        if let Some(deadline)=deadline {tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;}else{std::future::pending::<()>().await;}
                    }=>{first=None;},
                    accepted=async {
                        match &listener {
                            Some(listener) => listener.accept().await,
                            None => std::future::pending().await,
                        }
                    }=>{
                        let (socket,_)=match accepted {
                            Ok(value)=>value,
                            Err(error) if is_transient_accept_error(&error)=>{
                                status.set_error(error.to_string());
                                tokio::time::sleep(Duration::from_millis(50)).await;
                                continue;
                            }
                            Err(error)=>{
                                status.set_error(error.to_string());
                                break;
                            }
                        };
                        let _ = socket.set_nodelay(true);
                        if status.closed.load(Ordering::Relaxed) {
                            drop(socket);
                            continue;
                        }
                        let claim = match mux_tunnel.claim() {
                            Claim::Muxed(handle) if handle.is_closed() => {
                                mux_tunnel.set_failed();
                                status.store_mux(None);
                                Claim::Claimed
                            }
                            other => other,
                        };
                        match claim {
                            Claim::Muxed(handle) => {
                                status.store_mux(Some(handle.clone()));
                                if let Err(error) = handle.open(
                                    &record.remote_host,
                                    i32::from(record.remote_port),
                                    socket,
                                    Vec::new(),
                                ) {
                                    status.set_error(format!("{error:#}"));
                                }
                                continue;
                            }
                            Claim::Claimed => {
                                if channels.len() >= port_forward_mux::MAX_CHANNELS {
                                    status.set_error(format!(
                                        "Tunnel reached maximum pending/active channels ({})",
                                        port_forward_mux::MAX_CHANNELS
                                    ));
                                    drop(socket);
                                    continue;
                                }
                                let entry_epoch = connect_epoch.load(Ordering::Acquire);
                                let saved = first.take().filter(|(_, at)| at.elapsed() < Duration::from_secs(5)).map(|(c, _)| c);
                                let options = options.clone();
                                let record = record.clone();
                                let status = status.clone();
                                let mux_tunnel = mux_tunnel.clone();
                                let connect_epoch = connect_epoch.clone();
                                let mux_connect_lock = mux_connect_lock.clone();
                                channels.spawn(async move {
                                    let mut last_attempt = mux_connect_lock.lock().await;
                                    let inner_claim = match mux_tunnel.claim() {
                                        Claim::Muxed(handle) if handle.is_closed() => {
                                            mux_tunnel.set_failed();
                                            status.store_mux(None);
                                            Claim::Claimed
                                        }
                                        other => other,
                                    };
                                    match inner_claim {
                                        Claim::Muxed(handle) => {
                                            drop(last_attempt);
                                            status.store_mux(Some(handle.clone()));
                                            if let Err(error) = handle.open(
                                                &record.remote_host,
                                                i32::from(record.remote_port),
                                                socket,
                                                Vec::new(),
                                            ) {
                                                status.set_error(format!("{error:#}"));
                                            }
                                            return;
                                        }
                                        Claim::Legacy => {
                                            drop(last_attempt);
                                            if status.active.load(Ordering::Relaxed) >= MAX_CHANNELS {
                                                status.set_error(format!(
                                                    "Tunnel reached maximum concurrent connections ({MAX_CHANNELS})"
                                                ));
                                                return;
                                            }
                                            struct Count(Arc<State>);
                                            impl Drop for Count {fn drop(&mut self){self.0.active.fetch_sub(1,Ordering::Relaxed);}}
                                            status.active.fetch_add(1, Ordering::Relaxed);
                                            let _count = Count(status.clone());
                                            let result = async {
                                                let connection = match saved {
                                                    Some(c) => c,
                                                    None => options.open_forward(&record.remote_host, record.remote_port, false).await?,
                                                };
                                                forward(socket, connection).await
                                            }.await;
                                            if let Err(error) = result {
                                                status.set_error(format!("{error:#}"));
                                            }
                                            return;
                                        }
                                        Claim::Claimed => {}
                                    }
                                    if let Some((err_epoch, err_msg)) = last_attempt.as_ref() {
                                        if *err_epoch > entry_epoch && saved.is_none() {
                                            status.set_error(err_msg.clone());
                                            return;
                                        }
                                    }
                                    let next = match saved {
                                        Some(c) => Ok(c),
                                        None => options.open_forward(&record.remote_host, record.remote_port, true).await,
                                    };
                                    let connection = match next {
                                        Ok(c) => c,
                                        Err(error) => {
                                            let msg = format!("{error:#}");
                                            let gen_id = connect_epoch.fetch_add(1, Ordering::AcqRel) + 1;
                                            *last_attempt = Some((gen_id, msg.clone()));
                                            mux_tunnel.set_failed();
                                            status.store_mux(None);
                                            status.set_error(msg);
                                            return;
                                        }
                                    };
                                    if connection.peer.features.port_forward_mux {
                                        let direct = connection.direct();
                                        let transport = connection.transport();
                                        match connection.into_forward_parts() {
                                            Ok((stream, keep_alive, kcp, _, _, _)) => {
                                                connect_epoch.fetch_add(1, Ordering::AcqRel);
                                                *last_attempt = None;
                                                status.set_transport(direct, transport, true);
                                                let handle = mux_tunnel.set_muxed_with_guard(
                                                    stream,
                                                    TunnelInterface::new(status.clone()),
                                                    (keep_alive, kcp),
                                                );
                                                status.store_mux(Some(handle.clone()));
                                                drop(last_attempt);
                                                if let Err(error) = handle.open(
                                                    &record.remote_host,
                                                    i32::from(record.remote_port),
                                                    socket,
                                                    Vec::new(),
                                                ) {
                                                    status.set_error(format!("{error:#}"));
                                                }
                                            }
                                            Err(error) => {
                                                let msg = format!("{error:#}");
                                                let gen_id = connect_epoch.fetch_add(1, Ordering::AcqRel) + 1;
                                                *last_attempt = Some((gen_id, msg.clone()));
                                                mux_tunnel.set_failed();
                                                status.store_mux(None);
                                                status.set_error(msg);
                                            }
                                        }
                                    } else {
                                        connect_epoch.fetch_add(1, Ordering::AcqRel);
                                        *last_attempt = None;
                                        mux_tunnel.set_legacy();
                                        status.store_mux(None);
                                        status.set_transport(connection.direct(), connection.transport(), false);
                                        drop(last_attempt);
                                        struct Count(Arc<State>);
                                        impl Drop for Count {fn drop(&mut self){self.0.active.fetch_sub(1,Ordering::Relaxed);}}
                                        status.active.fetch_add(1, Ordering::Relaxed);
                                        let _count = Count(status.clone());
                                        if let Err(error) = forward(socket, connection).await {
                                            status.set_error(format!("{error:#}"));
                                        }
                                    }
                                });
                                continue;
                            }
                            Claim::Legacy => {
                                status.store_mux(None);
                            }
                        }
                        if status.active.load(Ordering::Relaxed) >= MAX_CHANNELS {
                            status.set_error(
                                format!("Tunnel reached maximum concurrent connections ({MAX_CHANNELS})"),
                            );
                            drop(socket);
                            continue;
                        }
                        let saved=first.take().filter(|(_,at)|at.elapsed()<Duration::from_secs(5)).map(|(c,_)|c);
                        let options=options.clone();let record=record.clone();let status=status.clone();
                        status.active.fetch_add(1,Ordering::Relaxed);
                        channels.spawn(async move {
                            struct Count(Arc<State>);
                            impl Drop for Count {fn drop(&mut self){self.0.active.fetch_sub(1,Ordering::Relaxed);}}
                            let _count=Count(status.clone());
                            let result=async {
                                #[cfg(feature = "embedded-ssh")]
                                super::auth::verify_ssh_account(&options.current_token()?).await?;
                                    let connection=match saved {Some(c)=>c,None=>options.open_forward(&record.remote_host,record.remote_port,false).await?};
                                status.set_transport(connection.direct(), connection.transport(), false);
                                forward(socket,connection).await
                            }.await;
                            if let Err(error)=result { status.set_error(format!("{error:#}")); }
                        });
                    },
                    accepted=control.accept()=>{
                        let (socket,_)=match accepted {
                            Ok(value)=>value,
                            Err(error) if is_transient_accept_error(&error)=>{
                                status.set_error(error.to_string());
                                tokio::time::sleep(Duration::from_millis(50)).await;
                                continue;
                            }
                            Err(error)=>{
                                status.set_error(error.to_string());
                                break;
                            }
                        };
                        if controls.len()>=8 {drop(socket);continue;}
                        let record=record.clone();let status=status.clone();
                        let close_tx=control_close.clone();let released=wait_released.clone();
                        controls.spawn(async move {
                            let _=tokio::time::timeout(Duration::from_secs(1),async {
                                socket.set_nodelay(true)?;
                                let mut socket=BufReader::new(socket);
                                let hello:Value=serde_json::from_slice(&read_line(&mut socket,CONTROL_LIMIT).await?)?;
                                let nonce=hello["nonce"].as_str().context("Missing tunnel owner challenge")?;
                                if hello["protocol"]!=CONTROL_PROTOCOL || hello["tunnel_id"]!=record.tunnel_id || nonce.len()!=32 || !nonce.bytes().all(|c|c.is_ascii_hexdigit()) {bail!("Invalid tunnel owner challenge");}
                                socket.get_mut().write_all(&serde_json::to_vec(&json!({"protocol":CONTROL_PROTOCOL,"tunnel_id":record.tunnel_id,"nonce":nonce,"proof":owner_proof(&record,nonce)?}))?).await?;
                                socket.get_mut().write_all(b"\n").await?;
                                let value:Value=serde_json::from_slice(&read_line(&mut socket,CONTROL_LIMIT).await?)?;
                                let provided = value["secret"].as_str().context("Missing tunnel owner token")?;
                                if !constant_time_eq(provided.as_bytes(), record.secret.as_bytes()) || value["tunnel_id"]!=record.tunnel_id {bail!("Invalid tunnel owner token");}
                                let close=value["op"]=="close";
                                if !close && value["op"]!="status" {bail!("Unknown tunnel operation");}
                                send_control_response(socket.get_mut(), &record, &status, close, close_tx, released).await
                            }).await;
                        });
                    },
                    _=close_rx.recv()=>break,
                    _=channels.join_next(),if !channels.is_empty()=>{if private {break;}},
                    _=controls.join_next(),if !controls.is_empty()=>{},
                }
            }
            drop(listener);
            drop(first);
            channels.shutdown().await;
            status.active.store(0, Ordering::Relaxed);
            drop(registration);
            status.mark_closed();
            status.store_mux(None);
            released.send_replace(true);
            drop(control);
            while controls.join_next().await.is_some() {}
        });
        Ok(Self {
            info,
            peer,
            setup_errors,
            record: managed_record,
            state,
            task,
            close_tx,
            path,
        })
    }

    pub(super) async fn serve(mut self) -> ResultType<i32> {
        let mut ready = self.snapshot();
        ready["event"] = json!("tunnel_ready");
        ready["ok"] = json!(true);
        output::write(ready)?;
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        std::thread::spawn(move || {
            use std::io::{BufRead, Read};
            let input = std::io::stdin();
            let mut input = input.lock();
            loop {
                let mut line = Vec::new();
                let count = match input
                    .by_ref()
                    .take(CONTROL_LIMIT as u64 + 1)
                    .read_until(b'\n', &mut line)
                {
                    Ok(0) => break,
                    Ok(n) => n,
                    Err(error) => {
                        let _ = tx.blocking_send(Err(error));
                        break;
                    }
                };
                if count > CONTROL_LIMIT {
                    let _ = tx.blocking_send(Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "Tunnel control line exceeds 2048 bytes",
                    )));
                    break;
                }
                let line = String::from_utf8(line)
                    .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error));
                if tx.blocking_send(line).is_err() {
                    break;
                }
            }
        });
        let state = self.state.clone();
        let closed_wait = async move {
            loop {
                let notified = state.closed_notify.notified();
                if state.closed.load(Ordering::Relaxed) {
                    break;
                }
                notified.await;
            }
        };
        tokio::pin!(closed_wait);
        #[cfg(unix)]
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        #[cfg(unix)]
        let mut sighup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
        let mut close_id: Option<Value> = None;
        loop {
            tokio::select! {
                line=rx.recv()=>match line {
                    None=>break,
                    Some(Err(error))=>return Err(error.into()),
                    Some(Ok(line))=>{
                        if line.trim().is_empty() {
                            continue;
                        }
                        let request: Value = match serde_json::from_str(&line) {
                            Ok(value) => value,
                            Err(_) => {
                                output::write(json!({"event":"tunnel_error","ok":false,"fatal":false,"error":"Use JSON Lines: {\"op\":\"status|close\"}"}))?;
                                continue;
                            }
                        };
                        let id = request.get("id").cloned().filter(|v| !v.is_null());
                        match request.get("op").and_then(Value::as_str) {
                            Some("close") => {
                                close_id = id;
                                break;
                            }
                            Some("status") => {
                                let mut info = self.snapshot();
                                info["event"] = json!("tunnel_status");
                                info["closed"] = json!(self.is_closed());
                                if let Some(id) = id {
                                    info["id"] = id;
                                }
                                output::write(info)?;
                            }
                            _ => {
                                let mut err = json!({"event":"tunnel_error","ok":false,"fatal":false,"error":"Unknown tunnel operation; use status or close"});
                                if let Some(id) = id {
                                    err["id"] = id;
                                }
                                output::write(err)?;
                            }
                        }
                    }
                },
                _ = &mut closed_wait => break,
                signal=tokio::signal::ctrl_c()=>{signal?;break;},
                _ = async {
                    #[cfg(unix)]
                    {
                        tokio::select! {
                            _ = sigterm.recv() => {}
                            _ = sighup.recv() => {}
                        }
                    }
                    #[cfg(not(unix))]
                    std::future::pending::<()>().await;
                } => break,
            }
        }
        let _ = self.close_tx.try_send(());
        (&mut self.task).await.context("Tunnel shutdown failed")?;
        let mut info = self.snapshot();
        info["event"] = json!("tunnel_closed");
        info["state"] = json!("closed");
        info["ok"] = json!(true);
        if let Some(id) = close_id {
            info["id"] = id;
        }
        output::write(info)?;
        Ok(0)
    }
}

async fn forward(
    local: impl tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
    mut connection: cli::Connection,
) -> ResultType<()> {
    connection.terminal_stream()?.set_raw();
    let (mut local_read, mut local_write) = tokio::io::split(local);
    let (write_tx, mut write_rx) = mpsc::channel::<hbb_common::bytes::Bytes>(16);
    let mut writer = tokio::spawn(async move {
        while let Some(chunk) = write_rx.recv().await {
            tokio::time::timeout(Duration::from_secs(30), local_write.write_all(&chunk))
                .await
                .context("Local TCP reader stalled")??;
        }
        Ok::<(), hbb_common::anyhow::Error>(())
    });
    struct AbortWriter(tokio::task::AbortHandle);
    impl Drop for AbortWriter {
        fn drop(&mut self) {
            self.0.abort();
        }
    }
    let _writer_guard = AbortWriter(writer.abort_handle());
    let mut bytes = vec![0u8; 64 * 1024];
    let mut writer_done = false;
    let res = loop {
        tokio::select! {
            result = local_read.read(&mut bytes) => {
                let n = match result {
                    Ok(n) => n,
                    Err(e) => break Err(e.into()),
                };
                if n == 0 {
                    break Ok(());
                }
                match tokio::time::timeout(
                    Duration::from_secs(30),
                    connection.terminal_stream()?.send_bytes(hbb_common::bytes::Bytes::copy_from_slice(&bytes[..n])),
                ).await {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => break Err(e.into()),
                    Err(_) => break Err(hbb_common::anyhow::anyhow!("TCP peer write stalled")),
                }
            }
            result = connection.terminal_stream()?.next() => {
                match result {
                    Some(Ok(chunk)) => {
                        if write_tx.send(chunk.freeze()).await.is_err() {
                            break Err(hbb_common::anyhow::anyhow!("Local TCP reader stopped"));
                        }
                    }
                    Some(Err(error)) => break Err(error.into()),
                    None => break Ok(()),
                }
            }
            joined = &mut writer => {
                writer_done = true;
                match joined {
                    Ok(Ok(())) => break Ok(()),
                    Ok(Err(e)) => break Err(e),
                    Err(e) => break Err(e.into()),
                }
            }
        }
    };
    drop(write_tx);
    if !writer_done {
        if res.is_ok() {
            if let Err(e) = writer.await.unwrap_or_else(|e| Err(e.into())) {
                return Err(e);
            }
        } else {
            writer.abort();
        }
    }
    res
}

pub(super) async fn manage(op: &str, mut args: impl Iterator<Item = String>) -> ResultType<i32> {
    let mut device = String::new();
    let mut tunnel = None;
    while let Some(arg) = args.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((name, val)) => (name, Some(val.to_owned())),
            None => (arg.as_str(), None),
        };
        if matches!(flag, "--help" | "-h") {
            println!("{}", super::cli::USAGE);
            return Ok(0);
        }
        if matches!(flag, "--json" | "--non-interactive") {
            if inline.is_some() {
                bail!("Option {flag} does not take a value");
            }
            continue;
        }
        if !matches!(flag, "--id" | "--tunnel-id") {
            bail!("Unknown option: {flag}");
        }
        let value = match inline {
            Some(v) => v,
            None => args.next().with_context(|| format!("Missing value for {flag}"))?,
        };
        match flag {
            "--id" if device.is_empty() => device = value,
            "--tunnel-id" if tunnel.is_none() => tunnel = Some(value),
            _ => bail!("Unknown or duplicate tunnel option: {flag}"),
        }
    }
    let device = cli::normalize_device_id(&device).1;
    let value = match op {
        "list" if tunnel.is_none() => {
            json!({"event":"tunnel_list","ok":true,"tunnels":list(&device).await?})
        }
        "close" => {
            let mut value = close(&device, &tunnel.context("Missing --tunnel-id")?).await?;
            value["event"] = json!("tunnel_closed");
            value["ok"] = json!(true);
            value
        }
        _ => bail!("Use tunnel open, list or close"),
    };
    output::write(value)?;
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn control_record(port: u16) -> Record {
        Record { tunnel_id: uuid::Uuid::new_v4().to_string(), device_id:"test-device".into(),
            local_port:1, remote_host:"127.0.0.1".into(), remote_port:22, owner_pid:0,
            purpose:"test".into(), control_port:port, secret:uuid::Uuid::new_v4().to_string(),
            relay:true, ..Default::default() }
    }

    #[tokio::test]
    async fn refused_control_port_reaches_cleanup_instead_of_short_rpc_timeout() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let record = control_record(listener.local_addr().unwrap().port());
        drop(listener);
        let error = rpc(&record, "status").await.unwrap_err();
        assert!(error.chain().any(|cause| cause.downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::ConnectionRefused)), "{error:#}");
    }

    #[tokio::test]
    async fn accepted_close_still_notifies_owner_when_reply_is_interrupted() {
        let (mut client, mut socket) = tokio::io::duplex(16);
        let (tx, mut rx) = mpsc::channel(1);
        let (release, released) = watch::channel(false);
        let task = tokio::spawn(async move {
            let state = State::default();
            send_control_response(&mut socket, &control_record(1), &state, true, tx, released).await
        });
        assert_eq!(rx.recv().await, Some(()));
        assert!(tokio::time::timeout(Duration::from_millis(50), client.read_u8()).await.is_err());
        drop(client);
        release.send_replace(true);
        assert!(task.await.unwrap().is_err());
    }

    #[tokio::test]
    async fn close_acknowledges_only_after_data_plane_releases() {
        let (client, mut socket) = tokio::io::duplex(8192);
        let mut client = BufReader::new(client);
        let (tx, mut rx) = mpsc::channel(1);
        let (release, released) = watch::channel(false);
        let state = Arc::new(State::default());
        state.active.store(1, Ordering::Relaxed);
        let status = state.clone();
        let task = tokio::spawn(async move {
            send_control_response(&mut socket, &control_record(1), &status, true, tx, released).await
        });
        assert_eq!(rx.recv().await, Some(()));
        assert!(tokio::time::timeout(Duration::from_millis(50), client.fill_buf()).await.is_err());
        state.active.store(0, Ordering::Relaxed);
        state.closed.store(true, Ordering::Relaxed);
        release.send_replace(true);
        let response = read_line(&mut client, 8192).await.unwrap();
        let response: Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(response["state"], "closed");
        assert_eq!(response["active_connections"], 0);
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn reused_control_port_never_receives_an_operation_or_owner_secret() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let record = Record {
            tunnel_id: uuid::Uuid::new_v4().to_string(),
            device_id: "test".into(),
            local_port: 1,
            remote_host: "localhost".into(),
            remote_port: 22,
            owner_pid: 0,
            purpose: "test".into(),
            control_port: listener.local_addr().unwrap().port(),
            secret: "SYNTHETIC_PRIVATE_OWNER_TOKEN".into(),
            relay: true,
            ..Default::default()
        };
        let expected_id = record.tunnel_id.clone();
        let secret = record.secret.clone();
        let server = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let mut socket = BufReader::new(socket);
            let first = read_line(&mut socket, CONTROL_LIMIT).await.unwrap();
            assert!(!String::from_utf8_lossy(&first).contains(&secret));
            let hello: Value = serde_json::from_slice(&first).unwrap();
            assert!(hello.get("op").is_none());
            socket.get_mut().write_all(&serde_json::to_vec(&json!({"protocol":CONTROL_PROTOCOL,"tunnel_id":expected_id,"nonce":hello["nonce"],"proof":"00".repeat(32)})).unwrap()).await.unwrap();
            socket.get_mut().write_all(b"\n").await.unwrap();
            let mut extra = [0; 1];
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(1), socket.read(&mut extra))
                    .await
                    .unwrap()
                    .unwrap(),
                0
            );
        });
        let error = rpc(&record, "close").await.unwrap_err();
        assert!(format!("{error:#}").contains("does not prove ownership"));
        server.await.unwrap();
    }

    #[tokio::test]
    async fn occupied_port_and_bad_mapping_fail_before_network_authentication() {
        let occupied = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let options = cli::ConnectOptions {
            connect_id: "unreachable-test-device".into(),
            id: "unreachable-test-device".into(),
            relay: true,
            token_override: Some(String::new()),
            device_password: String::new(),
            admin: None,
            timeout: 1,
            expected_platform: None,
            expected_hostname: None,
            ssh: None,
        };
        let error = match Managed::start(
            &options,
            occupied.local_addr().unwrap().port(),
            "127.0.0.1".into(),
            22,
            "test",
        )
        .await
        {
            Ok(_) => panic!("Occupied port accepted"),
            Err(error) => error,
        };
        assert!(format!("{error:#}").contains("Local tunnel port is unavailable"));
        let error = match Managed::start(&options, 0, "127.0.0.1".into(), 0, "test").await {
            Ok(_) => panic!("Remote port zero accepted"),
            Err(error) => error,
        };
        assert!(format!("{error:#}").contains("valid remote host and port"));
    }

    #[test]
    fn list_sorting_and_legacy_record_deserialization_are_deterministic() {
        let mut items = vec![
            json!({"tunnel_id": "b", "local_port": 9000}),
            json!({"tunnel_id": "a", "local_port": 9000}),
            json!({"tunnel_id": "c", "local_port": 8000}),
        ];
        sort_listed_tunnels(&mut items);
        let ids: Vec<_> = items.iter().filter_map(|v| v["tunnel_id"].as_str()).collect();
        assert_eq!(ids, vec!["c", "a", "b"]);

        let legacy_json = r#"{
            "tunnel_id": "00000000-0000-0000-0000-000000000001",
            "device_id": "123",
            "local_port": 2222,
            "remote_host": "127.0.0.1",
            "remote_port": 22,
            "owner_pid": 42,
            "purpose": "test",
            "control_port": 3333,
            "secret": "s"
        }"#;
        let record: Record = serde_json::from_str(legacy_json).unwrap();
        let value = record.public(&State::default());
        assert_eq!(value["direct"], false);
        assert_eq!(value["transport"], "unknown");
        assert_eq!(value["relay"], true);
        assert_eq!(value["multiplexed"], false);
    }

    #[tokio::test]
    async fn state_mark_closed_wakes_waiter_without_polling() {
        let state = Arc::new(State::default());
        let waiter = {
            let state = state.clone();
            tokio::spawn(async move {
                loop {
                    let notified = state.closed_notify.notified();
                    if state.closed.load(Ordering::Relaxed) {
                        break;
                    }
                    notified.await;
                }
            })
        };
        tokio::task::yield_now().await;
        state.mark_closed();
        tokio::time::timeout(Duration::from_secs(1), waiter)
            .await
            .unwrap()
            .unwrap();
    }
}
