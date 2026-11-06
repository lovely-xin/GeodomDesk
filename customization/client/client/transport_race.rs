use hbb_common::{
    anyhow::{anyhow, Error},
    futures::{future::BoxFuture, stream::FuturesUnordered, StreamExt},
    tokio,
    tokio::time::{sleep, Duration, Instant},
    ResultType,
};
use std::{future::Future, sync::OnceLock};

const CONNECTION_SETUP_TIMEOUT: Duration = Duration::from_secs(30);
const RELAY_FALLBACK_RESERVE: Duration = Duration::from_secs(6);

/// A connection has one overall deadline and one direct-preference window.
pub(super) struct ConnectionDeadline {
    end: Instant,
    window: Duration,
    until: OnceLock<Instant>,
}

impl ConnectionDeadline {
    pub(super) fn new(window_ms: u64) -> Self {
        Self {
            end: Instant::now() + CONNECTION_SETUP_TIMEOUT,
            window: Duration::from_millis(window_ms),
            until: OnceLock::new(),
        }
    }

    pub(super) async fn run<T>(
        &self,
        attempt: impl Future<Output = ResultType<T>>,
    ) -> ResultType<T> {
        tokio::time::timeout_at(self.end, attempt)
            .await
            .map_err(|_| anyhow!("Connection establishment timed out"))?
    }

    pub(super) async fn before_relay_reserve<T>(
        &self,
        direct: impl Future<Output = T>,
    ) -> Result<T, tokio::time::error::Elapsed> {
        tokio::time::timeout_at(self.end - RELAY_FALLBACK_RESERVE, direct).await
    }

    pub(super) async fn wait_for_direct<T>(&self, direct: impl Future<Output = T>) -> Option<T> {
        tokio::time::timeout(self.window, direct).await.ok()
    }

    fn when_relay_is_ready(&self) -> Instant {
        *self
            .until
            .get_or_init(|| (Instant::now() + self.window).min(self.end))
    }

    pub(super) async fn race<'a, T: 'a>(
        &self,
        preferred: BoxFuture<'a, ResultType<T>>,
        alternatives: Vec<BoxFuture<'a, ResultType<T>>>,
        is_direct: impl Fn(&T) -> bool,
    ) -> ResultType<T> {
        self.race_with_standby(preferred, alternatives, is_direct)
            .await
            .map(|(winner, _)| winner)
    }

    pub(super) async fn race_with_standby<'a, T: 'a>(
        &self,
        preferred: BoxFuture<'a, ResultType<T>>,
        alternatives: Vec<BoxFuture<'a, ResultType<T>>>,
        is_direct: impl Fn(&T) -> bool,
    ) -> ResultType<(T, Option<T>)> {
        self.race_routes(preferred, alternatives, is_direct, false)
            .await
    }

    pub(super) async fn race_authorized<'a, T: 'a>(
        &self,
        direct: BoxFuture<'a, ResultType<T>>,
        relay: BoxFuture<'a, ResultType<T>>,
        is_direct: impl Fn(&T) -> bool,
    ) -> ResultType<T> {
        self.race_routes(direct, vec![relay], is_direct, true)
            .await
            .map(|(winner, _)| winner)
    }

    async fn race_routes<'a, T: 'a>(
        &self,
        preferred: BoxFuture<'a, ResultType<T>>,
        alternatives: Vec<BoxFuture<'a, ResultType<T>>>,
        is_direct: impl Fn(&T) -> bool,
        stop_on_login_refusal: bool,
    ) -> ResultType<(T, Option<T>)> {
        let mut attempts = FuturesUnordered::new();
        attempts.push(connect(preferred, true));
        for alternative in alternatives {
            attempts.push(connect(alternative, false));
        }

        let wait = sleep(Duration::ZERO);
        tokio::pin!(wait);
        let mut relay = None;
        let mut secondary = None;
        let mut failure = None;
        let mut login_required = None;

        while !attempts.is_empty() {
            tokio::select! {
                biased;
                _ = &mut wait, if relay.is_some() => break,
                Some((preferred, result)) = attempts.next() => match result {
                    Ok(connection) if is_direct(&connection) => return Ok((connection, relay)),
                    Ok(connection) if relay.is_none() => {
                        wait.as_mut().reset(self.when_relay_is_ready());
                        relay = Some(connection);
                    }
                    // A preferred relay may replace the held one without extending the deadline.
                    Ok(connection) if preferred => secondary = relay.replace(connection),
                    Ok(connection) if secondary.is_none() => secondary = Some(connection),
                    Ok(_) => {}
                    Err(err) => {
                        if crate::geodomdesk_auth::requires_login(&err.to_string()) {
                            if stop_on_login_refusal {
                                return Err(err);
                            }
                            login_required = Some(err);
                        } else {
                            failure = Some(err);
                        }
                    }
                },
            }
        }

        relay.map(|winner| (winner, secondary)).ok_or_else(|| {
            login_required
                .or(failure)
                .unwrap_or_else(|| anyhow!("No connection attempt completed"))
        })
    }
}

#[cfg(test)]
pub(super) async fn race_transports_prefer_direct<'a, T: 'a>(
    preferred: BoxFuture<'a, ResultType<T>>,
    alternatives: Vec<BoxFuture<'a, ResultType<T>>>,
    window_ms: u64,
    is_direct: impl Fn(&T) -> bool,
) -> ResultType<T> {
    ConnectionDeadline::new(window_ms)
        .race(preferred, alternatives, is_direct)
        .await
}

async fn connect<T>(
    attempt: BoxFuture<'_, ResultType<T>>,
    preferred: bool,
) -> (bool, ResultType<T>) {
    (preferred, attempt.await)
}

/// Prepare a control socket in parallel. A completed decision never waits for
/// the dial, and an idle socket is dropped when its preparation window expires.
pub(super) async fn prepare_during<D, T>(
    decision: impl Future<Output = D>,
    prepare: impl Future<Output = ResultType<T>>,
    max_idle: Duration,
) -> (D, Option<T>) {
    tokio::pin!(decision, prepare);
    let prepared = tokio::select! {
        biased;
        result = &mut decision => return (result, None),
        result = &mut prepare => result.ok(),
    };
    if let Some(socket) = prepared {
        if let Ok(result) = tokio::time::timeout(max_idle, &mut decision).await {
            return (result, Some(socket));
        }
    }
    (decision.await, None)
}

/// Prepare a socket while waiting for approval. Only the caller can join the
/// approved pairing. A refusal returns immediately even if dialing is blocked.
pub(super) async fn prepare_while_authorizing<A, T>(
    approval: impl Future<Output = ResultType<Option<A>>>,
    prepare: impl Future<Output = ResultType<T>>,
    max_idle: Duration,
) -> ResultType<Option<(A, Option<T>)>> {
    tokio::pin!(approval, prepare);
    let prepared = tokio::select! {
        biased;
        decision = &mut approval => {
            return match decision? {
                Some(approved) => Ok(Some((approved, Some(prepare.await?)))),
                None => Ok(None),
            };
        }
        socket = &mut prepare => socket.ok(),
    };
    if let Some(socket) = prepared {
        if let Ok(decision) = tokio::time::timeout(max_idle, &mut approval).await {
            return Ok(decision?.map(|approved| (approved, Some(socket))));
        }
        // A slow authorization must not keep an idle relay slot occupied.
    }
    Ok(approval.await?.map(|approved| (approved, None)))
}

pub(super) fn can_retry_handshake(transport: &str, direct: bool, error: &Error) -> bool {
    if transport == "WebRTC" {
        // The new relay must complete its own peer identity handshake.
        return true;
    }
    use std::io::{Error as IoError, ErrorKind};
    direct
        && matches!(transport, "TCP" | "UDP" | "IPv6")
        && (error.is::<tokio::time::error::Elapsed>()
            || error.downcast_ref::<IoError>().is_some_and(|error| {
                matches!(
                    error.kind(),
                    ErrorKind::UnexpectedEof
                        | ErrorKind::TimedOut
                        | ErrorKind::ConnectionReset
                        | ErrorKind::ConnectionAborted
                        | ErrorKind::BrokenPipe
                        | ErrorKind::NotConnected
                )
            }))
}

pub(super) fn fallback_failure(transport: &str, handshake: Error, relay: Error) -> Error {
    if crate::geodomdesk_auth::requires_login(&relay.to_string()) {
        return relay;
    }
    anyhow!("{transport} secure failed ({handshake}); relay fallback also failed: {relay}")
}

#[cfg(test)]
mod tests {
    use super::super::{sign, Client, IdPk, Message as PeerMessage, SignedId, Stream};
    use super::*;
    use hbb_common::{futures::FutureExt, protobuf::Message};

    #[tokio::test]
    async fn establishment_deadline_also_bounds_the_last_fallback() {
        let mut deadline = ConnectionDeadline::new(400);
        deadline.end = Instant::now() + Duration::from_millis(20);
        let error = deadline
            .run(std::future::pending::<ResultType<()>>())
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), "Connection establishment timed out");
    }

    #[tokio::test]
    async fn direct_attempt_releases_the_relay_budget() {
        let mut deadline = ConnectionDeadline::new(400);
        deadline.end = Instant::now() + RELAY_FALLBACK_RESERVE + Duration::from_millis(20);
        assert!(deadline
            .before_relay_reserve(std::future::pending::<()>())
            .await
            .is_err());
        assert!(deadline.end > Instant::now());
    }

    #[tokio::test]
    async fn direct_success_never_waits_for_the_broker_preconnect() {
        let result = prepare_during(
            async { "direct" },
            std::future::pending::<ResultType<()>>(),
            Duration::from_secs(1),
        )
        .await;
        assert_eq!(result, ("direct", None));
    }

    #[tokio::test]
    async fn slow_direct_attempt_can_reuse_a_preconnected_broker() {
        let (connected, wait_for_broker) = tokio::sync::oneshot::channel();
        let result = prepare_during(
            async {
                wait_for_broker.await.unwrap();
                "use relay"
            },
            async {
                connected.send(()).unwrap();
                Ok("broker")
            },
            Duration::from_secs(1),
        )
        .await;
        assert_eq!(result, ("use relay", Some("broker")));
    }

    #[tokio::test]
    async fn expired_broker_is_closed_while_direct_is_still_running() {
        let (broker, closed) = tokio::sync::oneshot::channel::<()>();
        let (_, prepared) = tokio::time::timeout(
            Duration::from_secs(1),
            prepare_during(
                async { assert!(closed.await.is_err()) },
                async { Ok(broker) },
                Duration::from_millis(20),
            ),
        )
        .await
        .unwrap();
        assert!(prepared.is_none());
    }

    #[tokio::test]
    async fn slow_direct_attempt_survives_a_failed_relay_hedge() {
        let deadline = ConnectionDeadline::new(20);
        let (fallback_started, wait_for_fallback) = tokio::sync::oneshot::channel();
        let direct = async {
            wait_for_fallback.await?;
            Ok("direct")
        };
        tokio::pin!(direct);
        assert!(deadline.wait_for_direct(direct.as_mut()).await.is_none());
        let relay = async {
            fallback_started.send(()).unwrap();
            Err(anyhow!("relay unreachable"))
        };
        let winner = deadline
            .race_authorized(direct.boxed(), relay.boxed(), |r| *r == "direct")
            .await;
        assert_eq!(winner.unwrap(), "direct");
    }

    #[tokio::test]
    async fn an_explicit_relay_login_refusal_stops_the_hedged_connection() {
        let deadline = ConnectionDeadline::new(400);
        let reason = "Please log in before starting a remote connection";
        let result = deadline
            .race_authorized(
                std::future::pending::<ResultType<()>>().boxed(),
                async { Err(anyhow!(reason)) }.boxed(),
                |_| true,
            )
            .await;
        assert_eq!(result.unwrap_err().to_string(), reason);
    }

    #[tokio::test]
    async fn nested_races_keep_the_original_relay_deadline() {
        let deadline = ConnectionDeadline::new(400);
        let winner = deadline
            .race(async { Ok("relay") }.boxed(), vec![], |_| false)
            .await
            .unwrap();
        assert_eq!(winner, "relay");
        let original = *deadline.until.get().unwrap();
        sleep(Duration::from_millis(2)).await;
        let winner = deadline
            .race(async { Ok("other-relay") }.boxed(), vec![], |_| false)
            .await
            .unwrap();
        assert_eq!(winner, "other-relay");
        assert_eq!(deadline.until.get(), Some(&original));
    }

    #[tokio::test]
    async fn keeps_a_ready_relay_until_the_direct_handshake_can_finish() {
        let (relay_ready, wait_for_relay) = tokio::sync::oneshot::channel();
        let direct = async {
            wait_for_relay.await?;
            Ok("direct")
        }
        .boxed();
        let relay = async {
            let _ = relay_ready.send(());
            Ok("relay")
        }
        .boxed();
        let (winner, standby) = ConnectionDeadline::new(400)
            .race_with_standby(direct, vec![relay], |route| *route == "direct")
            .await
            .unwrap();
        assert_eq!(winner, "direct");
        assert_eq!(standby, Some("relay"));
    }

    #[tokio::test]
    async fn opens_a_relay_socket_while_waiting_for_approval() {
        let (dialing, started) = tokio::sync::oneshot::channel();
        let (approved, allowed) = tokio::sync::oneshot::channel();
        let approval = async {
            started.await?;
            let _ = approved.send(());
            Ok(Some("approved"))
        };
        let prepare = async {
            let _ = dialing.send(());
            allowed.await?;
            Ok("socket")
        };
        let (decision, socket) = tokio::time::timeout(
            Duration::from_secs(1),
            prepare_while_authorizing(approval, prepare, Duration::from_secs(1)),
        )
        .await
        .unwrap()
        .unwrap()
        .unwrap();
        assert_eq!(decision, "approved");
        assert_eq!(socket.unwrap(), "socket");
    }

    #[tokio::test]
    async fn refusal_does_not_wait_for_a_blocked_relay_socket() {
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            prepare_while_authorizing(
                async { Err::<Option<()>, _>(anyhow!("Account sign-in required")) },
                std::future::pending::<ResultType<()>>(),
                Duration::from_secs(1),
            ),
        )
        .await
        .unwrap();
        assert_eq!(result.unwrap_err().to_string(), "Account sign-in required");
    }

    #[tokio::test]
    async fn dial_failure_after_approval_is_returned_without_another_dial() {
        let result = prepare_while_authorizing(
            async { Ok(Some(())) },
            async { Err::<(), _>(anyhow!("Relay unreachable")) },
            Duration::from_secs(1),
        )
        .await;
        assert_eq!(result.unwrap_err().to_string(), "Relay unreachable");
    }

    #[tokio::test]
    async fn dial_failure_before_approval_leaves_the_authorized_dial_available() {
        let (dial_failed, wait_for_failure) = tokio::sync::oneshot::channel();
        let result = prepare_while_authorizing(
            async {
                wait_for_failure.await?;
                Ok(Some(()))
            },
            async {
                let _ = dial_failed.send(());
                Err::<(), _>(anyhow!("Relay unreachable"))
            },
            Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(result, Some(((), None)));
    }

    #[tokio::test]
    async fn an_idle_preconnection_is_released_before_slow_approval() {
        let (socket_dropped, confirm_dropped) = tokio::sync::oneshot::channel();
        struct Socket(Option<tokio::sync::oneshot::Sender<()>>);
        impl Drop for Socket {
            fn drop(&mut self) {
                if let Some(notify) = self.0.take() {
                    let _ = notify.send(());
                }
            }
        }
        let approval = async {
            confirm_dropped.await?;
            Ok(Some("approved"))
        };
        let prepare = async { Ok(Socket(Some(socket_dropped))) };
        let (decision, socket) = tokio::time::timeout(
            Duration::from_secs(1),
            prepare_while_authorizing(approval, prepare, Duration::from_millis(20)),
        )
        .await
        .unwrap()
        .unwrap()
        .unwrap();
        assert_eq!(decision, "approved");
        assert!(socket.is_none());
    }

    #[tokio::test]
    async fn native_udp_wins_over_a_ready_relay_with_webrtc_absent() {
        let udp = async {
            sleep(Duration::from_millis(25)).await;
            Ok("udp")
        }
        .boxed();
        let relay = async { Ok("relay") }.boxed();
        let result = race_transports_prefer_direct(udp, vec![relay], 400, |route| *route == "udp")
            .await
            .unwrap();
        assert_eq!(result, "udp");
    }

    #[tokio::test]
    async fn preserves_the_server_login_refusal_when_another_transport_fails_later() {
        let reason = "Please log in before starting a remote connection";
        let refusal = async { Err::<(), _>(anyhow!(reason)) }.boxed();
        let transport = async {
            sleep(Duration::from_millis(25)).await;
            Err(anyhow!("transport timeout"))
        }
        .boxed();
        let error = race_transports_prefer_direct(refusal, vec![transport], 400, |_| false)
            .await
            .unwrap_err();
        assert_eq!(error.to_string(), reason);
    }

    #[test]
    fn fallback_preserves_the_account_refusal_after_a_recoverable_handshake_failure() {
        let reason = "Please log in before starting a remote connection";
        let reset =
            std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "Reset by the peer").into();
        assert!(can_retry_handshake("TCP", true, &reset));
        assert!(!can_retry_handshake("Relay", false, &reset));
        assert!(!can_retry_handshake(
            "TCP",
            true,
            &anyhow!("Peer identity mismatch")
        ));
        assert_eq!(
            fallback_failure("TCP", reset, anyhow!(reason)).to_string(),
            reason
        );
    }

    #[tokio::test]
    async fn a_peer_that_closes_during_its_handshake_can_fall_back() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        drop(accepted.unwrap());
        let mut stream = Stream::Tcp(hbb_common::tcp::FramedStream::from(
            client.unwrap(),
            endpoint,
        ));
        let (public, secret) = sign::gen_keypair();
        let id = IdPk {
            id: "test-device".into(),
            pk: vec![1; 32].into(),
            ..Default::default()
        };
        let signed = sign::sign(&id.write_to_bytes().unwrap(), &secret);
        let error = Client::secure_connection(
            "test-device",
            signed,
            &crate::encode64(public.0),
            &mut stream,
        )
        .await
        .unwrap_err();
        assert_eq!(error.to_string(), "Reset by the peer");
        assert!(can_retry_handshake("TCP", true, &error));
    }

    #[tokio::test]
    async fn invalid_signature_is_distinct_from_legacy_unsigned_handshake() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (_peer, _) = accepted.unwrap();
        let mut stream = Stream::Tcp(hbb_common::tcp::FramedStream::from(
            client.unwrap(),
            endpoint,
        ));
        let (root, _) = sign::gen_keypair();
        let key = crate::encode64(root.0);
        let result = Client::secure_connection("test-device", vec![7; 32], &key, &mut stream).await;
        assert_eq!(
            result.unwrap_err().to_string(),
            "Invalid signed peer identity from rendezvous server"
        );
        let unsigned =
            Client::secure_connection("test-device", Vec::new(), &key, &mut stream).await;
        assert!(unsigned.unwrap().is_none());
    }

    #[tokio::test]
    async fn signed_peer_cannot_downgrade_by_sending_an_empty_handshake() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (peer, peer_addr) = accepted.unwrap();
        let mut stream = Stream::Tcp(hbb_common::tcp::FramedStream::from(
            client.unwrap(),
            endpoint,
        ));
        let mut peer = Stream::Tcp(hbb_common::tcp::FramedStream::from(peer, peer_addr));
        let (root, secret) = sign::gen_keypair();
        let identity = IdPk {
            id: "test-device".into(),
            pk: vec![1; 32].into(),
            ..Default::default()
        };
        let signed = sign::sign(&identity.write_to_bytes().unwrap(), &secret);
        peer.send(&PeerMessage::new()).await.unwrap();
        let result =
            Client::secure_connection("test-device", signed, &crate::encode64(root.0), &mut stream)
                .await;
        assert_eq!(
            result.unwrap_err().to_string(),
            "handshake received an unexpected message type"
        );
    }

    #[tokio::test]
    async fn accepts_a_valid_legacy_signed_peer_without_new_exchange_fields() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (client, accepted) =
            tokio::join!(tokio::net::TcpStream::connect(endpoint), listener.accept());
        let (peer, peer_addr) = accepted.unwrap();
        let mut stream = Stream::Tcp(hbb_common::tcp::FramedStream::from(
            client.unwrap(),
            endpoint,
        ));
        let mut peer = Stream::Tcp(hbb_common::tcp::FramedStream::from(peer, peer_addr));
        let (root, root_secret) = sign::gen_keypair();
        let (peer_key, peer_secret) = sign::gen_keypair();
        let (exchange_key, _) = hbb_common::sodiumoxide::crypto::box_::gen_keypair();
        let identity = IdPk {
            id: "test-device".into(),
            pk: peer_key.0.to_vec().into(),
            ..Default::default()
        };
        let signed = sign::sign(&identity.write_to_bytes().unwrap(), &root_secret);
        let proof = IdPk {
            id: "test-device".into(),
            pk: exchange_key.0.to_vec().into(),
            ..Default::default()
        };
        let mut message = PeerMessage::new();
        message.set_signed_id(SignedId {
            id: sign::sign(&proof.write_to_bytes().unwrap(), &peer_secret).into(),
            ..Default::default()
        });
        peer.send(&message).await.unwrap();
        let result =
            Client::secure_connection("test-device", signed, &crate::encode64(root.0), &mut stream)
                .await
                .unwrap();
        assert_eq!(result, Some(peer_key.0.to_vec()));
        let key_exchange =
            PeerMessage::parse_from_bytes(&peer.next().await.unwrap().unwrap()).unwrap();
        assert_eq!(key_exchange.public_key().kx_version, 0);
    }
}
