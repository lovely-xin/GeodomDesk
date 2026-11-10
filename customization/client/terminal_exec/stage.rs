use hbb_common::{
    anyhow::{self, Context},
    tokio, ResultType,
};
use std::{
    fmt,
    future::Future,
    io,
    time::{Duration, Instant},
};

#[derive(Debug)]
pub(super) struct Stage {
    pub phase: &'static str,
    pub state: &'static str,
}
#[derive(Debug)]
pub(super) struct ConnectionId(pub String);
impl fmt::Display for ConnectionId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "connection {}", self.0)
    }
}
impl fmt::Display for Stage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} failed", self.phase)
    }
}
pub(super) fn wrap(
    error: anyhow::Error,
    phase: &'static str,
    state: &'static str,
) -> anyhow::Error {
    error.context(Stage { phase, state })
}
pub(super) fn describe(error: &anyhow::Error) -> (String, &'static str, &'static str) {
    let stage = error.downcast_ref::<Stage>();
    let phase = stage.map(|s| s.phase).unwrap_or("request");
    let state = stage.map(|s| s.state).unwrap_or("not_started");
    let timeout = error
        .chain()
        .any(|cause| cause.is::<tokio::time::error::Elapsed>());
    (
        format!("{phase}_{}", if timeout { "timeout" } else { "failed" }),
        phase,
        state,
    )
}

#[derive(Debug)]
pub(super) struct PeerDisconnected;
impl fmt::Display for PeerDisconnected {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Remote device closed the connection")
    }
}

#[derive(Debug)]
struct ConnectAttemptTimeout;
impl fmt::Display for ConnectAttemptTimeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Connection attempt timed out")
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub(super) struct SetupError {
    phase: &'static str,
    error: String,
    elapsed_ms: u128,
}
impl SetupError {
    pub fn new(error: &anyhow::Error, elapsed: Duration) -> Self {
        Self {
            phase: describe(error).1,
            error: format!("{error:#}"),
            elapsed_ms: elapsed.as_millis(),
        }
    }
}
#[derive(Debug)]
pub(super) struct SetupHistory(pub Vec<SetupError>);
impl fmt::Display for SetupHistory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} setup attempt(s) failed", self.0.len())
    }
}

pub(super) fn transient_setup_error(error: &anyhow::Error) -> bool {
    if error.downcast_ref::<Stage>().is_some_and(|stage| {
        stage.state != "not_started" || !matches!(stage.phase, "connect" | "prepare")
    }) {
        return false;
    }
    error.downcast_ref::<PeerDisconnected>().is_some()
        || error.downcast_ref::<ConnectAttemptTimeout>().is_some()
        || error.chain().any(|cause| {
            // The shared client exposes an exhausted rendezvous exchange as this stable message.
            cause
                .to_string()
                .starts_with("Failed to connect via rendezvous server")
                || cause.downcast_ref::<io::Error>().is_some_and(|error| {
                    matches!(
                        error.kind(),
                        io::ErrorKind::ConnectionReset
                            | io::ErrorKind::ConnectionAborted
                            | io::ErrorKind::ConnectionRefused
                            | io::ErrorKind::NotConnected
                            | io::ErrorKind::BrokenPipe
                            | io::ErrorKind::UnexpectedEof
                            | io::ErrorKind::TimedOut
                    )
                })
        })
}

pub(super) async fn connect_with_recovery<T, F, Fut>(
    budget: Duration,
    credential_bound: bool,
    mut connect: F,
) -> ResultType<(T, Vec<SetupError>)>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = ResultType<T>>,
{
    let started = Instant::now();
    let mut last_attempt = started;
    // Explicit credentials and one-time codes get one full-budget authentication attempt.
    let max_attempts = if credential_bound { 1 } else { (budget.as_secs() / 5).clamp(1, 3) as usize };
    let setup_phase = if credential_bound { "authentication" } else { "connect" };
    let mut history = Vec::new();
    let outcome = tokio::time::timeout(budget, async {
        let mut attempt = 0;
        loop {
            attempt += 1;
            let connecting = Instant::now();
            last_attempt = connecting;
            let remaining = budget.saturating_sub(started.elapsed());
            let attempt_budget = remaining / (max_attempts - attempt + 1) as u32;
            let result = tokio::time::timeout(attempt_budget, connect())
                .await
                .context(ConnectAttemptTimeout)
                .and_then(|result| result);
            match result {
                Ok(connection) => return Ok(connection),
                Err(error) => {
                    let error = if error.downcast_ref::<Stage>().is_none() {
                        wrap(error, setup_phase, "not_started")
                    } else { error };
                    let retry = attempt < max_attempts && transient_setup_error(&error);
                    history.push(SetupError::new(&error, connecting.elapsed()));
                    if !retry { return Err(error); }
                    eprintln!("Temporary connection setup failure; retrying before command execution ({attempt}/{max_attempts}): {error:#}");
                    tokio::time::sleep(Duration::from_millis(if attempt == 1 { 200 } else { 500 })).await;
                }
            }
        }
    }).await;
    let total_timeout = outcome.is_err();
    let result = outcome
        .context("Timed out connecting or authenticating")
        .and_then(|result| result);
    match result {
        Ok(connection) => Ok((connection, history)),
        Err(error) => {
            let error = if error.downcast_ref::<Stage>().is_none() {
                wrap(error, setup_phase, "not_started")
            } else {
                error
            };
            if total_timeout || history.is_empty() {
                history.push(SetupError::new(&error, last_attempt.elapsed()));
            }
            Err(error.context(SetupHistory(history)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn explicit_credentials_never_repeat_a_login_after_transport_failure() {
        let calls = std::cell::Cell::new(0);
        let error = connect_with_recovery(Duration::from_secs(30), true, || {
            calls.set(calls.get() + 1);
            std::future::ready(Err::<u8, _>(anyhow::Error::new(io::Error::new(
                io::ErrorKind::ConnectionReset, "lost authentication response",
            ))))
        }).await.unwrap_err();
        assert_eq!(calls.get(), 1);
        assert_eq!(describe(&error).1, "authentication");
        assert!(!transient_setup_error(&error));
    }

    #[tokio::test]
    async fn setup_recovery_preserves_failures_and_succeeds_on_a_new_attempt() {
        let calls = std::cell::Cell::new(0);
        let (value, history) = connect_with_recovery(Duration::from_secs(30), false, || {
            calls.set(calls.get() + 1);
            std::future::ready(if calls.get() < 3 {
                Err(anyhow::Error::new(io::Error::new(
                    io::ErrorKind::ConnectionReset,
                    "test disconnect",
                )))
            } else {
                Ok(7)
            })
        })
        .await
        .unwrap();
        assert_eq!(value, 7);
        assert_eq!(calls.get(), 3);
        assert_eq!(history.len(), 2);
    }

    #[tokio::test]
    async fn setup_recovery_abandons_a_stuck_handshake_before_the_total_budget() {
        let calls = std::cell::Cell::new(0);
        let (value, history) = connect_with_recovery(Duration::from_secs(10), false, || {
            calls.set(calls.get() + 1);
            let attempt = calls.get();
            async move {
                if attempt == 1 {
                    std::future::pending::<ResultType<u8>>().await
                } else {
                    Ok(9)
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(value, 9);
        assert_eq!(calls.get(), 2);
        assert_eq!(history.len(), 1);
    }

    #[tokio::test]
    async fn setup_recovery_never_retries_rejection_unknown_execution_or_an_expired_budget() {
        let calls = std::cell::Cell::new(0);
        let rejected = connect_with_recovery(Duration::from_secs(30), false, || {
            calls.set(calls.get() + 1);
            std::future::ready(Err::<u8, _>(anyhow::anyhow!("Wrong Password")))
        })
        .await
        .unwrap_err();
        assert_eq!(calls.get(), 1);
        assert_eq!(describe(&rejected).2, "not_started");
        let unknown = connect_with_recovery(Duration::from_secs(30), false, || {
            std::future::ready(Err::<u8, _>(wrap(
                anyhow::Error::new(io::Error::new(
                    io::ErrorKind::ConnectionReset,
                    "lost result",
                )),
                "execution",
                "unknown",
            )))
        })
        .await
        .unwrap_err();
        assert_eq!(describe(&unknown).2, "unknown");
        assert_eq!(unknown.downcast_ref::<SetupHistory>().unwrap().0.len(), 1);
        let timeout = connect_with_recovery(Duration::from_millis(10), false, || {
            std::future::pending::<ResultType<u8>>()
        })
        .await
        .unwrap_err();
        assert_eq!(
            describe(&timeout),
            ("connect_timeout".into(), "connect", "not_started")
        );
    }

    #[tokio::test]
    async fn execution_timeout_remains_unknown_through_connection_context() {
        let elapsed = tokio::time::timeout(std::time::Duration::ZERO, std::future::pending::<()>())
            .await
            .unwrap_err();
        let error = wrap(anyhow::Error::new(elapsed), "execution", "unknown")
            .context(ConnectionId("test".into()));
        assert_eq!(
            describe(&error),
            ("execution_timeout".into(), "execution", "unknown")
        );
        let value = super::super::output::failure(None, &error, false, std::time::Duration::ZERO);
        assert_eq!(value["execution_state"], "unknown");
        assert_eq!(value["retry_safe"], false);
        assert_eq!(value["connection_id"], "test");
    }
}
