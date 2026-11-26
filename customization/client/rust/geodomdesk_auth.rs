//! Account authentication failures must wait for an explicit user login.
pub(crate) fn requires_login(message: &str) -> bool {
    let message = message.trim();
    let message = message
        .strip_prefix("Failed to connect via relay server: ")
        .unwrap_or(message);
    matches!(
        message,
        "Please log in before starting a remote connection" | "Invalid account or password"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_server_account_errors() {
        assert!(requires_login(
            "Please log in before starting a remote connection"
        ));
        assert!(requires_login("Invalid account or password"));
        assert!(requires_login("  Invalid account or password\n"));
        assert!(requires_login(
            "Failed to connect via relay server: Please log in before starting a remote connection"
        ));
    }

    #[test]
    fn does_not_confuse_peer_password_or_transport_errors() {
        for error in [
            "Wrong Password",
            "Password Required",
            "Connection reset",
            "Timeout",
            "Failed to connect via relay server: Timeout",
            "Account verification is temporarily unavailable",
            "Too many login attempts; try again later",
        ] {
            assert!(!requires_login(error));
        }
    }
}
