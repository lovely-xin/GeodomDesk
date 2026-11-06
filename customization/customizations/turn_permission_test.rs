
struct PermissionObserver {
    requests: Vec<SocketAddr>,
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
        let class = if address.ip().is_loopback() {
            CLASS_ERROR_RESPONSE
        } else {
            if self.requests.len() == 2 {
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
    let observer = Arc::new(Mutex::new(PermissionObserver {
        requests: Vec::new(),
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
    let blocked = "127.0.0.1:1234".parse()?;
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
    assert_eq!(relay.send_to(b"test", allowed).await, Err(Error::ErrFakeErr));
    assert!(relay.send_to(b"test", allowed).await? > 0);
    assert_eq!(observer.lock().await.requests, [blocked, allowed, allowed]);
    assert_eq!(relay.perm_map.addrs(), [SocketAddr::new(allowed.ip(), 0)]);
    Ok(())
}
