"""Keep native UDP punching independent of TCP NAT and WebRTC signaling."""

import re
from pathlib import Path


def apply(changes):
    apply_udp_probe(changes)
    keep_legacy_udp_punch(changes)
    prefer_direct_routes(changes)
    recognize_handshake_eof(changes)
    reuse_relay_standby(changes)
    move_relay_transport(changes)
    share_relay_deadline(changes)
    prepare_punch_fallback(changes)
    require_valid_signed_identity(changes)
    apply_mediator(changes)
    apply_port_forward_tuning(changes)
    apply_network_optimizations(changes)
    root = Path(__file__).resolve().parents[1] / "client/client"
    for name in ("relay.rs", "transport_race.rs"):
        changes["src/client/" + name] = (root / name).read_text(encoding="utf-8")


def apply_udp_probe(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("        let (stop_udp_tx, stop_udp_rx) = oneshot::channel::<()>();")
    end = text.index("        // Under force-relay a direct IPv6 path", start)
    changes.replace(
        path, text[start:end],
        """        let udp = if crate::get_udp_punch_enabled() && !interface.is_force_relay() {
            start_udp_probe(&rendezvous_server).await
        } else {
            (None, None)
        };
        let has_udp_probe = udp.0.is_some();
""",
    )
    text = changes[path]
    start = text.index("    /// UDP-NAT-test wait when the TCP clock is implausible")
    end = text.index("    /// Delay before re-sending an ICE candidate", start)
    changes.replace(
        path, text[start:end],
        """    // The same UDP result is needed regardless of authentication or WebRTC. Wait only
    // for the time left after the signaling handshake and continue immediately on a reply.
    const UDP_NAT_TEST_GRACE: Duration = Duration::from_millis(400);

""",
    )
    text = changes[path]
    start = text.index("        // A token or switch code has always taken this socket")
    end = text.index("        let mut msg_out = RendezvousMessage::new();", start)
    changes.replace(
        path, text[start:end],
        """        let udp_nat_port = collect_udp_probe(udp.1.take(), udp_nat_deadline).await;
""",
    )
    for old, new in (
        ("            udp.clone(),\n            Some(stop_udp_tx),", "            udp,"),
        ("(udp.0.is_none() && !has_webrtc_offerer)", "(!has_udp_probe && !has_webrtc_offerer)"),
        ("            (None, None),\n            None,\n            None,\n            None,",
         "            (None, None),\n            None,\n            None,"),
        ("        mut udp: (Option<Arc<UdpSocket>>, Option<Arc<Mutex<u16>>>),\n"
         "        stop_udp_tx: Option<oneshot::Sender<()>>,",
         "        mut udp: (Option<Arc<UdpSocket>>, Option<oneshot::Receiver<u16>>),"),
        ("        let udp_nat_wait_from = Instant::now();",
         "        let udp_nat_deadline = tokio::time::Instant::now() + Self::UDP_NAT_TEST_GRACE;"),
        ("                if socket.is_ok() {\n                    rendezvous_server = server;",
         "                if socket.is_ok() {\n"
         "                    // A mapping probed against the failed server may not reach the peer.\n"
         "                    if udp.0.is_some() {\n"
         "                        drop(udp.1.take());\n"
         "                        udp = start_udp_probe(&server).await;\n"
         "                    }\n"
         "                    rendezvous_server = server;"),
        ("        let udp_nat_port = udp.1.map(|x| *x.lock().unwrap()).unwrap_or(0);\n", ""),
        (
            "async fn test_udp_uat(\n    udp_socket: Arc<UdpSocket>,\n    server_addr: SocketAddr,\n"
            "    udp_port: Arc<Mutex<u16>>,\n    mut stop_udp_rx: oneshot::Receiver<()>,",
            """async fn start_udp_probe(server: &str) -> (Option<Arc<UdpSocket>>, Option<oneshot::Receiver<u16>>) {
    let Ok((socket, addr)) = new_direct_udp_for_unverified(server).await else {
        return (None, None);
    };
    let (port_tx, port_rx) = oneshot::channel();
    let probe_socket = socket.clone();
    tokio::spawn(async move {
        allow_err!(test_udp_uat(probe_socket, addr, port_tx).await);
    });
    (Some(socket), Some(port_rx))
}

async fn collect_udp_probe(
    probe: Option<oneshot::Receiver<u16>>,
    deadline: tokio::time::Instant,
) -> u16 {
    let Some(probe) = probe else { return 0 };
    tokio::time::timeout_at(deadline, probe)
        .await
        .ok()
        .and_then(Result::ok)
        .unwrap_or_default()
}

async fn test_udp_uat(
    udp_socket: Arc<UdpSocket>,
    server_addr: SocketAddr,
    mut port_tx: oneshot::Sender<u16>,""",
        ),
        ("    loop {\n        tokio::select! {\n            _ = &mut stop_udp_rx => {\n"
         '                log::debug!("UDP NAT test received stop signal after {} packets", packets_sent);\n'
         "                break;",
         "    let final_port = loop {\n        tokio::select! {\n            _ = port_tx.closed() => {\n"
         "                break 0;"),
        ("                                    *udp_port.lock().unwrap() = response.port as u16;\n"
         "                                    break;",
         "                                    if let Ok(port) = u16::try_from(response.port) {\n"
         "                                        if port != 0 {\n"
         "                                            break port;\n"
         "                                        }\n"
         "                                    }"),
        ("            res = udp_socket.recv(&mut buf[..]) => {\n"
         "                match res {\n                    Ok(n) => {",
         "            res = udp_socket.recv_from(&mut buf) => {\n"
         "                match res {\n                    Ok((n, source)) if source == server_addr => {"),
        ("                    Err(e) => {\n                        // Same ICMP-driven errors as punch_udp sees.",
         "                    Ok(_) => {}\n"
         "                    Err(e) => {\n                        // Same ICMP-driven errors as punch_udp sees."),
        ("    }\n\n    let final_port = *udp_port.lock().unwrap();",
         "    };\n    let _ = port_tx.send(final_port);"),
    ):
        changes.replace(path, old, new)
    changes.replace(
        path,
        "async fn test_udp_uat(\n",
        """#[cfg(test)]
mod native_udp_probe_tests {
    use super::*;

    #[tokio::test]
    async fn waits_for_the_native_udp_reply_from_the_selected_server_without_webrtc() {
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let other = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let server_addr = server.local_addr().unwrap();
        let client = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let (send, receive) = oneshot::channel();
        let responder = async {
            let mut bytes = [0; 256];
            let (size, addr) = server.recv_from(&mut bytes).await.unwrap();
            assert!(RendezvousMessage::parse_from_bytes(&bytes[..size]).unwrap().has_test_nat_request());
            let mut response = RendezvousMessage::new();
            response.set_test_nat_response(hbb_common::rendezvous_proto::TestNatResponse {
                port: if addr.port() == 1 { 2 } else { 1 },
                ..Default::default()
            });
            other.send_to(&response.write_to_bytes().unwrap(), addr).await.unwrap();
            tokio::time::sleep(Duration::from_millis(25)).await;
            response.mut_test_nat_response().port = i32::from(addr.port());
            server.send_to(&response.write_to_bytes().unwrap(), addr).await.unwrap();
        };
        let deadline = tokio::time::Instant::now() + Client::UDP_NAT_TEST_GRACE;
        let (result, _, port) = tokio::join!(
            test_udp_uat(client.clone(), server_addr, send),
            responder,
            collect_udp_probe(Some(receive), deadline),
        );
        result.unwrap();
        assert_eq!(port, client.local_addr().unwrap().port());
    }

    #[tokio::test]
    async fn missing_udp_reply_expires_and_cancels_the_probe() {
        let (mut send, receive) = oneshot::channel();
        let deadline = tokio::time::Instant::now() + Duration::from_millis(10);
        assert_eq!(collect_udp_probe(Some(receive), deadline).await, 0);
        send.closed().await;
    }
}

async fn test_udp_uat(
""",
    )


def keep_legacy_udp_punch(changes):
    path = "src/common.rs"
    changes.replace(path, "    // `connect` does not flush the receive queue, so the NAT test's extra replies are still in it.\n"
                          "    while socket.try_recv(&mut data).is_ok() {}\n",
                    "    let peer = socket.peer_addr()?;\n")
    changes.replace(path, "            res = socket.recv(&mut data) => match res {",
                    "            res = socket.recv_from(&mut data) => match res {")
    changes.replace(path, "                Ok(n) => {\n                    let ack = punch_tid",
                    """                Ok((_, source)) if source != peer => {}
                Ok((0, _)) if !listen => {
                    // Older peers prime their connected socket with empty datagrams.
                    // Only the connector can advance to KCP before receiving its first frame.
                    log::debug!("UDP punch received a legacy peer probe in {:?}", tm.elapsed());
                    return Ok(None);
                }
                Ok((n, _)) => {
                    let ack = punch_tid""")
    deadline_comment = "    // The deadline must hold against a peer that keeps the receive side ready."
    changes.replace(path, deadline_comment,
                    """    #[tokio::test]
    async fn test_udp_punch_preserves_a_queued_legacy_peer_probe() {
        let client = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        client.connect(peer.local_addr().unwrap()).await.unwrap();
        peer.send_to(&[], client.local_addr().unwrap()).await.unwrap();
        client.readable().await.unwrap();
        let result = tokio::time::timeout(Duration::from_millis(250), punch_udp(client, false))
            .await
            .unwrap()
            .unwrap();
        assert!(result.is_none());
    }

""" + deadline_comment)
    changes.replace(path, "        // Empty datagrams answer no probe and match no return branch, so they only feed the loop.",
                    "        // Empty datagrams do not carry the first KCP frame the listener needs.")
    changes.replace(path, "        let res = punch_udp(Arc::new(a), false).await;",
                    "        let res = punch_udp(Arc::new(a), true).await;")


def prefer_direct_routes(changes):
    path = "src/client.rs"
    changes.replace(path, """    /// Default relay fallback delay: how long an already-established relay result is held back
    /// while a WebRTC attempt is still in flight, and the floor for a punch-path WebRTC attempt""",
                    """    /// Hold an established relay briefly while a direct attempt is still in flight.
    /// The same value is the floor for a punch-path WebRTC attempt""")
    changes.replace(path, """    /// relay.
    fn relay_fallback_delay_ms() -> u64 {""", """    /// relay. Keep manually edited values in the settings field's supported range.
    fn relay_fallback_delay_ms() -> u64 {""")
    changes.replace(path, "=> (secs * 1000.0).round() as u64,",
                    "=> (secs.clamp(0.1, 99.9) * 1000.0).round() as u64,")
    text = changes[path]
    start = text.index("        if has_webrtc_offerer {\n            return race_transports_prefer_webrtc(")
    end = text.index("\n    }\n\n    fn is_expected_webrtc_ice_candidate", start)
    changes.replace(path, text[start:end], """        race_transports_prefer_webrtc(
            preferred_fut,
            vec![fallback_fut],
            Self::relay_fallback_delay_ms(),
            |result| result.0 .1,
        )
        .await""")
    changes.replace(path, """                        } else {
                            // Run all connection attempts concurrently, take the first success.
                            select_ok(connect_futures).await.map(|r| r.0)
                        };""", """                        } else if connect_futures.len() > 1 {
                            // The IPv6 attempt was added before the relay. Give it the same
                            // direct-connection preference even when there is no WebRTC offer.
                            let ipv6 = connect_futures.remove(0);
                            race_transports_prefer_webrtc(
                                ipv6,
                                connect_futures,
                                Self::relay_fallback_delay_ms(),
                                |result| result.3,
                            )
                            .await
                        } else {
                            select_ok(connect_futures).await.map(|r| r.0)
                        };""")
    changes.replace(path, "        if allow_tcp_punch {\n            let fut = connect_tcp_local", """        // A server-provided LAN endpoint uses ordinary TCP, independent of WAN TCP punching.
        if !interface.is_force_relay() && (is_local || allow_tcp_punch) {
            let fut = connect_tcp_local""")
    changes.replace(path, "        let mut is_local = false;\n        let mut feedback = 0;",
                    "        let mut is_local = false;\n        let mut peer_is_udp = false;\n        let mut feedback = 0;")
    changes.replace(path, "                            is_local = ph.is_local();",
                    "                            is_local = ph.is_local();\n                            peer_is_udp = ph.is_udp;")
    changes.replace(path, "                allow_tcp_punch,\n                &punch_type,",
                    "                allow_tcp_punch && !peer_is_udp,\n                &punch_type,")
    changes.replace(path, """                                    allow_err!(s.connect(peer_addr).await);
                                    udp.0 = Some(s);""", """                                    match s.connect(peer_addr).await {
                                        Ok(()) => udp.0 = Some(s),
                                        Err(err) => log::debug!("UDP peer socket setup failed: {err}"),
                                    }""")
    changes.replace(path, """                                        allow_err!(s.connect(addr).await);
                                        ipv6.0 = Some(s);""", """                                        match s.connect(addr).await {
                                            Ok(()) => ipv6.0 = Some(s),
                                            Err(err) => log::debug!("IPv6 peer socket setup failed: {err}"),
                                        }""")
    text = changes[path]
    start = text.index("/// Race WebRTC against the other transports")
    end = text.index("// A peer decides how many ICE candidates", start)
    changes.replace(path, text[start:end], """mod transport_race;
use transport_race::race_transports_prefer_direct;

""")
    changes.replace(path, "futures::future::{select_ok, BoxFuture, FutureExt}", "futures::future::{select_ok, FutureExt}")
    changes[path] = changes[path].replace("race_transports_prefer_webrtc", "race_transports_prefer_direct")


def recognize_handshake_eof(changes):
    path = "src/client.rs"
    changes.replace(path, '                bail!("Reset by the peer");',
                    '                return Err(std::io::Error::new(\n'
                    '                    std::io::ErrorKind::UnexpectedEof, "Reset by the peer",\n'
                    '                ).into());')


def reuse_relay_standby(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("                        let race_result = if let Some(webrtc)")
    end = text.index("                        if let Some(stop) = webrtc_bridge_stop", start)
    current = text[start:end]
    races = current.replace(
        "race_transports_prefer_direct(",
        "transport_race::race_transports_with_standby(",
    ).replace(
        "select_ok(connect_futures).await.map(|r| r.0)",
        "select_ok(connect_futures).await.map(|r| (r.0, None))",
    )
    changes.replace(path, current, races)
    changes.replace(
        path,
        "                        let (mut conn, kcp, mut typ, mut direct) = race_result?;",
        "                        let ((mut conn, mut kcp, mut typ, mut direct), standby) = race_result?;",
    )
    text = changes[path]
    start = text.index('                            Err(e) if typ == "WebRTC" => {')
    end = text.index("                            Err(e) => return Err(e),", start)
    changes.replace(path, text[start:end], """                            Err(e) if transport_race::can_retry_handshake(typ, direct, &e) => {
                                log::warn!("{typ} secure handshake failed ({e}), falling back to relay");
                                if typ == "WebRTC" {
                                    drop(webrtc_guard.take());
                                }
                                drop(conn);
                                if let Some(((standby_conn, standby_kcp, standby_typ, _), pk)) =
                                    Self::secure_relay_standby(&peer, &signed_id_pk, &key, standby).await
                                {
                                    conn = standby_conn;
                                    kcp = standby_kcp;
                                    typ = standby_typ;
                                    direct = false;
                                    pk
                                } else {
                                    drop(webrtc_guard.take());
                                    let mut relay_conn = Self::request_relay(
                                        &peer, relay_server_rr, &rendezvous_server,
                                        !signed_id_pk.is_empty(), &key, &token, conn_type,
                                        &interface.get_switch_code(), None,
                                    ).await.map_err(|relay_e| transport_race::fallback_failure(typ, e, relay_e))?;
                                    let pk = Self::secure_connection(
                                        &peer, signed_id_pk, &key, &mut relay_conn,
                                    ).await?;
                                    conn = relay_conn;
                                    kcp = None;
                                    typ = if use_ws() { "WebSocket" } else { "Relay" };
                                    direct = false;
                                    pk
                                }
                            }
""")


def move_relay_transport(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("    /// Request a relay connection to the server.")
    end = text.index("    #[inline]\n    #[cfg(feature = \"flutter\")]", start)
    changes.replace(path, text[start:end], "")
    changes.replace(path, "mod transport_race;", "mod relay;\nmod transport_race;")


def share_relay_deadline(changes):
    path = "src/client.rs"
    changes.replace(
        path,
        "use transport_race::race_transports_prefer_direct;",
        "#[cfg(test)]\nuse transport_race::race_transports_prefer_direct;",
    )
    changes.replace(
        path,
        "        match Self::_start(peer, key, token, conn_type, interface.clone()).await {",
        "        let relay_deadline = transport_race::ConnectionDeadline::new(Self::relay_fallback_delay_ms());\n"
        "        match relay_deadline\n"
        "            .run(Self::_start(peer, key, token, conn_type, interface.clone(), &relay_deadline))\n"
        "            .await\n"
        "        {",
    )
    changes.replace(
        path,
        "    async fn _start(\n        peer: &str,\n        key: &str,\n        token: &str,\n        conn_type: ConnType,\n        interface: impl Interface,",
        "    async fn _start(\n        peer: &str,\n        key: &str,\n        token: &str,\n        conn_type: ConnType,\n        interface: impl Interface,\n"
        "        relay_deadline: &transport_race::ConnectionDeadline,",
    )
    source, count = re.subn(
        r"            contained,\n        \)",
        "            contained,\n            relay_deadline,\n        )",
        changes[path],
    )
    if count != 2:
        raise ValueError("Rendezvous attempt signatures changed")
    changes[path] = source
    changes.replace(
        path,
        "        contained: bool,\n    ) -> ResultType<(",
        "        contained: bool,\n        relay_deadline: &transport_race::ConnectionDeadline,\n    ) -> ResultType<(",
    )
    changes.replace(
        path,
        "                &punch_type,\n            )",
        "                &punch_type,\n                relay_deadline,\n            )",
    )
    changes.replace(
        path,
        "        punch_type: &str,\n    ) -> ResultType<(",
        "        punch_type: &str,\n        relay_deadline: &transport_race::ConnectionDeadline,\n    ) -> ResultType<(",
    )
    pattern = (
        r"(race_transports_prefer_direct|transport_race::race_transports_with_standby)"
        r"\(\n([^\n]+\n[^\n]+\n)\s*Self::relay_fallback_delay_ms\(\),\n"
    )

    def race(match):
        method = "race" if match[1] == "race_transports_prefer_direct" else "race_with_standby"
        return "relay_deadline." + method + "(\n" + match[2]

    source, count = re.subn(pattern, race, changes[path])
    if count != 4:
        raise ValueError("Transport race call sites changed")
    changes[path] = source


def prepare_punch_fallback(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("        let direct_result = match (webrtc_fut, direct_futures.is_empty())")
    end = text.index('        if typ == "WebRTC" {\n            // WebRTC through a TURN server', start)
    changes.replace(path, text[start:end], """        use relay::DirectFailure;
        let force_relay = interface.is_force_relay();
        let try_direct = async {
            let result = match (webrtc_fut, direct_futures.is_empty()) {
                (Some(webrtc_fut), false) => relay_deadline
                    .race(webrtc_fut, direct_futures, |r| r.3)
                    .await,
                (Some(webrtc_fut), true) => webrtc_fut.await,
                (None, false) => select_ok(direct_futures).await.map(|c| c.0),
                (None, true) => Err(anyhow!("No direct transport available")),
            };
            let (mut conn, kcp, typ, direct) = result.map_err(DirectFailure::Transport)?;
            if force_relay && typ != "WebRTC" {
                return Err(DirectFailure::Transport(anyhow!("Relay required")));
            }
            let pk = Self::secure_connection(peer_id, signed_id_pk.clone(), key, &mut conn)
                .await
                .map_err(|error| DirectFailure::Handshake(error, typ, direct))?;
            Ok((conn, direct, pk, kcp, typ))
        };
        let switch_code = interface.get_switch_code();
        let try_direct = async {
            if relay_server.is_empty() {
                try_direct.await
            } else {
                relay_deadline.before_relay_reserve(try_direct).await
                    .unwrap_or_else(|error| Err(DirectFailure::Transport(error.into())))
            }
        };
        tokio::pin!(try_direct);
        let (result, prepared_broker) = if relay_server.is_empty() {
            (Some(try_direct.as_mut().await), None)
        } else {
            let decision = async {
                // A role switch or an independent TCP punch keeps its original signaling order.
                if switch_code.is_empty() && !tcp_punch_allowed() {
                    relay_deadline.wait_for_direct(try_direct.as_mut()).await
                } else {
                    Some(try_direct.as_mut().await)
                }
            };
            Self::prepare_relay_broker_during(
                decision,
                rendezvous_server,
                key,
                !token.is_empty() || !switch_code.is_empty(),
                if switch_code.is_empty() && !tcp_punch_allowed() {
                    relay_deadline.window()
                } else {
                    Duration::from_millis(CONNECT_TIMEOUT)
                },
            ).await
        };
        let connect_relay = |prepared_broker| async {
            let mut conn = Self::request_relay(
                peer_id, relay_server.to_owned(), rendezvous_server,
                !signed_id_pk.is_empty(), key, token, conn_type, &switch_code, prepared_broker,
            ).await?;
            let pk = Self::secure_connection(peer_id, signed_id_pk.clone(), key, &mut conn).await?;
            Ok((conn, false, pk, None, "Relay"))
        };
        let (conn, mut direct, pk, kcp, typ) = match result {
            Some(Ok(result)) => result,
            None => relay_deadline.race_authorized(
                try_direct.as_mut().map(|result| result.map_err(DirectFailure::into_error)).boxed(),
                connect_relay(prepared_broker).boxed(),
                |result| result.1,
            ).await?,
            Some(Err(error)) => {
                let handshake = match error {
                    DirectFailure::Handshake(error, typ, direct) => {
                        if relay_server.is_empty()
                            || !transport_race::can_retry_handshake(typ, direct, &error)
                        {
                            interface.update_direct(Some(direct));
                            return Err(error);
                        }
                        log::warn!("{typ} secure handshake failed ({error}), falling back to relay");
                        Some((typ, error))
                    }
                    DirectFailure::Transport(error) => {
                        if relay_server.is_empty() {
                            return Err(error.context("Failed to make direct connection to remote desktop"));
                        }
                        log::debug!("Direct transport unavailable: {error}");
                        None
                    }
                };
                drop(webrtc_guard.take());
                interface.update_direct(Some(false));
                match (connect_relay(prepared_broker).await, handshake) {
                    (Ok(result), _) => result,
                    (Err(error), Some((typ, handshake))) =>
                        return Err(transport_race::fallback_failure(typ, handshake, error)),
                    (Err(error), None) if crate::geodomdesk_auth::requires_login(&error.to_string()) =>
                        return Err(error),
                    (Err(error), None) => return Err(error.context("Failed to connect via relay server")),
                }
            }
        };
        if let Some(stop) = webrtc_bridge_stop {
            let _ = stop.send(());
        }
        log::info!(
            "{:?} used to establish {typ} connection with {} punch",
            start.elapsed(), punch_type
        );
""")


def require_valid_signed_identity(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("        // A WebRTC channel is peer-authenticated", text.index("    async fn secure_connection("))
    end = text.index("                conn.send(&Message::new()).await?;", start)
    current = text[start:end]
    replacement = """        let is_webrtc = conn.is_webrtc();
        let require_valid_proof = is_webrtc || !key.is_empty();
        let sign_pk = if signed_id_pk.is_empty() {
            None
        } else {
            let verified = rs_pk.and_then(|root| decode_id_pk(&signed_id_pk, &root).ok())
                .filter(|(id, _)| id == peer_id)
                .map(|(_, pk)| sign::PublicKey(pk));
            if verified.is_none() {
                if !key.is_empty() {
                    bail!("Invalid signed peer identity from rendezvous server");
                }
                log::error!("Handshake failed: invalid public key from rendezvous server");
            }
            verified
        };
        let (sign_pk, option_pk) = match sign_pk {
            Some(pk) => {
                let option_pk = Some(pk.0.to_vec());
                (pk, option_pk)
            }
            None => {
                // The official keyless handshake allows a missing signature.
"""
    changes.replace(path, current, replacement)
    changes.replace(path, "                return Ok(option_pk);\n            }\n        };\n        match timeout(READ_TIMEOUT", "                return Ok(None);\n            }\n        };\n        match timeout(READ_TIMEOUT")
    for reason in (
        "WebRTC handshake id mismatch (possible MITM)",
        "WebRTC peer identity could not be verified (refusing unbound channel)",
        "WebRTC handshake received an unexpected message type",
        "WebRTC handshake received a malformed message",
    ):
        pattern = rf'if is_webrtc \{{\n(\s*)bail!\("{re.escape(reason)}"\);'
        text, count = re.subn(
            pattern, lambda match: "if require_valid_proof {\n" + match[1]
            + 'bail!("' + reason.replace("WebRTC ", "") + '");', changes[path],
        )
        if count != 1:
            raise ValueError("Peer handshake branch changed: " + reason)
        changes[path] = text


def apply_mediator(changes):
    path = "src/rendezvous_mediator.rs"
    changes.replace(
        path,
        "pub struct RendezvousMediator {\n    addr: TargetAddr<'static>,",
        "pub struct RendezvousMediator {\n    addr: TargetAddr<'static>,\n    udp_server_addr: Option<SocketAddr>,",
    )
    changes.replace(
        path,
        "        let mut rz = Self {\n            addr: addr.clone(),",
        "        let udp_server_addr = match &addr {\n"
        "            hbb_common::TargetAddr::Ip(ip) => Some(*ip),\n"
        "            _ => None,\n"
        "        };\n"
        "        let mut rz = Self {\n            addr: addr.clone(),\n            udp_server_addr,",
    )
    changes.replace(
        path,
        "                                        rz.addr = new_addr.clone();",
        "                                        rz.udp_server_addr = match &new_addr {\n"
        "                                            hbb_common::TargetAddr::Ip(ip) => Some(*ip),\n"
        "                                            _ => None,\n"
        "                                        };\n"
        "                                        rz.addr = new_addr.clone();",
    )
    changes.replace(
        path,
        "        let mut rz = Self {\n            addr: conn.local_addr().into_target_addr()?,",
        "        let mut rz = Self {\n            addr: conn.local_addr().into_target_addr()?,\n            udp_server_addr: None,",
    )
    # The retained 1.5.3 relay-policy test constructs another mediator.
    test_address = "            addr: host.into_target_addr().unwrap(),\n"
    if not 1 <= changes[path].count(test_address) <= 2:
        raise ValueError("Unexpected rendezvous test constructor")
    changes[path] = changes[path].replace(
        test_address, test_address + "            udp_server_addr: Some(host),\n"
    )
    changes.replace(
        path,
        "    async fn handle_punch_hole(&self, ph: PunchHole, server: ServerPtr) -> ResultType<()> {",
        """    fn should_relay_ipv4(
        forced: bool,
        udp_port: i32,
        tcp_symmetric: bool,
        tcp_disabled: bool,
    ) -> bool {
        // The rendezvous NAT test classifies TCP, not the independently observed UDP mapping.
        forced || (udp_port <= 0 && (tcp_symmetric || tcp_disabled))
    }

    fn direct_udp_target(udp_server_addr: Option<SocketAddr>, has_socks: bool) -> Option<SocketAddr> {
        if has_socks {
            None
        } else {
            udp_server_addr
        }
    }

    async fn handle_punch_hole(&self, ph: PunchHole, server: ServerPtr) -> ResultType<()> {""",
    )
    changes.replace(
        path,
        "// A symmetric NAT relays the legacy transports but deliberately not WebRTC:",
        "// A symmetric TCP NAT relays the legacy TCP leg but deliberately not WebRTC:",
    )
    changes.replace(
        path,
        """        let relay_v4 = ph.nat_type.enum_value() == Ok(NatType::SYMMETRIC)
            || Config::get_nat_type() == NatType::SYMMETRIC as i32
            || relay
            || (config::is_disable_tcp_listen() && ph.udp_port <= 0);""",
        """        let relay_v4 = Self::should_relay_ipv4(
            relay,
            ph.udp_port,
            ph.nat_type.enum_value() == Ok(NatType::SYMMETRIC)
                || Config::get_nat_type() == NatType::SYMMETRIC as i32,
            config::is_disable_tcp_listen(),
        );""",
    )
    changes.replace(
        path,
        "        let (socket, addr) = new_direct_udp_for(&self.host).await?;",
        """        let (socket, addr) = match Self::direct_udp_target(self.udp_server_addr, Config::get_socks().is_some()) {
            Some(addr) => {
                let local_addr = Config::get_any_listen_addr(addr.is_ipv4());
                let socket = hbb_common::tokio::net::UdpSocket::bind(local_addr).await?;
                (Arc::new(socket), addr)
            }
            None => new_direct_udp_for(&self.host).await?,
        };""",
    )
    changes.replace(
        path,
        """        let socket_cloned = socket.clone();
        tokio::spawn(async move {
            for _ in 0..2 {
                let tm = (hbb_common::time_based_rand() % 20 + 10) as f32 / 1000.;
                hbb_common::sleep(tm).await;
                socket.send_to(&data, addr).await.ok();
            }
        });
        udp_nat_listen(
            socket_cloned.clone(),""",
        """        // Send one quick retransmit before udp_nat_listen connects this socket to peer_addr
        // (once connected, send_to to the rendezvous server fails with EISCONN).
        hbb_common::sleep(0.01).await;
        socket.send_to(&data, addr).await.ok();
        udp_nat_listen(
            socket,""",
    )
    changes.replace(
        path,
        "    #[tokio::test]\n    async fn manual_restart_interrupts_retry_wait() {",
        """    #[test]
    fn udp_mapping_is_not_disabled_by_tcp_nat_test() {
        assert!(!RendezvousMediator::should_relay_ipv4(false, 9080, true, false));
        assert!(!RendezvousMediator::should_relay_ipv4(false, 9080, true, true));
        assert!(RendezvousMediator::should_relay_ipv4(true, 9080, false, false));
        assert!(RendezvousMediator::should_relay_ipv4(false, 0, true, false));
        assert!(RendezvousMediator::should_relay_ipv4(false, 0, false, true));
        assert!(!RendezvousMediator::should_relay_ipv4(false, 0, false, false));
    }

    #[test]
    fn tcp_mediator_does_not_reuse_local_socket_addr_as_udp_server() {
        let addr: std::net::SocketAddr = "198.51.100.2:21128".parse().unwrap();
        assert_eq!(RendezvousMediator::direct_udp_target(Some(addr), false), Some(addr));
        assert_eq!(RendezvousMediator::direct_udp_target(Some(addr), true), None);
        assert_eq!(RendezvousMediator::direct_udp_target(None, false), None);
    }

    #[tokio::test]
    async fn manual_restart_interrupts_retry_wait() {""",
    )


def apply_port_forward_tuning(changes):
    changes.replace(
        "src/port_forward.rs",
        "            Ok((forward, addr)) = listener.accept() => {\n"
        '                log::info!("new connection from {:?}", addr);',
        "            Ok((forward, addr)) = listener.accept() => {\n"
        "                forward.set_nodelay(true).ok();\n"
        '                log::info!("new connection from {:?}", addr);',
    )
    changes.replace(
        "src/server/connection.rs",
        '        (format!("{}:{}", pf.host, pf.port), is_rdp)',
        "        let host = if pf.host.contains(':') && !pf.host.starts_with('[') {\n"
        '            format!("[{}]", pf.host)\n'
        "        } else {\n"
        "            pf.host.clone()\n"
        "        };\n"
        '        (format!("{}:{}", host, pf.port), is_rdp)',
    )
    changes.replace(
        "src/server/connection.rs",
        "            Ok(Ok(sock)) => {\n"
        "                self.port_forward_socket = Some(Framed::new(sock, BytesCodec::new()));",
        "            Ok(Ok(sock)) => {\n"
        "                sock.set_nodelay(true).ok();\n"
        "                self.port_forward_socket = Some(Framed::new(sock, BytesCodec::new()));",
    )


def apply_network_optimizations(changes):
    # Skip redundant service IPC when server coordinates are compiled in.
    for fn_sig, ret_expr in (
        (
            "pub async fn get_rendezvous_server(ms_timeout: u64) -> (String, Vec<String>) {\n",
            "(\n            Config::get_rendezvous_server(),\n            Config::get_rendezvous_servers(),\n        )",
        ),
        (
            "pub async fn get_rendezvous_servers(ms_timeout: u64) -> Vec<String> {\n",
            "Config::get_rendezvous_servers()",
        ),
    ):
        changes.replace(
            "src/ipc.rs",
            fn_sig,
            fn_sig
            + "    if config::OVERWRITE_SETTINGS\n"
            + "        .read()\n"
            + "        .unwrap()\n"
            + '        .get("custom-rendezvous-server")\n'
            + "        .is_some_and(|v| !v.is_empty())\n"
            + "    {\n"
            + f"        return {ret_expr};\n"
            + "    }\n",
        )

    # Clear stale cached IPv6 address and skip external STUN probes if local IPv6 bind fails.
    changes.replace(
        "src/common.rs",
        """        Err(e) => {
            log::warn!("Failed to bind IPv6 socket: {}", e);
        }""",
        """        Err(e) => {
            PUBLIC_IPV6_ADDR.lock().unwrap().0 = None;
            log::debug!("Failed to bind IPv6 socket: {}", e);
            return None;
        }""",
    )

    # Parallelize client startup probes (UDP probe, IPv6 check/bind, WebRTC offerer, primary TCP dial, NAT fetch).
    client_rs = "src/client.rs"
    text = changes[client_rs]
    start = text.index("        // Same relay gate as the v6 socket below:")
    end = text.index("        let fut = Self::_start_inner(", start)
    changes.replace(
        client_rs,
        text[start:end],
        """        let allow_p2p_v6 = crate::get_ipv6_punch_enabled() && !interface.is_force_relay();
        let allow_udp_probe = crate::get_udp_punch_enabled() && !interface.is_force_relay();
        let allow_webrtc = Self::should_create_webrtc_offerer(&interface);
        let probe_started_at = tokio::time::Instant::now();
        let (udp, ipv6, webrtc_res, primary_socket, my_nat_type) = tokio::join!(
            async {
                if allow_udp_probe {
                    start_udp_probe(&rendezvous_server).await
                } else {
                    (None, None)
                }
            },
            async {
                if allow_p2p_v6 {
                    crate::test_ipv6().await;
                    crate::get_ipv6_socket().await
                } else {
                    None
                }
            },
            async {
                if allow_webrtc {
                    Some(WebRTCStream::new("", interface.is_policy_relay(), CONNECT_TIMEOUT).await)
                } else {
                    None
                }
            },
            connect_tcp(&*rendezvous_server, CONNECT_TIMEOUT),
            crate::get_nat_type(100),
        );
        let has_udp_probe = udp.0.is_some();
        let webrtc_offerer = match webrtc_res {
            Some(Ok(stream)) => Some(stream),
            Some(Err(err)) => {
                log::warn!("webrtc offerer setup failed: {}", err);
                None
            }
            None => None,
        };
        let has_webrtc_offerer = webrtc_offerer.is_some();
""",
    )
    changes.replace(
        client_rs,
        """            rendezvous_server.clone(),
            servers.clone(),
            contained,
            relay_deadline,
        );""",
        """            rendezvous_server.clone(),
            servers.clone(),
            contained,
            relay_deadline,
            probe_started_at,
            Some(primary_socket),
            my_nat_type,
        );""",
    )
    changes.replace(
        client_rs,
        """            rendezvous_server,
            servers,
            contained,
            relay_deadline,
        )""",
        """            rendezvous_server,
            servers,
            contained,
            relay_deadline,
            probe_started_at,
            None,
            my_nat_type,
        )""",
    )
    changes.replace(
        client_rs,
        "        contained: bool,\n        relay_deadline: &transport_race::ConnectionDeadline,\n    ) -> ResultType<(",
        "        contained: bool,\n        relay_deadline: &transport_race::ConnectionDeadline,\n"
        "        mut probe_started_at: tokio::time::Instant,\n"
        "        preconnected: Option<ResultType<Stream>>,\n"
        "        my_nat_type: i32,\n    ) -> ResultType<(",
    )
    changes.replace(
        client_rs,
        "        let mut socket = connect_tcp(&*rendezvous_server, CONNECT_TIMEOUT).await;",
        "        let mut socket = match preconnected {\n"
        "            Some(res) => res,\n"
        "            None => connect_tcp(&*rendezvous_server, CONNECT_TIMEOUT).await,\n"
        "        };",
    )
    changes.replace(
        client_rs,
        "                    if udp.0.is_some() {\n                        drop(udp.1.take());",
        "                    if udp.0.is_some() {\n                        drop(udp.1.take());\n                        probe_started_at = tokio::time::Instant::now();",
    )
    changes.replace(client_rs, "        let my_nat_type = crate::get_nat_type(100).await;\n", "")
    changes.replace(
        client_rs,
        "        let udp_nat_deadline = tokio::time::Instant::now() + Self::UDP_NAT_TEST_GRACE;",
        "        let udp_nat_deadline = probe_started_at + Self::UDP_NAT_TEST_GRACE;",
    )
    changes.replace(
        client_rs,
        "                    if my_nat_type == NatType::UNKNOWN_NAT as i32 {\n"
        "                        my_nat_type = crate::get_nat_type(100).await;\n"
        "                    }",
        "                    if my_nat_type == NatType::UNKNOWN_NAT as i32 {\n"
        "                        my_nat_type = Config::get_nat_type();\n"
        "                    }",
    )

    # Raise KCP UDP socket buffers to 2 MiB (without shrinking larger OS defaults).
    changes["libs/hbb_common/src/lib.rs"] = (
        changes["libs/hbb_common/src/lib.rs"].rstrip()
        + """

#[cfg(any(unix, windows))]
pub fn tune_udp_socket_buffers(socket: &tokio::net::UdpSocket, target_bytes: usize) {
    #[cfg(unix)]
    let raw = {
        use std::os::unix::io::{AsRawFd, FromRawFd};
        unsafe { socket2::Socket::from_raw_fd(socket.as_raw_fd()) }
    };
    #[cfg(windows)]
    let raw = {
        use std::os::windows::io::{AsRawSocket, FromRawSocket};
        unsafe { socket2::Socket::from_raw_socket(socket.as_raw_socket()) }
    };
    let sock = std::mem::ManuallyDrop::new(raw);
    if sock.recv_buffer_size().is_ok_and(|cur| cur < target_bytes) {
        let _ = sock.set_recv_buffer_size(target_bytes);
    }
    if sock.send_buffer_size().is_ok_and(|cur| cur < target_bytes) {
        let _ = sock.set_send_buffer_size(target_bytes);
    }
}
"""
    )
    changes.replace(
        "src/kcp_stream.rs",
        "        let mut endpoint = KcpEndpoint::new();\n        Self::apply_kcp_config(&mut endpoint);\n        endpoint.run().await;\n\n        let (input, output) = (\n            endpoint.input_sender(),\n            endpoint\n                .output_receiver()\n                .ok_or_else(|| anyhow::anyhow!(\"Failed to get output receiver\"))?,\n        );\n        let (stop_sender, stop_receiver) = oneshot::channel();\n        if let Some(packet) =",
        "        hbb_common::tune_udp_socket_buffers(&udp_socket, 2 * 1024 * 1024);\n"
        "        let mut endpoint = KcpEndpoint::new();\n        Self::apply_kcp_config(&mut endpoint);\n        endpoint.run().await;\n\n        let (input, output) = (\n            endpoint.input_sender(),\n            endpoint\n                .output_receiver()\n                .ok_or_else(|| anyhow::anyhow!(\"Failed to get output receiver\"))?,\n        );\n        let (stop_sender, stop_receiver) = oneshot::channel();\n        if let Some(packet) =",
    )
    changes.replace(
        "src/kcp_stream.rs",
        "        let mut endpoint = KcpEndpoint::new();\n        Self::apply_kcp_config(&mut endpoint);\n        endpoint.run().await;\n\n        let (input, output) = (\n            endpoint.input_sender(),\n            endpoint\n                .output_receiver()\n                .ok_or_else(|| anyhow::anyhow!(\"Failed to get output receiver\"))?,\n        );\n        let (stop_sender, stop_receiver) = oneshot::channel();\n        Self::kcp_io",
        "        hbb_common::tune_udp_socket_buffers(&udp_socket, 2 * 1024 * 1024);\n"
        "        let mut endpoint = KcpEndpoint::new();\n        Self::apply_kcp_config(&mut endpoint);\n        endpoint.run().await;\n\n        let (input, output) = (\n            endpoint.input_sender(),\n            endpoint\n                .output_receiver()\n                .ok_or_else(|| anyhow::anyhow!(\"Failed to get output receiver\"))?,\n        );\n        let (stop_sender, stop_receiver) = oneshot::channel();\n        Self::kcp_io",
    )

    # Enable TCP_NODELAY on direct WebSocket connections.
    changes.replace(
        "libs/hbb_common/src/websocket.rs",
        "            let disable_nagle = false;",
        "            let disable_nagle = true;",
    )

    # Upgrade multiplexed port forwarding to a 1 MiB receive window while keeping
    # UPDATE_THRESHOLD at 128 KiB so unmodified 1.5.1 peers (256 KiB cap) never deadlock.
    mux = "src/port_forward_mux.rs"
    changes.replace(
        mux,
        """pub const CHANNEL_WINDOW: u32 = 256 * 1024;
pub const MAX_FRAME: usize = 64 * 1024;
/// Cap on one framed packet once the tunnel is up: a `MAX_FRAME` data frame,
/// its protobuf envelope and the 16-byte MAC fit with room to spare. The codec
/// otherwise takes a header declaring up to 1 GiB, and the channel window is
/// only checked once the whole packet has arrived.
pub const MAX_PACKET: usize = 2 * MAX_FRAME;
pub const UPDATE_THRESHOLD: u32 = CHANNEL_WINDOW / 2;""",
        """pub const CHANNEL_WINDOW: u32 = 1024 * 1024;
pub const MAX_FRAME: usize = 56 * 1024;
/// Cap on one framed packet once the tunnel is up: a `MAX_FRAME` data frame,
/// its protobuf envelope and the 16-byte MAC fit with room to spare. The codec
/// otherwise takes a header declaring up to 1 GiB, and the channel window is
/// only checked once the whole packet has arrived.
pub const MAX_PACKET: usize = 128 * 1024;
/// Pin at 128 KiB (not CHANNEL_WINDOW / 2) so unmodified 1.5.1 peers with a
/// 256 KiB MAX_SEND_CREDIT cap receive timely window updates and never stall.
pub const UPDATE_THRESHOLD: u32 = 128 * 1024;""",
    )
    changes.replace(
        mux,
        "pub use tunnel::{Claim, Tunnel};",
        "pub use tunnel::{Claim, Tunnel, TunnelHandle};",
    )
    changes.replace(
        mux,
        "        #[cfg(test)]\n        pub fn live_channels(&self) -> usize {",
        "        pub fn live_channels(&self) -> usize {",
    )
    changes.replace(
        mux,
        "    #[test]\n    fn raise_initial_rebases_credit_from_initial_window() {",
        """    #[test]
    fn recv_window_updates_within_legacy_256k_credit_cap() {
        let mut recv = RecvWindow::new(CHANNEL_WINDOW);
        let mut peer_credit: u32 = 256 * 1024;
        let mut total_sent = 0usize;
        let frame = MAX_FRAME;
        while total_sent < 1024 * 1024 {
            let cost = charge(frame);
            assert!(peer_credit >= cost, "stalled at {total_sent} bytes with {peer_credit} credit");
            peer_credit -= cost;
            assert!(recv.accept(frame));
            if let Some(add) = recv.drained(frame) {
                peer_credit = peer_credit.saturating_add(add).min(256 * 1024);
            }
            total_sent += frame;
        }
    }

    #[test]
    fn raise_initial_rebases_credit_from_initial_window() {""",
    )

    smux = "src/server/port_forward_mux.rs"
    changes.replace(
        smux,
        "pub struct PortForwardMux {\n    channels: HashMap<i32, Entry>,\n    tx: Sender,\n    login_target: String,",
        "pub struct PortForwardMux {\n    channels: HashMap<i32, Entry>,\n    sink: FrameSink,\n    login_target: String,",
    )
    changes.replace(
        smux,
        """    pub fn new(tx: Sender, login_target: String) -> Self {
        Self {
            channels: HashMap::new(),
            tx,
            login_target,
            teardown: watch::channel(false).0,
        }
    }""",
        """    pub fn with_sink(sink: FrameSink, login_target: String) -> Self {
        Self {
            channels: HashMap::new(),
            sink,
            login_target,
            teardown: watch::channel(false).0,
        }
    }

    pub fn new(tx: Sender, login_target: String) -> Self {
        Self::with_sink(FrameSink::Direct(tx), login_target)
    }""",
    )
    changes.replace(
        smux,
        "            FrameSink::Direct(self.tx.clone()),",
        "            self.sink.clone(),",
    )
    changes.replace(
        smux,
        """    fn reply(&self, msg: Message) {
        self.tx
            .send((tokio::time::Instant::now(), Arc::new(msg)))
            .ok();
    }""",
        """    fn reply(&self, msg: Message) {
        self.sink.send_control(msg).ok();
    }""",
    )
    changes.replace(
        smux,
        "                    Ok(Ok(s)) => break s,",
        "                    Ok(Ok(s)) => { s.set_nodelay(true).ok(); break s; },",
    )

    sconn = "src/server/connection.rs"
    changes.replace(
        sconn,
        "    port_forward_mux: Option<super::port_forward_mux::PortForwardMux>,\n",
        "    port_forward_mux: Option<super::port_forward_mux::PortForwardMux>,\n"
        "    mux_ctrl_rx: Option<mpsc::UnboundedReceiver<Message>>,\n"
        "    mux_data_rx: Option<mpsc::Receiver<Message>>,\n",
    )
    changes.replace(
        sconn,
        "            port_forward_mux: None,\n",
        "            port_forward_mux: None,\n"
        "            mux_ctrl_rx: None,\n"
        "            mux_data_rx: None,\n",
    )
    changes.replace(
        sconn,
        """                Some((instant, value)) = rx.recv() => {""",
        """                maybe_msg = async {
                    match conn.mux_ctrl_rx.as_mut() {
                        Some(rx) => rx.recv().await,
                        None => std::future::pending().await,
                    }
                } => {
                    match maybe_msg {
                        Some(msg) => {
                            if let Err(err) = conn.stream.send(&msg).await {
                                conn.on_close(&err.to_string(), false).await;
                                break;
                            }
                        }
                        None => conn.mux_ctrl_rx = None,
                    }
                },
                maybe_msg = async {
                    match conn.mux_data_rx.as_mut() {
                        Some(rx) => rx.recv().await,
                        None => std::future::pending().await,
                    }
                } => {
                    let mut send_failed = false;
                    while let Some(ctrl_rx) = conn.mux_ctrl_rx.as_mut() {
                        match ctrl_rx.try_recv() {
                            Ok(ctrl_msg) => {
                                if let Err(err) = conn.stream.send(&ctrl_msg).await {
                                    conn.on_close(&err.to_string(), false).await;
                                    send_failed = true;
                                    break;
                                }
                            }
                            Err(_) => break,
                        }
                    }
                    if send_failed {
                        break;
                    }
                    match maybe_msg {
                        Some(msg) => {
                            if let Err(err) = conn.stream.send(&msg).await {
                                conn.on_close(&err.to_string(), false).await;
                                break;
                            }
                        }
                        None => conn.mux_data_rx = None,
                    }
                },
                Some((instant, value)) = rx.recv() => {""",
    )
    changes.replace(
        sconn,
        """        if pf.multiplex {
            crate::port_forward_mux::cap_packet_size(&mut self.stream);
            // `inner.tx` is set for the connection's whole life; `None` here is
            // unreachable, and refusing the login is the only honest answer.
            self.port_forward_mux = self.inner.tx.clone().map(|tx| {
                super::port_forward_mux::PortForwardMux::new(tx, self.port_forward_address.clone())
            });
            return self.port_forward_mux.is_some();
        }""",
        """        if pf.multiplex {
            crate::port_forward_mux::cap_packet_size(&mut self.stream);
            let (data_tx, data_rx) =
                mpsc::channel(crate::port_forward_mux::DATA_QUEUE_FRAMES);
            let (control_tx, control_rx) = mpsc::unbounded_channel();
            self.mux_data_rx = Some(data_rx);
            self.mux_ctrl_rx = Some(control_rx);
            self.port_forward_mux = Some(super::port_forward_mux::PortForwardMux::with_sink(
                crate::port_forward_mux::FrameSink::Queued {
                    data: data_tx,
                    control: control_tx,
                },
                self.port_forward_address.clone(),
            ));
            return true;
        }""",
    )
    changes.replace(
        sconn,
        """        if let Some(mut mux) = self.port_forward_mux.take() {
            mux.close_all();
        }""",
        """        if let Some(mut mux) = self.port_forward_mux.take() {
            mux.close_all();
        }
        self.mux_ctrl_rx = None;
        self.mux_data_rx = None;""",
    )
