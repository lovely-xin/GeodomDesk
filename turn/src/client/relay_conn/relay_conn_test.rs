use std::net::{IpAddr, Ipv4Addr};

use super::*;
use crate::error::Result;

struct DummyRelayConnObserver {
    turn_server_addr: String,
    username: Username,
    realm: Realm,
}

#[async_trait]
impl RelayConnObserver for DummyRelayConnObserver {
    fn turn_server_addr(&self) -> String {
        self.turn_server_addr.clone()
    }

    fn username(&self) -> Username {
        self.username.clone()
    }

    fn realm(&self) -> Realm {
        self.realm.clone()
    }

    async fn write_to(&self, _data: &[u8], _to: &str) -> std::result::Result<usize, util::Error> {
        Ok(0)
    }

    async fn perform_transaction(
        &mut self,
        _msg: &Message,
        _to: &str,
        _dont_wait: bool,
    ) -> Result<TransactionResult> {
        Err(Error::ErrFakeErr)
    }
}

#[tokio::test]
async fn test_relay_conn() -> Result<()> {
    let obs = DummyRelayConnObserver {
        turn_server_addr: String::new(),
        username: Username::new(ATTR_USERNAME, "username".to_owned()),
        realm: Realm::new(ATTR_REALM, "realm".to_owned()),
    };

    let (_read_ch_tx, read_ch_rx) = mpsc::channel(100);

    let config = RelayConnConfig {
        relayed_addr: SocketAddr::new(Ipv4Addr::new(0, 0, 0, 0).into(), 0),
        integrity: MessageIntegrity::default(),
        nonce: Nonce::new(ATTR_NONCE, "nonce".to_owned()),
        lifetime: Duration::from_secs(0),
        binding_mgr: Arc::new(Mutex::new(BindingManager::new())),
        read_ch_rx: Arc::new(Mutex::new(read_ch_rx)),
    };

    let rc = RelayConn::new(Arc::new(Mutex::new(obs)), config).await;

    let rci = rc.relay_conn.lock().await;
    let (bind_addr, bind_number) = {
        let mut bm = rci.binding_mgr.lock().await;
        let b = bm
            .create(SocketAddr::new(Ipv4Addr::new(127, 0, 0, 1).into(), 1234))
            .unwrap();
        (b.addr, b.number)
    };

    //let binding_mgr = Arc::clone(&rci.binding_mgr);
    let rc_obs = Arc::clone(&rci.obs);
    let nonce = rci.nonce.clone();
    let integrity = rci.integrity.clone();

    if let Err(err) =
        RelayConnInternal::bind(rc_obs, bind_addr, bind_number, nonce, integrity).await
    {
        assert!(Error::ErrUnexpectedResponse != err);
    } else {
        panic!("should fail");
    }

    Ok(())
}

struct PermissionObserver {
    requests: Vec<SocketAddr>,
    blocked: IpAddr,
    fail_next: bool,
}

#[async_trait]
impl RelayConnObserver for PermissionObserver {
    fn turn_server_addr(&self) -> String {
        String::new()
    }

    fn username(&self) -> Username {
        Username::new(ATTR_USERNAME, "username".to_owned())
    }

    fn realm(&self) -> Realm {
        Realm::new(ATTR_REALM, "realm".to_owned())
    }

    async fn write_to(&self, data: &[u8], _to: &str) -> std::result::Result<usize, util::Error> {
        Ok(data.len())
    }

    async fn perform_transaction(
        &mut self,
        request: &Message,
        _to: &str,
        _ignore_result: bool,
    ) -> Result<TransactionResult> {
        use crate::proto::peeraddr::PeerAddress;

        if request.typ.method != METHOD_CREATE_PERMISSION {
            return Err(Error::ErrFakeErr);
        }
        let mut peer = PeerAddress::default();
        peer.get_from(request)?;
        let address = SocketAddr::new(peer.ip, peer.port);
        self.requests.push(address);
        let mut response = Message::new();
        let is_batch = request
            .attributes
            .0
            .iter()
            .filter(|attribute| attribute.typ == ATTR_XOR_PEER_ADDRESS)
            .count()
            > 1;
        let class = if address.ip() == self.blocked || is_batch {
            CLASS_ERROR_RESPONSE
        } else {
            if std::mem::take(&mut self.fail_next) {
                return Err(Error::ErrFakeErr);
            }
            CLASS_SUCCESS_RESPONSE
        };
        response.typ = MessageType::new(METHOD_CREATE_PERMISSION, class);
        if class == CLASS_ERROR_RESPONSE {
            ErrorCodeAttribute {
                code: CODE_FORBIDDEN,
                reason: b"Forbidden".to_vec(),
            }
            .add_to(&mut response)?;
        }
        Ok(TransactionResult {
            msg: response,
            ..Default::default()
        })
    }
}

#[tokio::test]
async fn caches_forbidden_peer_per_allocation_and_retries_temporary_failure() -> Result<()> {
    let blocked: SocketAddr = "127.0.0.1:1234".parse()?;
    let observer = Arc::new(Mutex::new(PermissionObserver {
        requests: Vec::new(),
        blocked: blocked.ip(),
        fail_next: true,
    }));
    let (_read_tx, read_rx) = mpsc::channel(1);
    let config = RelayConnConfig {
        relayed_addr: "127.0.0.1:0".parse()?,
        integrity: MessageIntegrity::default(),
        nonce: Nonce::new(ATTR_NONCE, "nonce".to_owned()),
        lifetime: Duration::from_secs(600),
        binding_mgr: Arc::new(Mutex::new(BindingManager::new())),
        read_ch_rx: Arc::new(Mutex::new(read_rx)),
    };
    let mut relay = RelayConnInternal::new(observer.clone(), config);
    let other_port = "127.0.0.1:5678".parse()?;
    let allowed = "192.0.2.1:1234".parse()?;
    assert_eq!(
        relay.send_to(b"test", blocked).await,
        Err(Error::ErrPeerForbidden)
    );
    assert_eq!(
        relay.send_to(b"test", other_port).await,
        Err(Error::ErrPeerForbidden)
    );
    assert_eq!(
        relay.send_to(b"test", allowed).await,
        Err(Error::ErrFakeErr)
    );
    assert!(relay.send_to(b"test", allowed).await? > 0);
    assert_eq!(observer.lock().await.requests, [blocked, allowed, allowed]);
    assert_eq!(relay.perm_map.addrs(), [SocketAddr::new(allowed.ip(), 0)]);

    let revoked: SocketAddr = "192.0.2.2:1234".parse()?;
    assert!(relay.send_to(b"test", revoked).await? > 0);
    observer.lock().await.blocked = revoked.ip();
    relay.refresh_permissions().await?;
    assert_eq!(relay.perm_map.addrs(), [SocketAddr::new(allowed.ip(), 0)]);
    assert_eq!(
        relay.send_to(b"test", revoked).await,
        Err(Error::ErrPeerForbidden)
    );
    assert!(relay.send_to(b"test", allowed).await? > 0);
    assert_eq!(observer.lock().await.requests.len(), 7);
    Ok(())
}
