//! Account authentication failures must wait for an explicit user login.
pub(crate) fn requires_login(message: &str) -> bool {
    matches!(
        message.trim(),
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
    }

    #[test]
    fn does_not_confuse_peer_password_or_transport_errors() {
        for error in [
            "Wrong Password",
            "Password Required",
            "Connection reset",
            "Timeout",
            "Account verification is temporarily unavailable",
            "Too many login attempts; try again later",
        ] {
            assert!(!requires_login(error));
        }
    }
}
