//! Process-owned loopback mappings. The control socket, rather than a PID, proves liveness.
use super::{cli, output};
use hbb_common::{
    anyhow::Context,
    bail,
    config::Config,
    futures::{stream, StreamExt},
    tokio, ResultType,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    task::JoinSet,
};

const MAX_CHANNELS: usize = 16;
const CONTROL_LIMIT: usize = 2048;

#[derive(Clone, Serialize, Deserialize)]
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
}

#[derive(Default)]
struct State {
    active: AtomicUsize,
    closed: AtomicBool,
    last_error: Mutex<String>,
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
    fs::create_dir_all(&path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(path)
}

fn record_path(id: &str) -> ResultType<PathBuf> {
    let id = uuid::Uuid::parse_str(id).context("Invalid tunnel ID")?;
    Ok(directory()?.join(format!("{id}.json")))
}

impl Record {
    fn public(&self, state: &State) -> Value {
        let active = state.active.load(Ordering::Relaxed);
        json!({"tunnel_id":self.tunnel_id,"device_id":self.device_id,
            "local_host":"127.0.0.1","local_port":self.local_port,"private_stream":self.local_port == 0,
            "remote_host":self.remote_host,"remote_port":self.remote_port,
            "owner":"cli","owner_pid":self.owner_pid,"purpose":self.purpose,
            "state":if state.closed.load(Ordering::Relaxed){"closed"}else if active>0{"active"}else{"listening"},
            "active_connections":active,"max_connections":if self.local_port == 0 {1} else {MAX_CHANNELS},
            "last_error":*state.last_error.lock().unwrap_or_else(|poison| poison.into_inner()),"relay":true,
            "payload_encryption":"provided by the forwarded protocol (SSH/HTTPS)"})
    }
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
    let bytes = hex::decode(proof).context("Invalid tunnel owner proof")?;
    let tag = hbb_common::sodiumoxide::crypto::auth::Tag::from_slice(&bytes)
        .context("Invalid tunnel owner proof")?;
    if !hbb_common::sodiumoxide::crypto::auth::verify(
        &tag,
        owner_message(record, nonce).as_bytes(),
        &owner_key(record)?,
    ) {
        bail!("Control endpoint does not prove ownership of this tunnel");
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
        let hello: Value = serde_json::from_slice(&read_line(&mut socket, CONTROL_LIMIT).await?)?;
        if hello["protocol"] != CONTROL_PROTOCOL
            || hello["tunnel_id"] != record.tunnel_id
            || hello["nonce"] != nonce
        {
            bail!("Tunnel owner identity changed");
        }
        verify_owner(
            record,
            &nonce,
            hello["proof"]
                .as_str()
                .context("Missing tunnel owner proof")?,
        )?;
        let bytes = serde_json::to_vec(
            &json!({"tunnel_id":record.tunnel_id,"secret":record.secret,"op":op}),
        )?;
        socket.get_mut().write_all(&bytes).await?;
        socket.get_mut().write_all(b"\n").await?;
        let response = read_line(&mut socket, 8192).await?;
        let value: Value = serde_json::from_slice(&response)?;
        if value["tunnel_id"] != record.tunnel_id {
            bail!("Tunnel owner identity changed");
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
    let records = records()?;
    let requests = records
        .into_iter()
        .filter(|(_, r)| device.is_empty() || r.device_id == device)
        .map(|(path, r)| async move {
            match rpc(&r, "status").await {
                Ok(value) => Some(value),
                Err(error) => {
                    // A dead process cannot own an OS listener. Never kill a PID or another mapping.
                    if error.chain().any(|e| {
                        e.downcast_ref::<std::io::Error>()
                            .is_some_and(|e| e.kind() == std::io::ErrorKind::ConnectionRefused)
                    }) {
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
    let values = stream::iter(requests)
        .buffer_unordered(16)
        .filter_map(hbb_common::futures::future::ready)
        .collect::<Vec<_>>()
        .await;
    Ok(json!(values))
}

pub(crate) async fn close(device: &str, id: &str) -> ResultType<Value> {
    let bytes = match fs::read(record_path(id)?) {
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
        Err(error) if error.chain().any(|cause| cause.downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::ConnectionRefused)) => {
            if let Err(error) = fs::remove_file(record_path(id)?) {
                if error.kind() != std::io::ErrorKind::NotFound { return Err(error.into()); }
            }
            Ok(json!({"tunnel_id":record.tunnel_id,"state":"closed","already_closed":true}))
        }
        result => result,
    }
}

// Once authenticated, closing the owner must not depend on delivering its reply.
struct CloseRequest(tokio::sync::mpsc::Sender<()>);
impl Drop for CloseRequest {
    fn drop(&mut self) { let _ = self.0.try_send(()); }
}

async fn send_control_response(
    socket: &mut (impl tokio::io::AsyncWrite + Unpin),
    record: &Record, state: &State, close: bool,
    close_tx: tokio::sync::mpsc::Sender<()>,
) -> ResultType<()> {
    let _close = close.then(|| CloseRequest(close_tx));
    if close { state.closed.store(true, Ordering::Relaxed); }
    socket.write_all(&serde_json::to_vec(&record.public(state))?).await?;
    socket.write_all(b"\n").await?;
    Ok(())
}

pub(super) struct Managed {
    pub(super) info: Value,
    pub(super) peer: base::message_proto::PeerInfo,
    pub(super) setup_errors: Vec<super::stage::SetupError>,
    state: Arc<State>,
    task: tokio::task::JoinHandle<()>,
    path: PathBuf,
}
impl Drop for Managed {
    fn drop(&mut self) {
        self.state.closed.store(true, Ordering::Relaxed);
        self.task.abort();
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
        let mut info = self.info.clone();
        let active = self.state.active.load(Ordering::Relaxed);
        info["state"] = json!(if self.is_closed() {
            "closed"
        } else if active > 0 {
            "active"
        } else {
            "listening"
        });
        info["active_connections"] = json!(active);
        info["last_error"] = json!(*self.state.last_error.lock().unwrap_or_else(|poison| poison.into_inner()));
        info
    }
    pub(super) fn port(&self) -> u16 {
        self.info["local_port"].as_u64().unwrap_or_default() as u16
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
        options.relay = true;
        let first = options.open_forward(&host, remote_port).await?;
        let peer = first.peer.clone();
        let setup_errors = first.setup_errors.clone();
        let state = Arc::new(State::default());
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
        let status = state.clone();
        let task = tokio::spawn(async move {
            let _registration = registration;
            let mut first = Some((first, Instant::now()));
            let mut channels = JoinSet::new();
            let mut controls = JoinSet::new();
            let private = private_stream.is_some();
            if let (Some(stream), Some((connection, _))) = (private_stream, first.take_if(|_| private)) {
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
            let (close_tx, mut close_rx) = tokio::sync::mpsc::channel::<()>(1);
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
                        let (socket,_)=match accepted {Ok(value)=>value,Err(error)=>{*status.last_error.lock().unwrap_or_else(|poison| poison.into_inner())=error.to_string();break;}};
                        if status.closed.load(Ordering::Relaxed) || status.active.load(Ordering::Relaxed)>=MAX_CHANNELS {drop(socket);continue;}
                        let saved=first.take().filter(|(_,at)|at.elapsed()<Duration::from_secs(5)).map(|(c,_)|c);
                        let options=options.clone();let record=record.clone();let status=status.clone();
                        status.active.fetch_add(1,Ordering::Relaxed);
                        channels.spawn(async move {
                            struct Count(Arc<State>);
                            impl Drop for Count {fn drop(&mut self){self.0.active.fetch_sub(1,Ordering::Relaxed);}}
                            let _count=Count(status.clone());
                            let result=async {
                                let connection=match saved {Some(c)=>c,None=>options.open_forward(&record.remote_host,record.remote_port).await?};
                                forward(socket,connection).await
                            }.await;
                            if let Err(error)=result { *status.last_error.lock().unwrap_or_else(|poison| poison.into_inner())=format!("{error:#}").chars().take(512).collect(); }
                        });
                    },
                    accepted=control.accept()=>{
                        let (socket,_)=match accepted {Ok(value)=>value,Err(_)=>break};
                        if controls.len()>=8 {drop(socket);continue;}
                        let record=record.clone();let status=status.clone();let close_tx=close_tx.clone();
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
                                if value["secret"]!=record.secret || value["tunnel_id"]!=record.tunnel_id {bail!("Invalid tunnel owner token");}
                                let close=value["op"]=="close";
                                if !close && value["op"]!="status" {bail!("Unknown tunnel operation");}
                                send_control_response(socket.get_mut(), &record, &status, close, close_tx).await
                            }).await;
                        });
                    },
                    _=close_rx.recv()=>break,
                    _=channels.join_next(),if !channels.is_empty()=>{if private {break;}},
                    _=controls.join_next(),if !controls.is_empty()=>{},
                }
            }
            status.closed.store(true, Ordering::Relaxed);
        });
        Ok(Self {
            info,
            peer,
            setup_errors,
            state,
            task,
            path,
        })
    }

    pub(super) async fn serve(self) -> ResultType<i32> {
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
        let mut tick = tokio::time::interval(Duration::from_millis(100));
        loop {
            tokio::select! {
                line=rx.recv()=>match line {
                    None=>break,
                    Some(Err(error))=>return Err(error.into()),
                    Some(Ok(line))=>{
                        let request:Value=serde_json::from_str(&line).context("Use JSON Lines: {\"op\":\"status|close\"}")?;
                        match request["op"].as_str() {
                            Some("close")=>break,
                            Some("status")=>{let mut info=self.snapshot();info["event"]=json!("tunnel_status");info["closed"]=json!(self.is_closed());output::write(info)?;},
                            _=>bail!("Unknown tunnel operation; use status or close"),
                        }
                    }
                },
                _=tick.tick()=>if self.is_closed(){break;},
                signal=tokio::signal::ctrl_c()=>{signal?;break;},
            }
        }
        let mut info = self.info.clone();
        drop(self);
        info["event"] = json!("tunnel_closed");
        info["state"] = json!("closed");
        info["ok"] = json!(true);
        output::write(info)?;
        Ok(0)
    }
}

async fn forward(
    mut local: impl tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    mut connection: cli::Connection,
) -> ResultType<()> {
    connection.terminal_stream()?.set_raw();
    let mut bytes = vec![0u8; 32 * 1024];
    loop {
        tokio::select! {
            result=local.read(&mut bytes)=>{
                let n=result?;if n==0 {break;}
                tokio::time::timeout(Duration::from_secs(30),connection.terminal_stream()?.send_bytes(bytes[..n].to_vec().into())).await.context("TCP peer write stalled")??;
            },
            result=connection.terminal_stream()?.next()=>{
                match result {Some(Ok(bytes))=>tokio::time::timeout(Duration::from_secs(30),local.write_all(&bytes)).await.context("Local TCP reader stalled")??,Some(Err(error))=>return Err(error.into()),None=>break}
            },
        }
    }
    Ok(())
}

pub(super) async fn manage(op: &str, mut args: impl Iterator<Item = String>) -> ResultType<i32> {
    let mut device = String::new();
    let mut tunnel = None;
    while let Some(flag) = args.next() {
        if matches!(flag.as_str(), "--json" | "--non-interactive") {
            continue;
        }
        let value = args
            .next()
            .with_context(|| format!("Missing value for {flag}"))?;
        match flag.as_str() {
            "--id" if device.is_empty() => device = value,
            "--tunnel-id" if tunnel.is_none() => tunnel = Some(value),
            _ => bail!("Unknown or duplicate tunnel option: {flag}"),
        }
    }
    let device = device.trim();
    let device = if device.chars().all(|c| c.is_ascii_digit() || c.is_ascii_whitespace()) {
        device.chars().filter(|c| !c.is_ascii_whitespace()).collect::<String>()
    } else { device.to_owned() };
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
            purpose:"test".into(), control_port:port, secret:uuid::Uuid::new_v4().to_string() }
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
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let task = tokio::spawn(async move {
            send_control_response(&mut socket, &control_record(1), &State::default(), true, tx).await
        });
        assert_eq!(client.read_u8().await.unwrap(), b'{');
        drop(client); // The response has started, then its receiver disappears.
        assert!(task.await.unwrap().is_err());
        assert_eq!(rx.recv().await, Some(()));
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
            id: "unreachable-test-device".into(),
            relay: true,
            token: String::new(),
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
}
