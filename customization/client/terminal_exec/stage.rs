use hbb_common::{anyhow, tokio};
use std::fmt;

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

#[cfg(test)]
mod tests {
    use super::*;
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
