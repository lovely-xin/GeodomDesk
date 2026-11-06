"""Keep native UDP punching independent of TCP NAT and WebRTC signaling."""


def apply(changes):
    apply_udp_probe(changes)
    keep_legacy_udp_punch(changes)
    prefer_direct_routes(changes)
    apply_mediator(changes)


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
    changes.replace(path, """/// Race WebRTC against the other transports, preferring P2P: `select_ok` would always pick the
/// relay, whose TCP connect beats ICE + DTLS + SCTP by an order of magnitude. An `is_p2p` result
/// wins outright; a relayed one — from either side, since `webrtc_fut` is a whole punch attempt
/// that can also end in a relay — is held for `window_ms` to give the other side a chance.""", """/// Prefer a direct result while any direct attempt can still finish. Keep a successful relay
/// as a fallback for at most `window_ms`, and use it sooner when all remaining attempts fail.""")
    changes.replace(path, """                    // `webrtc_fut` is a whole punch attempt, not just the WebRTC connect, so it
                    // can end in a relay of its own. Committing that immediately would preempt a
                    // direct punch still in flight on the other branch — the exact inversion this
                    // function exists to prevent — so hold it on the same terms as any relay.""", """                    // A whole punch attempt can itself end in a relay. Hold it while another
                    // branch can still deliver a direct connection.""")
    changes.replace(path, '                            None if others_fut.is_none() => bail!("WebRTC failed: {}", e),',
                    '                            None if others_fut.is_none() => return Err(e),')
    changes.replace(path, '                                log::warn!("WebRTC leg also failed: {}", we);',
                    '                                log::warn!("Preferred connection attempt also failed: {}", we);')
    changes.replace(path, '    async fn unfinished_ipv6_still_beats_held_relay() {', '''    async fn native_udp_still_beats_a_faster_relay_without_webrtc() {
        let got = race_transports_prefer_webrtc(
            ok_after(90, "udp"),
            vec![ok_after(5, "relay")],
            400,
            |transport| *transport == "udp",
        )
        .await
        .unwrap();
        assert_eq!(got, "udp");
    }

    #[tokio::test]
    async fn unfinished_ipv6_still_beats_held_relay() {''')
    text = changes[path].replace("race_transports_prefer_webrtc", "race_transports_prefer_direct")
    start = text.index("async fn race_transports_prefer_direct")
    end = text.index("// A peer decides how many ICE candidates", start)
    changes[path] = text[:start] + text[start:end].replace("webrtc_fut", "preferred_fut").replace(
        "webrtc_err", "preferred_err"
    ) + text[end:]


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
