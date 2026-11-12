pub(super) async fn verify_ssh_account(token: &str) -> ResultType<()> {
    if token.is_empty() || token.bytes().any(|byte| !byte.is_ascii_graphic()) {
        return Err(super::stage::wrap(
            anyhow!("GeodomDesk session is missing or invalid. Use --account-user to sign in"),
            "account_auth",
            "not_started",
        ));
    }
    let checked = async {
        let (status, body) = tokio::time::timeout(
            Duration::from_secs(10),
            crate::common::post_terminal_account("currentUser", "{}".into(), Some(token)),
        )
        .await
        .context("GeodomDesk account verification timed out")??;
        verified_profile(status, &body)
    }
    .await;
    checked.map_err(|error| super::stage::wrap(error, "account_auth", "not_started"))
}

fn verified_profile(status: u16, body: &str) -> ResultType<()> {
    if status != 200 {
        bail!("GeodomDesk account could not be verified (HTTP {status}). Sign in again with --account-user");
    }
    let value: serde_json::Value =
        serde_json::from_str(body).context("Invalid GeodomDesk account verification response")?;
    if value.get("error").is_some()
        || !value["name"]
            .as_str()
            .is_some_and(|name| !name.trim().is_empty())
    {
        bail!("GeodomDesk account was rejected. Sign in again with --account-user");
    }
    Ok(())
}

#[cfg(test)]
mod ssh_account_tests {
    use super::{verified_profile, verify_ssh_account};

    #[test]
    fn ssh_requires_an_active_account_response() {
        assert!(verified_profile(200, r#"{"name":"test"}"#).is_ok());
        assert!(verified_profile(401, r#"{"name":"test"}"#).is_err());
        assert!(verified_profile(200, r#"{"name":"test","error":"expired"}"#).is_err());
        assert!(verified_profile(200, r#"{"name":" "}"#).is_err());
        assert!(verified_profile(200, "{}").is_err());
    }

    #[tokio::test]
    async fn absent_account_is_rejected_before_network_access() {
        for token in ["", "foo\r\nbar"] {
            let error = verify_ssh_account(token).await.unwrap_err();
            let (_, phase, state) = super::super::stage::describe(&error);
            assert_eq!((phase, state), ("account_auth", "not_started"));
        }
    }
}
