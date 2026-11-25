"""Performance, P2P setup latency, transport zero-copy and lifecycle fixes."""


def _replace_once(changes, path, old, new):
    text = changes[path]
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{path}: expected 1 occurrence of anchor, found {count}: {old[:80]!r}")
    changes[path] = text.replace(old, new, 1)


def apply_port_forward_mux(changes):
    name = "src/port_forward_mux.rs"
    old = """        pub fn set_muxed(&self, mut stream: Stream, kcp: Option<crate::kcp_stream::KcpStream>, interface: impl Interface) -> Arc<TunnelHandle> {
            cap_packet_size(&mut stream);
            let (data_tx, data_rx) = mpsc::channel(DATA_QUEUE_FRAMES);
            let (control_tx, control_rx) = mpsc::unbounded_channel();
            let handle = Arc::new(TunnelHandle {
                sink: FrameSink::Queued { data: data_tx, control: control_tx },
                channels: Mutex::new(HashMap::new()),
                next_id: AtomicI32::new(1),
                reported: Default::default(),
                teardown: watch::channel(false).0,
            });
            let state = self.state.clone();
            // Publish before spawning: if the loop exits first and resets the
            // state, a later publish here would pin it at Muxed with a dead
            // handle and the listener could never re-establish.
            self.state.send_replace(TunnelState::Muxed(handle.clone()));
            tokio::spawn(tunnel_loop(
                stream,
                kcp,
                handle.clone(),
                data_rx,
                control_rx,
                interface,
                state,
                self.lifetime.subscribe(),
            ));
            handle
        }"""
    new = """        pub fn set_muxed(&self, stream: Stream, kcp: Option<crate::kcp_stream::KcpStream>, interface: impl Interface) -> Arc<TunnelHandle> {
            self.set_muxed_with_guard(stream, kcp, interface, ())
        }

        pub fn set_muxed_with_guard<G: Send + 'static>(
            &self,
            mut stream: Stream,
            kcp: Option<crate::kcp_stream::KcpStream>,
            interface: impl Interface,
            guard: G,
        ) -> Arc<TunnelHandle> {
            cap_packet_size(&mut stream);
            let (data_tx, data_rx) = mpsc::channel(DATA_QUEUE_FRAMES);
            let (control_tx, control_rx) = mpsc::unbounded_channel();
            let handle = Arc::new(TunnelHandle {
                sink: FrameSink::Queued { data: data_tx, control: control_tx },
                channels: Mutex::new(HashMap::new()),
                next_id: AtomicI32::new(1),
                reported: Default::default(),
                teardown: watch::channel(false).0,
            });
            let state = self.state.clone();
            // Publish before spawning: if the loop exits first and resets the
            // state, a later publish here would pin it at Muxed with a dead
            // handle and the listener could never re-establish.
            self.state.send_replace(TunnelState::Muxed(handle.clone()));
            tokio::spawn(tunnel_loop(
                stream,
                kcp,
                handle.clone(),
                data_rx,
                control_rx,
                interface,
                state,
                self.lifetime.subscribe(),
                guard,
            ));
            handle
        }"""
    _replace_once(changes, name, old, new)

    old_loop = """    async fn tunnel_loop(
        mut stream: Stream,
        // Drives a KCP stream's UDP I/O; dropping it kills the stream.
        kcp: Option<crate::kcp_stream::KcpStream>,
        handle: Arc<TunnelHandle>,
        mut data_rx: mpsc::Receiver<Message>,
        mut control_rx: mpsc::UnboundedReceiver<Message>,
        interface: impl Interface,
        state: watch::Sender<TunnelState>,
        mut lifetime: watch::Receiver<()>,
    ) {
        let err = loop {
            tokio::select! {
                Some(msg) = control_rx.recv() => {
                    if let Err(e) = stream.send(&msg).await {
                        break format!("send failed: {}", e);
                    }
                }
                res = stream.next_timeout(READ_TIMEOUT) => match res {
                    Some(Ok(bytes)) => {
                        let Ok(msg) = Message::parse_from_bytes(&bytes) else { continue };
                        match msg.union {
                            Some(message::Union::PortForwardChannel(ch)) => {
                                if let Some(err) = handle.on_frame(ch) {
                                    interface.on_error(&err);
                                }
                            }
                            Some(message::Union::TestDelay(t)) => {
                                interface.handle_test_delay(t, &mut stream).await;
                            }
                            Some(message::Union::Misc(misc)) => {
                                if let Some(misc::Union::CloseReason(r)) = misc.union {
                                    break format!("closed by peer: {}", r);
                                }
                            }
                            _ => {}
                        }
                    }
                    Some(Err(e)) => break format!("read failed: {}", e),
                    None => break "timeout or reset by the peer".to_owned(),
                },
                Some(msg) = data_rx.recv() => {
                    if let Err(e) = stream.send(&msg).await {
                        break format!("send failed: {}", e);
                    }
                }
                _ = lifetime.changed() => {
                    if kcp.is_some() {
                        send_kcp_close_reason(&mut stream).await;
                    }
                    break "window closed".to_owned()
                }
            }
        };
        log::info!("port forward tunnel ended: {}", err);
        handle.close_all();
        state.send_replace(TunnelState::Unset);
    }"""
    new_loop = """    async fn tunnel_loop<G: Send + 'static>(
        mut stream: Stream,
        // Drives a KCP stream's UDP I/O; dropping it kills the stream.
        kcp: Option<crate::kcp_stream::KcpStream>,
        handle: Arc<TunnelHandle>,
        mut data_rx: mpsc::Receiver<Message>,
        mut control_rx: mpsc::UnboundedReceiver<Message>,
        interface: impl Interface,
        state: watch::Sender<TunnelState>,
        mut lifetime: watch::Receiver<()>,
        guard: G,
    ) {
        let _guard = guard;
        let read_timeout = Duration::from_millis(READ_TIMEOUT);
        let mut last_recv = Instant::now();
        let mut webrtc_suspect: Option<Instant> = None;
        let mut watchdog = tokio::time::interval_at(
            Instant::now() + Duration::from_secs(1),
            Duration::from_secs(1),
        );
        watchdog.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let err = loop {
            tokio::select! {
                Some(msg) = control_rx.recv() => {
                    match tokio::time::timeout(Duration::from_secs(10), stream.send(&msg)).await {
                        Ok(Ok(())) => {}
                        Ok(Err(e)) => break format!("send failed: {}", e),
                        Err(_) => break "send timed out".to_owned(),
                    }
                }
                res = stream.next() => match res {
                    Some(Ok(bytes)) => {
                        last_recv = Instant::now();
                        webrtc_suspect = None;
                        let Ok(msg) = Message::parse_from_bytes(&bytes) else { continue };
                        match msg.union {
                            Some(message::Union::PortForwardChannel(ch)) => {
                                if let Some(err) = handle.on_frame(ch) {
                                    interface.on_error(&err);
                                }
                            }
                            Some(message::Union::TestDelay(t)) => {
                                interface.handle_test_delay(t, &mut stream).await;
                            }
                            Some(message::Union::Misc(misc)) => {
                                if let Some(misc::Union::CloseReason(r)) = misc.union {
                                    break format!("closed by peer: {}", r);
                                }
                            }
                            _ => {}
                        }
                    }
                    Some(Err(e)) => break format!("read failed: {}", e),
                    None => break "reset by the peer".to_owned(),
                },
                _ = watchdog.tick() => {
                    if stream.webrtc_disconnected() {
                        let since = *webrtc_suspect.get_or_insert_with(Instant::now);
                        if since.elapsed() >= Duration::from_secs(3) {
                            break "webrtc disconnected".to_owned();
                        }
                    } else {
                        webrtc_suspect = None;
                    }
                    if kcp
                        .as_ref()
                        .and_then(|k| k.peer_silent_for())
                        .is_some_and(|silent| silent >= Duration::from_secs(8))
                    {
                        break "kcp peer silent".to_owned();
                    }
                    if last_recv.elapsed() >= read_timeout {
                        break "timeout or reset by the peer".to_owned();
                    }
                }
                Some(msg) = data_rx.recv() => {
                    let mut send_err: Option<String> = None;
                    for _ in 0..16 {
                        let Ok(ctrl) = control_rx.try_recv() else {
                            break;
                        };
                        match tokio::time::timeout(Duration::from_secs(10), stream.send(&ctrl)).await {
                            Ok(Ok(())) => {}
                            Ok(Err(e)) => {
                                send_err = Some(format!("send failed: {}", e));
                                break;
                            }
                            Err(_) => {
                                send_err = Some("send timed out".to_owned());
                                break;
                            }
                        }
                    }
                    if let Some(e) = send_err {
                        break e;
                    }
                    match tokio::time::timeout(Duration::from_secs(10), stream.send(&msg)).await {
                        Ok(Ok(())) => {}
                        Ok(Err(e)) => break format!("send failed: {}", e),
                        Err(_) => break "send timed out".to_owned(),
                    }
                }
                _ = lifetime.changed() => {
                    if kcp.is_some() {
                        send_kcp_close_reason(&mut stream).await;
                    }
                    break "window closed".to_owned();
                }
            }
        };
        stream.close_webrtc();
        drop(stream);
        drop(kcp);
        drop(_guard);
        log::info!("port forward tunnel ended: {}", err);
        handle.close_all();
        state.send_replace(TunnelState::Unset);
    }"""
    _replace_once(changes, name, old_loop, new_loop)
    _replace_once(
        changes,
        name,
        """        pub fn set_failed(&self) {
            self.state.send_replace(TunnelState::Failed);
        }
    }""",
        """        pub fn set_failed(&self) {
            self.state.send_replace(TunnelState::Failed);
        }
    }

    impl TunnelHandle {
        pub fn is_closed(&self) -> bool {
            self.sink.is_closed() || *self.teardown.borrow()
        }
    }""",
    )


def apply_port_forward_lifetime(changes):
    name = "src/port_forward.rs"
    # RustDesk 1.5.3 already binds KCP I/O to each tunnel's lifetime.
    source = changes[name]
    keep = "let _keep_it = hc_connection(feedback, rendezvous_server, token).await;"
    if source.count(keep) != 2:
        raise ValueError("Unexpected port-forward keep-alive calls")
    changes[name] = source.replace(keep, keep.replace("_keep_it", "keep_alive"))
    pairs = (
        ("Ok(Some((stream, kcp)))", "Ok(Some((stream, keep_alive, kcp)))"),
        (") -> ResultType<Option<(Stream, Option<crate::kcp_stream::KcpStream>)>> {",
         ") -> ResultType<Option<(Stream, Option<mpsc::UnboundedSender<()>>, Option<crate::kcp_stream::KcpStream>)>> {"),
        ("Ok(Some((stream, kcp))) =>", "Ok(Some((stream, keep_alive, kcp))) =>"),
        ("let _kcp = kcp;", "let _retainer = (keep_alive, kcp);"),
        ("let handle = tunnel.set_muxed(outcome.stream, outcome.kcp, interface.clone());",
         "let handle = tunnel.set_muxed_with_guard(outcome.stream, outcome.kcp, interface.clone(), outcome.keep_alive);"),
        ("Ok(Some(LoginOutcome {\n        stream,", "Ok(Some(LoginOutcome {\n        stream,\n        keep_alive,"),
        ("let kcp = outcome.kcp;", "let retainer = (outcome.keep_alive, outcome.kcp);"),
        ("struct LoginOutcome {\n    stream: Stream,", "struct LoginOutcome {\n    stream: Stream,\n    keep_alive: Option<mpsc::UnboundedSender<()>>,"),
    )
    for old, new in pairs:
        if old == "let _kcp = kcp;":
            _replace_once(changes, name, "                            let _kcp = kcp;",
                          "                            let _retainer = (keep_alive, kcp);")
            _replace_once(changes, name, "        let _kcp = kcp;\n        stream.set_raw();",
                          "        let _retainer = retainer;\n        stream.set_raw();")
        elif old == "Ok(Some((stream, kcp)))":
            _replace_once(changes, name, "    Ok(Some((stream, kcp)))\n}",
                          "    Ok(Some((stream, keep_alive, kcp)))\n}")
        else:
            _replace_once(changes, name, old, new)
    _replace_once(changes, name, """            res = stream.next() => {
                if let Some(Ok(bytes)) = res {
                    allow_err!(forward.send(bytes).await);
                } else {
                    break;
                }
            },
        }
    }
    Ok(())
}""", """            res = stream.next() => {
                if let Some(Ok(bytes)) = res {
                    allow_err!(forward.send(bytes).await);
                } else {
                    break;
                }
            },
        }
    }
    stream.close_webrtc();
    Ok(())
}""")


def apply_video_service_sync_channel(changes):
    name = "src/server/video_service.rs"
    _replace_once(
        changes,
        name,
        """use hbb_common::{
    anyhow::anyhow,
    config,
    tokio::sync::{
        mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender},
        Mutex as TokioMutex,
    },
};""",
        """use hbb_common::{anyhow::anyhow, config};""",
    )
    _replace_once(
        changes,
        name,
        """type FrameFetchedNotifierSender = UnboundedSender<(i32, Option<Instant>)>;
type FrameFetchedNotifierReceiver = Arc<TokioMutex<UnboundedReceiver<(i32, Option<Instant>)>>>;""",
        """type FrameFetchedNotifierSender = std::sync::mpsc::Sender<(i32, Option<Instant>)>;
type FrameFetchedNotifierReceiver = Arc<Mutex<std::sync::mpsc::Receiver<(i32, Option<Instant>)>>>;""",
    )
    _replace_once(
        changes,
        name,
        """    #[tokio::main(flavor = "current_thread")]
    async fn try_wait_next(&mut self, fetched_conn_ids: &mut HashSet<i32>, timeout_millis: u64) {
        if self.send_conn_ids.is_empty() {
            return;
        }

        let timeout_dur = Duration::from_millis(timeout_millis as u64);
        let receiver = {
            match FRAME_FETCHED_NOTIFIERS
                .lock()
                .unwrap()
                .get(&self.display_idx)
            {
                Some(notifier) => notifier.1.clone(),
                None => {
                    return;
                }
            }
        };
        let mut receiver_guard = receiver.lock().await;
        match tokio::time::timeout(timeout_dur, receiver_guard.recv()).await {
            Err(_) => {
                // break if timeout
                // log::error!("blocking wait frame receiving timeout {}", timeout_millis);
            }
            Ok(Some((id, instant))) => {
                if let Some(tm) = instant {
                    log::trace!("Channel recv latency: {}", tm.elapsed().as_secs_f32());
                }
                fetched_conn_ids.insert(id);
            }
            Ok(None) => {
                // this branch would never be reached
            }
        }
        while !receiver_guard.is_empty() {
            if let Some((id, instant)) = receiver_guard.recv().await {
                if let Some(tm) = instant {
                    log::trace!("Channel recv latency: {}", tm.elapsed().as_secs_f32());
                }
                fetched_conn_ids.insert(id);
            }
        }
    }""",
        """    fn try_wait_next(&mut self, fetched_conn_ids: &mut HashSet<i32>, timeout_millis: u64) {
        if self.send_conn_ids.is_empty() {
            return;
        }

        let timeout_dur = Duration::from_millis(timeout_millis);
        let receiver = {
            match FRAME_FETCHED_NOTIFIERS
                .lock()
                .unwrap()
                .get(&self.display_idx)
            {
                Some(notifier) => notifier.1.clone(),
                None => {
                    return;
                }
            }
        };
        let receiver_guard = receiver.lock().unwrap();
        if let Ok((id, instant)) = receiver_guard.recv_timeout(timeout_dur) {
            if let Some(tm) = instant {
                log::trace!("Channel recv latency: {}", tm.elapsed().as_secs_f32());
            }
            fetched_conn_ids.insert(id);
        }
        while let Ok((id, instant)) = receiver_guard.try_recv() {
            if let Some(tm) = instant {
                log::trace!("Channel recv latency: {}", tm.elapsed().as_secs_f32());
            }
            fetched_conn_ids.insert(id);
        }
    }""",
    )
    _replace_once(
        changes,
        name,
        """            let (tx, rx) = unbounded_channel();
            (tx, Arc::new(TokioMutex::new(rx)))""",
        """            let (tx, rx) = std::sync::mpsc::channel();
            (tx, Arc::new(Mutex::new(rx)))""",
    )


def apply_webrtc_and_ice_bridge(changes):
    _replace_once(
        changes,
        "libs/hbb_common/src/webrtc.rs",
        """        // Answerer only: `remote_endpoint` is empty when this side is the offerer. The
        // desktop controller keeps the crate default so it stays an unchanged comparison
        // point while the persistence experiment runs on the controlled side.
        if !remote_endpoint.is_empty() {
            s.set_ice_max_binding_requests(Some(Self::ICE_MAX_BINDING_REQUESTS));
        }""",
        """        s.set_ice_max_binding_requests(Some(Self::ICE_MAX_BINDING_REQUESTS));""",
    )

    client_rs = "src/client.rs"
    _replace_once(
        changes,
        client_rs,
        "            mpsc::{error::TryRecvError, unbounded_channel, UnboundedReceiver},",
        "            mpsc::{unbounded_channel, UnboundedReceiver},",
    )
    old_bridge = """    fn spawn_webrtc_ice_bridge(
        mut socket: Stream,
        mut local_ice_rx: Option<UnboundedReceiver<String>>,
        webrtc: WebRTCStream,
        peer: String,
        session_key: String,
    ) -> oneshot::Sender<()> {
        let (stop_tx, mut stop_rx) = oneshot::channel::<()>();
        tokio::spawn(async move {
            let mut pending_resend: Vec<(Instant, RendezvousMessage)> = Vec::new();
            loop {
                match stop_rx.try_recv() {
                    Ok(_) | Err(tokio::sync::oneshot::error::TryRecvError::Closed) => break,
                    Err(tokio::sync::oneshot::error::TryRecvError::Empty) => {}
                }

                if let Some(rx) = local_ice_rx.as_mut() {
                    loop {
                        match rx.try_recv() {
                            Ok(candidate) => {
                                let mut msg = RendezvousMessage::new();
                                msg.set_ice_candidate(IceCandidate {
                                    id: peer.clone(),
                                    session_key: session_key.clone(),
                                    candidate,
                                    ..Default::default()
                                });
                                // Bound the send so a stalled rendezvous socket cannot block the
                                // bridge past its stop signal.
                                match timeout(3000, socket.send(&msg)).await {
                                    Ok(Ok(())) => {}
                                    Ok(Err(err)) => {
                                        log::warn!("failed to send WebRTC ICE candidate: {}", err);
                                        return;
                                    }
                                    Err(_) => {
                                        log::warn!("WebRTC ICE candidate send timed out");
                                        return;
                                    }
                                }
                                pending_resend.push((Instant::now(), msg));
                            }
                            Err(TryRecvError::Empty) => break,
                            Err(TryRecvError::Disconnected) => {
                                local_ice_rx = None;
                                break;
                            }
                        }
                    }
                }

                // Re-send each candidate once after a short delay: the rendezvous hop to the peer
                // can be UDP, so the first copy may be lost; the remote ICE agent dedups repeats.
                let mut i = 0;
                while i < pending_resend.len() {
                    if pending_resend[i].0.elapsed() >= Self::WEBRTC_ICE_RESEND_DELAY {
                        let (_, msg) = pending_resend.swap_remove(i);
                        match timeout(3000, socket.send(&msg)).await {
                            Ok(Ok(())) => {}
                            Ok(Err(err)) => {
                                log::warn!("failed to re-send WebRTC ICE candidate: {}", err);
                                return;
                            }
                            Err(_) => {
                                log::warn!("WebRTC ICE candidate re-send timed out");
                                return;
                            }
                        }
                    } else {
                        i += 1;
                    }
                }

                // Read one incoming message, blocking up to the poll window. Distinguish a real
                // timeout (keep looping to drain outbound candidates) from stream EOF/error: the
                // rendezvous server closes the punch connection after the response, and an
                // unrecognized-message server closes it on the first candidate we send. Without
                // this, the collapsed None-on-EOF made the loop hot-spin a core until stop.
                match timeout(100, socket.next()).await {
                    Err(_) => {}
                    Ok(None) => break,
                    Ok(Some(Ok(bytes))) => {
                        if let Ok(msg_in) = RendezvousMessage::parse_from_bytes(&bytes) {
                            if let Some(rendezvous_message::Union::IceCandidate(ice)) = msg_in.union
                            {
                                if Self::is_expected_webrtc_ice_candidate(&ice, &session_key) {
                                    if let Err(err) =
                                        webrtc.add_remote_ice_candidate(&ice.candidate).await
                                    {
                                        if let Some(n) = REJECTED_ICE_LOG.due() {
                                            log::warn!(
                                                "failed to add {} WebRTC ICE candidate(s), last: {}",
                                                n,
                                                err
                                            );
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Ok(Some(Err(err))) => {
                        log::debug!("WebRTC ICE bridge socket read ended: {}", err);
                        break;
                    }
                }
            }
        });
        stop_tx
    }"""
    new_bridge = """    fn spawn_webrtc_ice_bridge(
        mut socket: Stream,
        mut local_ice_rx: Option<UnboundedReceiver<String>>,
        webrtc: WebRTCStream,
        peer: String,
        session_key: String,
    ) -> oneshot::Sender<()> {
        let (stop_tx, mut stop_rx) = oneshot::channel::<()>();
        tokio::spawn(async move {
            let mut pending_resend: std::collections::VecDeque<(
                tokio::time::Instant,
                RendezvousMessage,
            )> = std::collections::VecDeque::new();
            loop {
                let recv_local = async {
                    match local_ice_rx.as_mut() {
                        Some(rx) => rx.recv().await,
                        None => std::future::pending().await,
                    }
                };
                let next_resend = async {
                    match pending_resend.front() {
                        Some((deadline, _)) => tokio::time::sleep_until(*deadline).await,
                        None => std::future::pending().await,
                    }
                };
                tokio::select! {
                    _ = &mut stop_rx => break,
                    maybe_candidate = recv_local => {
                        match maybe_candidate {
                            Some(candidate) => {
                                let mut msg = RendezvousMessage::new();
                                msg.set_ice_candidate(IceCandidate {
                                    id: peer.clone(),
                                    session_key: session_key.clone(),
                                    candidate,
                                    ..Default::default()
                                });
                                match timeout(3000, socket.send(&msg)).await {
                                    Ok(Ok(())) => {}
                                    Ok(Err(err)) => {
                                        log::warn!("failed to send WebRTC ICE candidate: {}", err);
                                        return;
                                    }
                                    Err(_) => {
                                        log::warn!("WebRTC ICE candidate send timed out");
                                        return;
                                    }
                                }
                                pending_resend.push_back((
                                    tokio::time::Instant::now() + Self::WEBRTC_ICE_RESEND_DELAY,
                                    msg,
                                ));
                            }
                            None => {
                                local_ice_rx = None;
                            }
                        }
                    }
                    _ = next_resend => {
                        while pending_resend
                            .front()
                            .is_some_and(|(deadline, _)| tokio::time::Instant::now() >= *deadline)
                        {
                            if let Some((_, msg)) = pending_resend.pop_front() {
                                match timeout(3000, socket.send(&msg)).await {
                                    Ok(Ok(())) => {}
                                    Ok(Err(err)) => {
                                        log::warn!("failed to re-send WebRTC ICE candidate: {}", err);
                                        return;
                                    }
                                    Err(_) => {
                                        log::warn!("WebRTC ICE candidate re-send timed out");
                                        return;
                                    }
                                }
                            }
                        }
                    }
                    incoming = socket.next() => {
                        match incoming {
                            None => break,
                            Some(Ok(bytes)) => {
                                if let Ok(msg_in) = RendezvousMessage::parse_from_bytes(&bytes) {
                                    if let Some(rendezvous_message::Union::IceCandidate(ice)) =
                                        msg_in.union
                                    {
                                        if Self::is_expected_webrtc_ice_candidate(
                                            &ice,
                                            &session_key,
                                        ) {
                                            if let Err(err) =
                                                webrtc.add_remote_ice_candidate(&ice.candidate).await
                                            {
                                                if let Some(n) = REJECTED_ICE_LOG.due() {
                                                    log::warn!(
                                                        "failed to add {} WebRTC ICE candidate(s), last: {}",
                                                        n,
                                                        err
                                                    );
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Some(Err(err)) => {
                                log::debug!("WebRTC ICE bridge socket read ended: {}", err);
                                break;
                            }
                        }
                    }
                }
            }
        });
        stop_tx
    }"""
    _replace_once(changes, client_rs, old_bridge, new_bridge)

    _replace_once(
        changes,
        client_rs,
        """        if let Some(udp_socket_nat) = udp_socket_nat {
            direct_futures.push(
                async move {
                    let (conn, kcp, typ) =
                        udp_nat_connect(udp_socket_nat, "UDP", connect_timeout).await?;
                    Ok((conn, kcp, typ, true))
                }
                .boxed(),
            );
        }
        if let Some(udp_socket_v6) = udp_socket_v6 {
            direct_futures.push(
                async move {
                    let (conn, kcp, typ) =
                        udp_nat_connect(udp_socket_v6, "IPv6", connect_timeout).await?;
                    Ok((conn, kcp, typ, true))
                }
                .boxed(),
            );
        }""",
        """        let kcp_timeout = if is_local {
            connect_timeout
        } else {
            connect_timeout.max(Self::relay_fallback_delay_ms())
        };
        if let Some(udp_socket_nat) = udp_socket_nat {
            direct_futures.push(
                async move {
                    let (conn, kcp, typ) =
                        udp_nat_connect(udp_socket_nat, "UDP", kcp_timeout).await?;
                    Ok((conn, kcp, typ, true))
                }
                .boxed(),
            );
        }
        if let Some(udp_socket_v6) = udp_socket_v6 {
            direct_futures.push(
                async move {
                    let (conn, kcp, typ) =
                        udp_nat_connect(udp_socket_v6, "IPv6", kcp_timeout).await?;
                    Ok((conn, kcp, typ, true))
                }
                .boxed(),
            );
        }""",
    )


def apply_transport_hotpath(changes):
    _replace_once(
        changes,
        "libs/hbb_common/src/bytes_codec.rs",
        """        if data.len() <= 0x3F {
            buf.put_u8((data.len() << 2) as u8);
        } else if data.len() <= 0x3FFF {
            buf.put_u16_le((data.len() << 2) as u16 | 0x1);
        } else if data.len() <= 0x3FFFFF {
            let h = (data.len() << 2) as u32 | 0x2;
            buf.put_u16_le((h & 0xFFFF) as u16);
            buf.put_u8((h >> 16) as u8);
        } else if data.len() <= MAX_FRAME_LENGTH {
            buf.put_u32_le((data.len() << 2) as u32 | 0x3);
        } else {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Overflow"));
        }
        buf.extend(data);""",
        """        if data.len() <= 0x3F {
            buf.reserve(1 + data.len());
            buf.put_u8((data.len() << 2) as u8);
        } else if data.len() <= 0x3FFF {
            buf.reserve(2 + data.len());
            buf.put_u16_le((data.len() << 2) as u16 | 0x1);
        } else if data.len() <= 0x3FFFFF {
            buf.reserve(3 + data.len());
            let h = (data.len() << 2) as u32 | 0x2;
            buf.put_u16_le((h & 0xFFFF) as u16);
            buf.put_u8((h >> 16) as u8);
        } else if data.len() <= MAX_FRAME_LENGTH {
            buf.reserve(4 + data.len());
            buf.put_u32_le((data.len() << 2) as u32 | 0x3);
        } else {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "Overflow"));
        }
        buf.put_slice(&data);""",
    )

    tcp_rs = "libs/hbb_common/src/tcp.rs"
    _replace_once(
        changes,
        tcp_rs,
        "use bytes::{BufMut, Bytes, BytesMut};",
        "use bytes::{Buf, Bytes, BytesMut};",
    )
    _replace_once(
        changes,
        tcp_rs,
        """    pub async fn new<T: ToSocketAddrs + std::fmt::Display>(
        remote_addr: T,
        local_addr: Option<SocketAddr>,
        ms_timeout: u64,
    ) -> ResultType<Self> {
        for remote_addr in lookup_host(&remote_addr).await? {
            let local = if let Some(addr) = local_addr {
                addr
            } else {
                crate::config::Config::get_any_listen_addr(remote_addr.is_ipv4())
            };
            if let Ok(socket) = new_socket(local, true) {
                if let Ok(Ok(stream)) =
                    super::timeout(ms_timeout, socket.connect(remote_addr)).await
                {
                    stream.set_nodelay(true).ok();
                    let addr = stream.local_addr()?;
                    return Ok(Self(
                        Framed::new(DynTcpStream(Box::new(stream)), BytesCodec::new()),
                        addr,
                        None,
                        0,
                    ));
                }
            }
        }
        bail!(format!("Failed to connect to {remote_addr}"));
    }""",
        """    pub async fn new<T: ToSocketAddrs + std::fmt::Display>(
        remote_addr: T,
        local_addr: Option<SocketAddr>,
        ms_timeout: u64,
    ) -> ResultType<Self> {
        let resolved: Vec<SocketAddr> = lookup_host(&remote_addr).await?.collect();
        if resolved.is_empty() {
            bail!(format!("Failed to connect to {remote_addr}"));
        }
        let filtered: Vec<SocketAddr> = match local_addr {
            Some(local) => {
                let matching: Vec<SocketAddr> = resolved
                    .iter()
                    .copied()
                    .filter(|addr| addr.is_ipv4() == local.is_ipv4())
                    .collect();
                if matching.is_empty() {
                    resolved
                } else {
                    matching
                }
            }
            None => resolved,
        };
        let ordered = order_happy_eyeballs(filtered);
        if ordered.len() == 1 {
            let target = ordered[0];
            let local = local_addr
                .unwrap_or_else(|| crate::config::Config::get_any_listen_addr(target.is_ipv4()));
            if let Ok(socket) = new_socket(local, true) {
                if let Ok(Ok(stream)) = super::timeout(ms_timeout, socket.connect(target)).await {
                    stream.set_nodelay(true).ok();
                    let addr = stream.local_addr()?;
                    return Ok(Self(
                        Framed::new(DynTcpStream(Box::new(stream)), BytesCodec::new()),
                        addr,
                        None,
                        0,
                    ));
                }
            }
            bail!(format!("Failed to connect to {remote_addr}"));
        }
        let deadline =
            tokio::time::Instant::now() + std::time::Duration::from_millis(ms_timeout.max(1));
        let stagger = std::time::Duration::from_millis(250);
        let mut iter = ordered.into_iter();
        let mut inflight = futures::stream::FuturesUnordered::new();
        let mut next_dial_at = None;
        if let Some(first) = iter.next() {
            inflight.push(Box::pin(dial_tcp_candidate(first, local_addr)));
            if iter.len() > 0 {
                next_dial_at = Some(tokio::time::Instant::now() + stagger);
            }
        }
        loop {
            if tokio::time::Instant::now() >= deadline {
                break;
            }
            let sleep_deadline = next_dial_at.map_or(deadline, |t| t.min(deadline));
            let timer = tokio::time::sleep_until(sleep_deadline);
            tokio::select! {
                _ = timer => {
                    if tokio::time::Instant::now() >= deadline {
                        break;
                    }
                    if let Some(next_addr) = iter.next() {
                        inflight.push(Box::pin(dial_tcp_candidate(next_addr, local_addr)));
                        next_dial_at = (iter.len() > 0).then(|| tokio::time::Instant::now() + stagger);
                    } else {
                        next_dial_at = None;
                    }
                }
                res = inflight.next(), if !inflight.is_empty() => {
                    match res {
                        Some(Ok((stream, addr))) => {
                            stream.set_nodelay(true).ok();
                            return Ok(Self(
                                Framed::new(DynTcpStream(Box::new(stream)), BytesCodec::new()),
                                addr,
                                None,
                                0,
                            ));
                        }
                        Some(Err(())) => {
                            if let Some(next_addr) = iter.next() {
                                inflight.push(Box::pin(dial_tcp_candidate(next_addr, local_addr)));
                                next_dial_at = (iter.len() > 0).then(|| tokio::time::Instant::now() + stagger);
                            } else if inflight.is_empty() {
                                break;
                            }
                        }
                        None => {
                            if iter.len() == 0 {
                                break;
                            }
                        }
                    }
                }
            }
        }
        bail!(format!("Failed to connect to {remote_addr}"));
    }""",
    )
    _replace_once(
        changes,
        tcp_rs,
        """pub(crate) fn new_socket(""",
        """fn order_happy_eyeballs(addrs: Vec<SocketAddr>) -> Vec<SocketAddr> {
    if addrs.len() <= 2 {
        return addrs;
    }
    let first_is_v4 = addrs[0].is_ipv4();
    let (mut primary, mut secondary): (Vec<SocketAddr>, Vec<SocketAddr>) =
        addrs.into_iter().partition(|a| a.is_ipv4() == first_is_v4);
    let mut out = Vec::with_capacity(primary.len() + secondary.len());
    let mut p = primary.drain(..);
    let mut s = secondary.drain(..);
    loop {
        match (p.next(), s.next()) {
            (Some(a), Some(b)) => {
                out.push(a);
                out.push(b);
            }
            (Some(a), None) => {
                out.push(a);
                out.extend(p);
                break;
            }
            (None, Some(b)) => {
                out.push(b);
                out.extend(s);
                break;
            }
            (None, None) => break,
        }
    }
    out
}

async fn dial_tcp_candidate(
    remote_addr: SocketAddr,
    local_addr: Option<SocketAddr>,
) -> Result<(tokio::net::TcpStream, SocketAddr), ()> {
    let local = local_addr
        .unwrap_or_else(|| crate::config::Config::get_any_listen_addr(remote_addr.is_ipv4()));
    let socket = new_socket(local, true).map_err(|_| ())?;
    let stream = socket.connect(remote_addr).await.map_err(|_| ())?;
    let addr = stream.local_addr().map_err(|_| ())?;
    Ok((stream, addr))
}

pub(crate) fn new_socket(""",
    )
    _replace_once(
        changes,
        tcp_rs,
        """    pub fn dec(&mut self, bytes: &mut BytesMut) -> Result<(), Error> {
        if bytes.len() <= 1 {
            return Ok(());
        }
        self.2 += 1;
        let nonce = FramedStream::get_nonce(self.2);
        match secretbox::open(bytes, &nonce, self.3.as_ref().unwrap_or(&self.0)) {
            Ok(res) => {
                bytes.clear();
                bytes.put_slice(&res);
                Ok(())
            }
            Err(()) => Err(Error::new(ErrorKind::Other, "decryption error")),
        }
    }

    pub fn enc(&mut self, data: &[u8]) -> Vec<u8> {
        self.1 += 1;
        let nonce = FramedStream::get_nonce(self.1);
        secretbox::seal(&data, &nonce, &self.0)
    }""",
        """    pub fn dec(&mut self, bytes: &mut BytesMut) -> Result<(), Error> {
        if bytes.len() <= 1 {
            return Ok(());
        }
        if bytes.len() < secretbox::MACBYTES {
            return Err(Error::new(ErrorKind::Other, "decryption error"));
        }
        self.2 += 1;
        let nonce = FramedStream::get_nonce(self.2);
        let tag = secretbox::Tag::from_slice(&bytes[..secretbox::MACBYTES])
            .ok_or_else(|| Error::new(ErrorKind::Other, "decryption error"))?;
        bytes.advance(secretbox::MACBYTES);
        match secretbox::open_detached(
            bytes.as_mut(),
            &tag,
            &nonce,
            self.3.as_ref().unwrap_or(&self.0),
        ) {
            Ok(()) => Ok(()),
            Err(()) => Err(Error::new(ErrorKind::Other, "decryption error")),
        }
    }

    pub fn enc(&mut self, data: &[u8]) -> Vec<u8> {
        self.1 += 1;
        let nonce = FramedStream::get_nonce(self.1);
        secretbox::seal(&data, &nonce, &self.0)
    }""",
    )

    _replace_once(
        changes,
        "libs/hbb_common/src/socket_client.rs",
        """#[inline]
pub async fn new_direct_udp_for_unverified(
    target: &str,
) -> ResultType<(Arc<UdpSocket>, SocketAddr)> {
    let peer_addr = super::timeout(1000, resolve_target(target)).await??;
    let local_addr = Config::get_any_listen_addr(peer_addr.is_ipv4());
    let socket = UdpSocket::bind(local_addr).await?;
    Ok((Arc::new(socket), peer_addr))
}""",
        """async fn resolve_target_v4_first(target: &str) -> ResultType<SocketAddr> {
    let mut fallback = None;
    for addr in tokio::net::lookup_host(target).await? {
        if addr.is_ipv4() {
            return Ok(addr);
        }
        if fallback.is_none() {
            fallback = Some(addr);
        }
    }
    fallback.context(format!("Failed to look up host for {target}"))
}

#[inline]
pub async fn new_direct_udp_for_unverified(
    target: &str,
) -> ResultType<(Arc<UdpSocket>, SocketAddr)> {
    let peer_addr = super::timeout(1000, resolve_target_v4_first(target)).await??;
    let local_addr = Config::get_any_listen_addr(peer_addr.is_ipv4());
    let socket = UdpSocket::bind(local_addr).await?;
    Ok((Arc::new(socket), peer_addr))
}""",
    )

    ws_rs = "libs/hbb_common/src/websocket.rs"
    _replace_once(
        changes,
        ws_rs,
        """            Err(e) => match (tls_type, is_tls_type_cached, danger_accept_invalid_cert) {
                (TlsType::Rustls, _, None) => {""",
        """            Err(e) => match (tls_type, is_tls_type_cached, danger_accept_invalid_cert) {
                (TlsType::Rustls, _, None) if url.starts_with("wss://") && is_tls_handshake_or_cert_error(&e) => {""",
    )
    _replace_once(
        changes,
        ws_rs,
        """                (TlsType::NativeTls, _, None) => {""",
        """                (TlsType::NativeTls, _, None) if url.starts_with("wss://") && is_tls_handshake_or_cert_error(&e) => {""",
    )
    _replace_once(
        changes,
        ws_rs,
        """                (TlsType::Rustls, false, Some(_)) => {""",
        """                (TlsType::Rustls, false, Some(_)) if url.starts_with("wss://") && is_tls_handshake_or_cert_error(&e) => {""",
    )
    _replace_once(
        changes,
        ws_rs,
        "const DEFAULT_MAX_MESSAGE_SIZE: usize = 64 << 20;",
        """fn is_tls_handshake_or_cert_error(err: &tungstenite::Error) -> bool {
    match err {
        tungstenite::Error::Tls(_) => true,
        tungstenite::Error::Io(io_err) => {
            !matches!(
                io_err.kind(),
                ErrorKind::ConnectionRefused
                    | ErrorKind::TimedOut
                    | ErrorKind::NotFound
                    | ErrorKind::AddrNotAvailable
            ) && !matches!(io_err.raw_os_error(), Some(101 | 113 | 10051 | 10065))
        }
        _ => false,
    }
}

const DEFAULT_MAX_MESSAGE_SIZE: usize = 64 << 20;""",
    )

    _replace_once(
        changes,
        "src/kcp_stream.rs",
        """    async fn kcp_io(
        udp_socket: Arc<UdpSocket>,
        input: mpsc::Sender<KcpPacket>,
        mut output: mpsc::Receiver<KcpPacket>,
        mut stop_receiver: oneshot::Receiver<()>,
    ) {
        let udp = udp_socket.clone();
        tokio::spawn(async move {
            let mut buf = vec![0; 1500];
            // Socket errors are ICMP unreachable on a connected UDP socket — advisory, and
            // routine while a hole forms — so treat them as loss and let KCP's pong timeout reap
            // a link that is really dead. One throttle PER DIRECTION: the error is reported once
            // and cleared, so send-ok/recv-err alternates and a shared counter never fires.
            loop {
                tokio::select! {
                    _ = &mut stop_receiver => {
                        log::debug!("KCP io loop received stop signal");
                        break;
                    }
                    Some(data) = output.recv() => {
                        if let Err(e) = udp.send(&data.inner()).await {
                            if let Some(n) = KCP_SEND_ERR_LOG.due() {
                                log::debug!("KCP send error x{n} (treated as loss), last: {e}");
                            }
                            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                        }
                    }
                    result = udp.recv_from(&mut buf) => {
                        match result {
                            Ok((size, _)) => {
                                if size < std::mem::size_of::<KcpPacketHeader>() {
                                    continue;
                                }
                                input
                                    .send(BytesMut::from(&buf[..size]).into())
                                    .await.ok();
                            }
                            Err(e) => {
                                if let Some(n) = KCP_RECV_ERR_LOG.due() {
                                    log::debug!("KCP recv error x{n} (treated as loss), last: {e}");
                                }
                                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                            }
                        }
                    }
                    else => {
                        log::debug!("KCP endpoint input closed");
                        break;
                    }
                }
            }
        });
    }""",
        """    async fn kcp_io(
        udp_socket: Arc<UdpSocket>,
        input: mpsc::Sender<KcpPacket>,
        mut output: mpsc::Receiver<KcpPacket>,
        stop_receiver: oneshot::Receiver<()>,
    ) {
        let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
        tokio::spawn(async move {
            let _ = stop_receiver.await;
            let _ = stop_tx.send(true);
        });
        let send_udp = udp_socket.clone();
        let mut send_stop = stop_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = send_stop.changed() => {
                        if *send_stop.borrow() {
                            break;
                        }
                    }
                    data = output.recv() => {
                        let Some(data) = data else {
                            break;
                        };
                        if let Err(e) = send_udp.send(&data.inner()).await {
                            if let Some(n) = KCP_SEND_ERR_LOG.due() {
                                log::debug!("KCP send error x{n} (treated as loss), last: {e}");
                            }
                            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                        }
                    }
                }
            }
        });
        let recv_udp = udp_socket;
        let mut recv_stop = stop_rx;
        tokio::spawn(async move {
            let mut buf = vec![0; 1500];
            loop {
                tokio::select! {
                    _ = recv_stop.changed() => {
                        if *recv_stop.borrow() {
                            log::debug!("KCP io loop received stop signal");
                            break;
                        }
                    }
                    result = recv_udp.recv(&mut buf) => {
                        match result {
                            Ok(size) => {
                                if size < std::mem::size_of::<KcpPacketHeader>() {
                                    continue;
                                }
                                if input.send(BytesMut::from(&buf[..size]).into()).await.is_err() {
                                    break;
                                }
                            }
                            Err(e) => {
                                if let Some(n) = KCP_RECV_ERR_LOG.due() {
                                    log::debug!("KCP recv error x{n} (treated as loss), last: {e}");
                                }
                                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                            }
                        }
                    }
                }
            }
        });
    }""",
    )


def apply_network_discovery_and_lifecycle(changes):
    common_rs = "src/common.rs"
    _replace_once(
        changes,
        common_rs,
        """    #[cfg(not(target_os = "ios"))]
    let mut key = if sync {
        Config::get_option("key")
    } else {""",
        """    if let Some(k) = config::OVERWRITE_SETTINGS
        .read()
        .unwrap()
        .get("key")
        .filter(|v| !v.is_empty())
    {
        return k.clone();
    }
    #[cfg(not(target_os = "ios"))]
    let mut key = if sync {
        Config::get_option("key")
    } else {""",
    )

    _replace_once(
        changes,
        common_rs,
        """    match test_bind_ipv6().await {
        Ok(mut addr) => {
            if let std::net::IpAddr::V6(ip) = addr.ip() {
                if !ip.is_loopback()
                    && !ip.is_unspecified()
                    && !ip.is_multicast()
                    && (ip.segments()[0] & 0xe000) == 0x2000
                {
                    addr.set_port(0);
                    PUBLIC_IPV6_ADDR.lock().unwrap().0 = Some(addr);
                    log::debug!("Found public IPv6 address locally: {}", addr);
                }
            }
        }
        Err(e) => {
            PUBLIC_IPV6_ADDR.lock().unwrap().0 = None;
            log::debug!("Failed to bind IPv6 socket: {}", e);
            return None;
        }
    }""",
        """    match test_bind_ipv6().await {
        Ok(mut addr) => {
            if let std::net::IpAddr::V6(ip) = addr.ip() {
                if !ip.is_loopback()
                    && !ip.is_unspecified()
                    && !ip.is_multicast()
                    && (ip.segments()[0] & 0xe000) == 0x2000
                {
                    addr.set_port(0);
                    PUBLIC_IPV6_ADDR.lock().unwrap().0 = Some(addr);
                    log::debug!("Found public IPv6 address locally: {}", addr);
                    return None;
                }
            }
            PUBLIC_IPV6_ADDR.lock().unwrap().0 = None;
        }
        Err(e) => {
            PUBLIC_IPV6_ADDR.lock().unwrap().0 = None;
            log::debug!("Failed to bind IPv6 socket: {}", e);
            return None;
        }
    }""",
    )
    _replace_once(
        changes,
        common_rs,
        """    Some(tokio::spawn(async {
        use hbb_common::futures::future::{select_ok, FutureExt};
        let tests = hbb_common::webrtc::WebRTCStream::default_stun_servers()
            .into_iter()
            .map(|stun| stun_ipv6_test(stun).boxed())
            .collect::<Vec<_>>();""",
        """    Some(tokio::spawn(async {
        use hbb_common::futures::future::{select_ok, FutureExt};
        let mut servers: Vec<String> = Config::get_option(config::keys::OPTION_ICE_SERVERS)
            .split(',')
            .filter_map(|raw| {
                let raw = raw.trim();
                let bare = raw
                    .strip_prefix("stun://")
                    .or_else(|| raw.strip_prefix("stun:"))?
                    .trim();
                if bare.is_empty() || bare.chars().any(char::is_whitespace) {
                    return None;
                }
                let with_port = if bare
                    .rsplit_once(':')
                    .is_some_and(|(_, port)| port.parse::<u16>().is_ok())
                {
                    bare.to_owned()
                } else if bare.starts_with('[') && bare.ends_with(']') {
                    format!("{bare}:3478")
                } else if bare.contains(':') {
                    format!("[{bare}]:3478")
                } else {
                    format!("{bare}:3478")
                };
                Some(with_port)
            })
            .collect();
        if servers.is_empty() {
            servers = hbb_common::webrtc::WebRTCStream::default_stun_servers();
        }
        let tests = servers
            .into_iter()
            .map(|stun| stun_ipv6_test(stun).boxed())
            .collect::<Vec<_>>();""",
    )

    _replace_once(
        changes,
        "src/rendezvous_mediator.rs",
        """        let mut last_register_resp: Option<Instant> = None;
        let mut last_register_sent: Option<Instant> = None;
        let mut last_dns_check = Instant::now();""",
        """        let mut last_register_resp: Option<Instant> = None;
        let mut last_register_sent: Option<Instant> = None;
        let mut last_dns_check: Option<Instant> = None;""",
    )
    _replace_once(
        changes,
        "src/rendezvous_mediator.rs",
        "                                if last_dns_check.elapsed().as_millis() as i64 > DNS_INTERVAL {",
        "                                if last_dns_check.map_or(true, |t| t.elapsed().as_millis() as i64 > DNS_INTERVAL) {",
    )
    _replace_once(
        changes,
        "src/rendezvous_mediator.rs",
        "                                    last_dns_check = Instant::now();",
        "                                    last_dns_check = Some(Instant::now());",
    )

    _replace_once(
        changes,
        "src/client/io_loop.rs",
        """        allow_err!(peer.send(&msg).await);
        self.sent_close_reason = true;""",
        """        allow_err!(hbb_common::timeout(2000, peer.send(&msg)).await);
        self.sent_close_reason = true;""",
    )

    ipc_rs = "src/ipc.rs"
    _replace_once(
        changes,
        ipc_rs,
        """    #[cfg(target_os = "windows")]
    PortForwardSessionCount(Option<usize>),""",
        """    #[cfg(any(target_os = "windows", target_os = "linux"))]
    PortForwardSessionCount(Option<usize>),""",
    )
    _replace_once(
        changes,
        ipc_rs,
        """        #[cfg(target_os = "windows")]
        Data::PortForwardSessionCount(c) => match c {""",
        """        #[cfg(any(target_os = "windows", target_os = "linux"))]
        Data::PortForwardSessionCount(c) => match c {""",
    )
    _replace_once(
        changes,
        ipc_rs,
        """    if let Some(err) = last_err {
        Err(err.into())
    } else {
        Ok(0)
    }
}

async fn handle_wayland_screencast_restore_token(""",
        """    if let Some(err) = last_err {
        Err(err.into())
    } else {
        Ok(0)
    }
}

#[cfg(target_os = "linux")]
#[tokio::main(flavor = "current_thread")]
pub async fn get_port_forward_session_count_linux() -> ResultType<usize> {
    let timeout_ms = 500;
    let effective_uid = unsafe { hbb_common::libc::geteuid() as u32 };
    let mut total = 0usize;
    for candidate_uid in terminal_count_candidate_uids(effective_uid) {
        let socket_path = Config::ipc_path_for_uid(candidate_uid, "");
        let Ok(Ok(connection)) = timeout(timeout_ms, Endpoint::connect(&socket_path)).await else {
            continue;
        };
        let mut ipc_conn = ConnectionTmpl::new(connection);
        if ipc_conn.send(&Data::PortForwardSessionCount(None)).await.is_err() {
            continue;
        }
        if let Ok(Some(Data::PortForwardSessionCount(Some(count)))) =
            ipc_conn.next_timeout(timeout_ms).await
        {
            total = total.saturating_add(count);
        }
    }
    Ok(total)
}

async fn handle_wayland_screencast_restore_token(""",
    )
    _replace_once(
        changes,
        "src/platform/linux.rs",
        """        let terminal_session_count = crate::ipc::get_terminal_session_count().unwrap_or(0);
        if terminal_session_count > 0 {""",
        """        let terminal_session_count = crate::ipc::get_terminal_session_count().unwrap_or(0);
        let port_forward_count = crate::ipc::get_port_forward_session_count_linux().unwrap_or(0);
        if terminal_session_count > 0 || port_forward_count > 0 {""",
    )


def apply_ui_cleanup(changes):
    desktop = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    _replace_once(
        changes,
        desktop,
        """  List<Widget> webrtcOptions(BuildContext context) {
    final stored = bind.mainGetLocalOption(key: kOptionRelayFallbackDelay);
    final controller = TextEditingController(text: stored);
    // What the field holds against what is saved. Apply is offered only while
    // the two differ, so an untouched field shows no button at all, and neither
    // does one typed back to its saved value or cleared when nothing was saved
    // — the state an "edited" flag alone would still call dirty.
    final typed = RxString(stored);
    final saved = RxString(stored);
    return [
      _OptionCheckBox(
        context,
        'Enable WebRTC P2P connection',
        kOptionEnableWebrtc,
        isServer: false,
        update: (_) => setState(() {}),
      ),
      () {
        final enabled = mainGetLocalBoolOptionSync(kOptionEnableWebrtc);
        final isOptFixed = isOptionFixed(kOptionRelayFallbackDelay);
        return Offstage(
          offstage: !enabled,
          child: Tooltip(
            message: translate('relay-fallback-delay-tip'),
            child: _SubLabeledWidget(
              context,
              'Relay fallback delay in seconds',
              Row(children: [
                SizedBox(
                  width: 95,
                  child: TextField(
                    controller: controller,
                    enabled: enabled && !isOptFixed,
                    onChanged: (v) => typed.value = v,
                    inputFormatters: [
                      // Seconds, at most one decimal. Clearing the field is
                      // allowed and restores the built-in default.
                      FilteringTextInputFormatter.allow(
                          RegExp(r'^([0-9]|[1-9][0-9])(\\.[0-9]?)?$')),
                    ],
                    decoration: const InputDecoration(
                      hintText: '2.5',
                      contentPadding:
                          EdgeInsets.symmetric(vertical: 12, horizontal: 12),
                    ),
                  ).workaroundFreezeLinuxMint().marginOnly(right: 15),
                ),
                Obx(() => Offstage(
                      offstage: typed.value.trim() == saved.value.trim(),
                      child: ElevatedButton(
                        onPressed: enabled &&
                                !isOptFixed &&
                                !typed.value.trim().endsWith('.') &&
                                double.tryParse(typed.value.trim()) != 0
                            ? () async {
                                final v = controller.text.trim();
                                await bind.mainSetLocalOption(
                                    key: kOptionRelayFallbackDelay, value: v);
                                if (controller.text != v) controller.text = v;
                                typed.value = v;
                                saved.value = v;
                              }
                            : null,
                        child: Text(translate('Apply')),
                      ),
                    ))
              ]),
              enabled: enabled && !isOptFixed,
            ),
          ),
        );
      }(),
    ];
  }
}""",
        """  List<Widget> webrtcOptions(BuildContext context) {
    return const [_WebRtcOptions()];
  }
}

class _WebRtcOptions extends StatefulWidget {
  const _WebRtcOptions({Key? key}) : super(key: key);

  @override
  State<_WebRtcOptions> createState() => _WebRtcOptionsState();
}

class _WebRtcOptionsState extends State<_WebRtcOptions> {
  late final TextEditingController _controller;
  late final RxString _typed;
  late final RxString _saved;

  @override
  void initState() {
    super.initState();
    final stored = bind.mainGetLocalOption(key: kOptionRelayFallbackDelay);
    _controller = TextEditingController(text: stored);
    _typed = RxString(stored);
    _saved = RxString(stored);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final enabled = mainGetLocalBoolOptionSync(kOptionEnableWebrtc);
    final isOptFixed = isOptionFixed(kOptionRelayFallbackDelay);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _OptionCheckBox(
          context,
          'Enable WebRTC P2P connection',
          kOptionEnableWebrtc,
          isServer: false,
          update: (_) => setState(() {}),
        ),
        Offstage(
          offstage: !enabled,
          child: Tooltip(
            message: translate('relay-fallback-delay-tip'),
            child: _SubLabeledWidget(
              context,
              'Relay fallback delay in seconds',
              Row(children: [
                SizedBox(
                  width: 95,
                  child: TextField(
                    controller: _controller,
                    enabled: enabled && !isOptFixed,
                    onChanged: (v) => _typed.value = v,
                    inputFormatters: [
                      FilteringTextInputFormatter.allow(
                          RegExp(r'^([0-9]|[1-9][0-9])(\\.[0-9]?)?$')),
                    ],
                    decoration: const InputDecoration(
                      hintText: '2.5',
                      contentPadding:
                          EdgeInsets.symmetric(vertical: 12, horizontal: 12),
                    ),
                  ).workaroundFreezeLinuxMint().marginOnly(right: 15),
                ),
                Obx(() => Offstage(
                      offstage: _typed.value.trim() == _saved.value.trim(),
                      child: ElevatedButton(
                        onPressed: enabled &&
                                !isOptFixed &&
                                !_typed.value.trim().endsWith('.') &&
                                double.tryParse(_typed.value.trim()) != 0
                            ? () async {
                                final v = _controller.text.trim();
                                await bind.mainSetLocalOption(
                                    key: kOptionRelayFallbackDelay, value: v);
                                if (!mounted) return;
                                if (_controller.text != v) _controller.text = v;
                                _typed.value = v;
                                _saved.value = v;
                              }
                            : null,
                        child: Text(translate('Apply')),
                      ),
                    ))
              ]),
              enabled: enabled && !isOptFixed,
            ),
          ),
        ),
      ],
    );
  }
}""",
    )


def apply(changes):
    apply_port_forward_mux(changes)
    apply_port_forward_lifetime(changes)
    apply_video_service_sync_channel(changes)
    apply_webrtc_and_ice_bridge(changes)
    apply_transport_hotpath(changes)
    apply_network_discovery_and_lifecycle(changes)
    apply_ui_cleanup(changes)
