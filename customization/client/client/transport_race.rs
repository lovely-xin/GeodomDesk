use hbb_common::{
    anyhow::{anyhow, Error},
    futures::{future::BoxFuture, stream::FuturesUnordered, StreamExt},
    tokio,
    tokio::time::{sleep, Duration, Instant},
    ResultType,
};

/// Keep a ready relay for a bounded time while direct transports are still racing.
pub(super) async fn race_transports_prefer_direct<'a, T: 'a>(
    preferred: BoxFuture<'a, ResultType<T>>,
    alternatives: Vec<BoxFuture<'a, ResultType<T>>>,
    window_ms: u64,
    is_direct: impl Fn(&T) -> bool,
) -> ResultType<T> {
    let mut attempts = FuturesUnordered::new();
    attempts.push(connect(preferred, true));
    for alternative in alternatives {
        attempts.push(connect(alternative, false));
    }

    let wait = sleep(Duration::ZERO);
    tokio::pin!(wait);
    let mut relay = None;
    let mut failure = None;
    let mut login_required = None;

    while !attempts.is_empty() {
        tokio::select! {
            biased;
            _ = &mut wait, if relay.is_some() => break,
            Some((preferred, result)) = attempts.next() => match result {
                Ok(connection) if is_direct(&connection) => return Ok(connection),
                Ok(connection) if relay.is_none() => {
                    wait.as_mut().reset(Instant::now() + Duration::from_millis(window_ms));
                    relay = Some(connection);
                }
                // A preferred relay may replace the held one without extending the deadline.
                Ok(connection) if preferred => relay = Some(connection),
                Ok(_) => {}
                Err(err) => {
                    if crate::geodomdesk_auth::requires_login(&err.to_string()) {
                        login_required = Some(err);
                    } else {
                        failure = Some(err);
                    }
                }
            },
        }
    }

    relay.ok_or_else(|| {
        login_required
            .or(failure)
            .unwrap_or_else(|| anyhow!("No connection attempt completed"))
    })
}

async fn connect<T>(
    attempt: BoxFuture<'_, ResultType<T>>,
    preferred: bool,
) -> (bool, ResultType<T>) {
    (preferred, attempt.await)
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
    use super::super::{sign, Client, IdPk, Stream};
    use super::*;
    use hbb_common::{futures::FutureExt, protobuf::Message};

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
}
