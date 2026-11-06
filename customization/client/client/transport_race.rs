use hbb_common::{
    anyhow::anyhow,
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

#[cfg(test)]
mod tests {
    use super::*;
    use hbb_common::futures::FutureExt;

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
}
