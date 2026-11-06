use std::net::IpAddr;
use std::sync::atomic::{AtomicUsize, Ordering};

use tokio::net::UdpSocket;
use tokio::sync::Notify;
use tokio::time::{timeout, Duration};
use util::conn::conn_pipe::pipe;
use util::vnet::net::*;

use super::*;
use crate::auth::*;
use crate::relay::relay_static::*;
use crate::server::config::*;
use crate::server::*;

struct StalledSend {
    socket: UdpSocket,
    send_count: AtomicUsize,
    stall_on: usize,
    entered: Notify,
    released: Notify,
}

impl StalledSend {
    async fn new(stall_on: usize) -> Result<Arc<Self>> {
        Ok(Arc::new(Self {
            socket: UdpSocket::bind("127.0.0.1:0").await?,
            send_count: AtomicUsize::new(0),
            stall_on,
            entered: Notify::new(),
            released: Notify::new(),
        }))
    }
}

struct NotifyOnDrop<'a>(&'a Notify);

impl Drop for NotifyOnDrop<'_> {
    fn drop(&mut self) {
        self.0.notify_one();
    }
}

#[async_trait]
impl Conn for StalledSend {
    async fn connect(&self, addr: SocketAddr) -> util::Result<()> {
        Conn::connect(&self.socket, addr).await
    }
    async fn recv(&self, buf: &mut [u8]) -> util::Result<usize> {
        Conn::recv(&self.socket, buf).await
    }
    async fn recv_from(&self, buf: &mut [u8]) -> util::Result<(usize, SocketAddr)> {
        Conn::recv_from(&self.socket, buf).await
    }
    async fn send(&self, buf: &[u8]) -> util::Result<usize> {
        Conn::send(&self.socket, buf).await
    }
    async fn send_to(&self, buf: &[u8], _: SocketAddr) -> util::Result<usize> {
        if self.send_count.fetch_add(1, Ordering::Relaxed) == self.stall_on {
            let _release = NotifyOnDrop(&self.released);
            self.entered.notify_one();
            return std::future::pending().await;
        }
        Ok(buf.len())
    }
    fn local_addr(&self) -> util::Result<SocketAddr> {
        Conn::local_addr(&self.socket)
    }
    fn remote_addr(&self) -> Option<SocketAddr> {
        Conn::remote_addr(&self.socket)
    }
    async fn close(&self) -> util::Result<()> {
        Conn::close(&self.socket).await
    }
    fn as_any(&self) -> &(dyn std::any::Any + Send + Sync) {
        self
    }
}

async fn transaction_client(conn: Arc<dyn Conn + Send + Sync>) -> Result<ClientInternal> {
    ClientInternal::new(ClientConfig {
        stun_serv_addr: String::new(),
        turn_serv_addr: String::new(),
        username: String::new(),
        password: String::new(),
        realm: String::new(),
        software: String::new(),
        rto_in_ms: 15,
        conn,
        vnet: None,
    })
    .await
}

fn test_binding_request() -> Result<Message> {
    let mut request = Message::new();
    request.build(&[Box::new(BINDING_REQUEST), Box::new(TransactionId::new())])?;
    Ok(request)
}

#[tokio::test]
async fn transaction_is_removed_when_first_send_fails() -> Result<()> {
    let (conn, _peer) = pipe();
    let mut client = transaction_client(Arc::new(conn)).await?;
    let request = test_binding_request()?;
    assert!(client
        .perform_transaction(&request, "127.0.0.1:9", false)
        .await
        .is_err());
    assert_eq!(client.tr_map.lock().size(), 0);
    Ok(())
}

#[tokio::test]
async fn transaction_is_removed_when_first_send_is_cancelled() -> Result<()> {
    let conn = StalledSend::new(0).await?;
    let mut client = transaction_client(conn.clone()).await?;
    let request = test_binding_request()?;
    {
        let send = client.perform_transaction(&request, "127.0.0.1:9", false);
        tokio::pin!(send);
        tokio::select! {
            _ = &mut send => panic!("the first send should still be stalled"),
            result = timeout(Duration::from_secs(1), conn.entered.notified()) => result.unwrap(),
        }
    }
    assert_eq!(client.tr_map.lock().size(), 0);
    Ok(())
}

#[tokio::test]
async fn blocked_retransmission_does_not_prevent_processing_responses() -> Result<()> {
    let conn = StalledSend::new(1).await?;
    let mut client = transaction_client(conn.clone()).await?;
    let destination: SocketAddr = "127.0.0.1:9".parse().unwrap();
    let first = test_binding_request()?;
    client
        .perform_transaction(&first, &destination.to_string(), true)
        .await?;
    timeout(Duration::from_secs(1), conn.entered.notified())
        .await
        .unwrap();

    let second = test_binding_request()?;
    client
        .perform_transaction(&second, &destination.to_string(), true)
        .await?;
    for id in [second.transaction_id, first.transaction_id] {
        let mut response = Message::new();
        response.build(&[Box::new(BINDING_SUCCESS), Box::new(id)])?;
        timeout(
            Duration::from_secs(1),
            ClientInternal::handle_stun_message(
                &client.tr_map,
                &client.read_ch_tx,
                &response.raw,
                destination,
            ),
        )
        .await
        .unwrap()?;
    }
    assert_eq!(client.tr_map.lock().size(), 0);
    timeout(Duration::from_secs(1), conn.released.notified())
        .await
        .unwrap();
    Ok(())
}

async fn create_listening_test_client(rto_in_ms: u16) -> Result<Client> {
    let conn = UdpSocket::bind("0.0.0.0:0").await?;

    let c = Client::new(ClientConfig {
        stun_serv_addr: String::new(),
        turn_serv_addr: String::new(),
        username: String::new(),
        password: String::new(),
        realm: String::new(),
        software: "TEST SOFTWARE".to_owned(),
        rto_in_ms,
        conn: Arc::new(conn),
        vnet: None,
    })
    .await?;

    c.listen().await?;

    Ok(c)
}

async fn create_listening_test_client_with_stun_serv() -> Result<Client> {
    let conn = UdpSocket::bind("0.0.0.0:0").await?;

    let c = Client::new(ClientConfig {
        stun_serv_addr: "stun1.l.google.com:19302".to_owned(),
        turn_serv_addr: String::new(),
        username: String::new(),
        password: String::new(),
        realm: String::new(),
        software: "TEST SOFTWARE".to_owned(),
        rto_in_ms: 0,
        conn: Arc::new(conn),
        vnet: None,
    })
    .await?;

    c.listen().await?;

    Ok(c)
}

#[tokio::test]
async fn test_client_with_stun_send_binding_request() -> Result<()> {
    //env_logger::init();

    let c = create_listening_test_client_with_stun_serv().await?;

    let resp = c.send_binding_request().await?;
    log::debug!("mapped-addr: {}", resp);
    {
        let ci = c.client_internal.lock().await;
        let tm = ci.tr_map.lock();
        assert_eq!(0, tm.size(), "should be no transaction left");
    }

    c.close().await?;

    Ok(())
}

#[tokio::test]
async fn test_client_with_stun_send_binding_request_to_parallel() -> Result<()> {
    env_logger::init();

    let c1 = create_listening_test_client(0).await?;
    let c2 = c1.clone();

    let (stared_tx, mut started_rx) = mpsc::channel::<()>(1);
    let (finished_tx, mut finished_rx) = mpsc::channel::<()>(1);

    let to = lookup_host(true, "stun1.l.google.com:19302").await?;

    tokio::spawn(async move {
        drop(stared_tx);
        if let Ok(resp) = c2.send_binding_request_to(&to.to_string()).await {
            log::debug!("mapped-addr: {}", resp);
        }
        drop(finished_tx);
    });

    let _ = started_rx.recv().await;

    let resp = c1.send_binding_request_to(&to.to_string()).await?;
    log::debug!("mapped-addr: {}", resp);

    let _ = finished_rx.recv().await;

    c1.close().await?;

    Ok(())
}

#[tokio::test]
async fn test_client_with_stun_send_binding_request_to_timeout() -> Result<()> {
    //env_logger::init();

    let c = create_listening_test_client(10).await?;

    let to = lookup_host(true, "127.0.0.1:9").await?;

    let result = c.send_binding_request_to(&to.to_string()).await;
    assert!(result.is_err(), "expected error, but got ok");

    c.close().await?;

    Ok(())
}

struct TestAuthHandler;
impl AuthHandler for TestAuthHandler {
    fn auth_handle(&self, username: &str, realm: &str, _src_addr: SocketAddr) -> Result<Vec<u8>> {
        Ok(generate_auth_key(username, realm, "pass"))
    }
}

// Create an allocation, and then delete all nonces
// The subsequent Write on the allocation will cause a CreatePermission
// which will be forced to handle a stale nonce response
#[tokio::test]
async fn test_client_nonce_expiration() -> Result<()> {
    // env_logger::init();

    // here, it should use static port, like "0.0.0.0:3478",
    // but, due to different test environment, let's fake it by using "0.0.0.0:0"
    // to auto assign a "static" port
    let conn = Arc::new(UdpSocket::bind("0.0.0.0:0").await?);
    let server_port = conn.local_addr()?.port();

    let server = Server::new(ServerConfig {
        conn_configs: vec![ConnConfig {
            conn,
            relay_addr_generator: Box::new(RelayAddressGeneratorStatic {
                relay_address: IpAddr::from_str("127.0.0.1")?,
                address: "0.0.0.0".to_owned(),
                net: Arc::new(Net::new(None)),
            }),
        }],
        realm: "webrtc.rs".to_owned(),
        auth_handler: Arc::new(TestAuthHandler {}),
        channel_bind_timeout: Duration::from_secs(0),
        alloc_close_notify: None,
    })
    .await?;

    let conn = Arc::new(UdpSocket::bind("0.0.0.0:0").await?);

    let client = Client::new(ClientConfig {
        stun_serv_addr: format!("127.0.0.1:{server_port}"),
        turn_serv_addr: format!("127.0.0.1:{server_port}"),
        username: "foo".to_owned(),
        password: "pass".to_owned(),
        realm: String::new(),
        software: String::new(),
        rto_in_ms: 0,
        conn,
        vnet: None,
    })
    .await?;

    client.listen().await?;

    let allocation = client.allocate().await?;

    {
        let mut nonces = server.nonces.lock().await;
        nonces.clear();
    }

    allocation
        .send_to(&[0x00], SocketAddr::from_str("127.0.0.1:8080")?)
        .await?;

    // Shutdown
    client.close().await?;
    server.close().await?;

    Ok(())
}
