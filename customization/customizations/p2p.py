"""Keep native UDP punching independent of TCP NAT and WebRTC signaling."""


def apply(changes):
    apply_udp_probe(changes)
    apply_mediator(changes)


def apply_udp_probe(changes):
    path = "src/client.rs"
    text = changes[path]
    start = text.index("        let (stop_udp_tx, stop_udp_rx) = oneshot::channel::<()>();")
    end = text.index("        // Under force-relay a direct IPv6 path", start)
    changes.replace(
        path, text[start:end],
        """        let udp = if crate::get_udp_punch_enabled() && !interface.is_force_relay() {
            if let Ok((socket, addr)) = new_direct_udp_for_unverified(&rendezvous_server).await {
                let (port_tx, port_rx) = oneshot::channel();
                let probe_socket = socket.clone();
                tokio::spawn(async move {
                    allow_err!(test_udp_uat(probe_socket, addr, port_tx).await);
                });
                (Some(socket), Some(port_rx))
            } else {
                (None, None)
            }
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
        ("        let udp_nat_port = udp.1.map(|x| *x.lock().unwrap()).unwrap_or(0);\n", ""),
        (
            "async fn test_udp_uat(\n    udp_socket: Arc<UdpSocket>,\n    server_addr: SocketAddr,\n"
            "    udp_port: Arc<Mutex<u16>>,\n    mut stop_udp_rx: oneshot::Receiver<()>,",
            """async fn collect_udp_probe(
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
    async fn waits_for_the_native_udp_reply_without_webrtc() {
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let server_addr = server.local_addr().unwrap();
        let client = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        client.connect(server_addr).await.unwrap();
        let (send, receive) = oneshot::channel();
        let responder = async {
            let mut bytes = [0; 256];
            let (size, addr) = server.recv_from(&mut bytes).await.unwrap();
            assert!(RendezvousMessage::parse_from_bytes(&bytes[..size]).unwrap().has_test_nat_request());
            tokio::time::sleep(Duration::from_millis(25)).await;
            let mut response = RendezvousMessage::new();
            response.set_test_nat_response(hbb_common::rendezvous_proto::TestNatResponse {
                port: i32::from(addr.port()),
                ..Default::default()
            });
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
