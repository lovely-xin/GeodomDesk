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
                decision, rendezvous_server, key, !token.is_empty() || !switch_code.is_empty(),
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

    #[tokio::test]
    async fn manual_restart_interrupts_retry_wait() {""",
    )
