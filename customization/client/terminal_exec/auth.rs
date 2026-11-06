use crate::{
    client::{self, Interface},
    trusted_devices::client::Session,
    ui_interface,
};
use hbb_common::{
    anyhow::{anyhow, Context},
    bail,
    config::LocalConfig,
    tokio, ResultType, Stream,
};
use std::{env, time::Duration};

const ACCOUNT_PASSWORD: &str = "GEODOMDESK_ACCOUNT_PASSWORD";
const DEVICE_PASSWORD: &str = "GEODOMDESK_DEVICE_PASSWORD";
const ADMIN_PASSWORD: &str = "GEODOMDESK_ADMIN_PASSWORD";
const DEVICE_CODE: &str = "GEODOMDESK_DEVICE_CODE";

pub(super) struct Credentials {
    pub(super) username: String,
    pub(super) password: String,
}

fn environment_password(name: &str) -> ResultType<Option<String>> {
    match env::var(name) {
        Ok(value) => Ok(Some(value)),
        Err(env::VarError::NotPresent) => Ok(None),
        Err(env::VarError::NotUnicode(_)) => bail!("{name} must be valid Unicode"),
    }
}

fn prompt_password(name: &str, prompt: &str, non_interactive: bool) -> ResultType<String> {
    if non_interactive {
        bail!("Set {name} for non-interactive use");
    }
    rpassword::prompt_password(prompt).with_context(|| format!("Cannot read password, set {name}"))
}

fn credentials(
    username: String,
    variable: &str,
    prompt: &str,
    non_interactive: bool,
) -> ResultType<Credentials> {
    let username = username.trim();
    if username.is_empty() || username.chars().any(char::is_control) {
        bail!("Invalid login username");
    }
    let password = match environment_password(variable)? {
        Some(password) => password,
        None => prompt_password(variable, prompt, non_interactive)?,
    };
    Ok(Credentials {
        username: username.to_owned(),
        password,
    })
}

pub(super) fn device_password(ask: bool, non_interactive: bool) -> ResultType<String> {
    match environment_password(DEVICE_PASSWORD)? {
        Some(password) => Ok(password),
        None if ask => {
            prompt_password(DEVICE_PASSWORD, "Device access password: ", non_interactive)
        }
        None => Ok(String::new()),
    }
}

pub(super) fn device_code() -> ResultType<String> {
    environment_password(DEVICE_CODE)?
        .filter(|code| !code.is_empty())
        .with_context(|| {
            format!("The target requires two-factor authentication, set {DEVICE_CODE}")
        })
}

pub(super) fn admin_credentials(
    username: Option<String>,
    non_interactive: bool,
) -> ResultType<Option<Credentials>> {
    username
        .map(|name| {
            credentials(
                name,
                ADMIN_PASSWORD,
                "Windows administrator password: ",
                non_interactive,
            )
        })
        .transpose()
}

pub(super) async fn account_token(
    username: Option<String>,
    non_interactive: bool,
) -> ResultType<String> {
    let Some(username) = username else {
        let token = LocalConfig::get_option("access_token");
        if token.is_empty() {
            bail!("Sign in to GeodomDesk first or supply --account-user");
        }
        return Ok(token);
    };
    let account = credentials(
        username,
        ACCOUNT_PASSWORD,
        "GeodomDesk account password: ",
        non_interactive,
    )?;
    let id = tokio::task::spawn_blocking(crate::ipc::get_id).await?;
    let body = serde_json::to_string(&serde_json::json!({
        "username": account.username,
        "password": account.password,
        "id": id,
        "uuid": ui_interface::get_uuid(),
        "autoLogin": false,
        "type": "account",
        "deviceInfo": ui_interface::get_login_device_info(),
    }))?;
    let (status, response) = tokio::time::timeout(
        Duration::from_secs(20),
        crate::common::post_terminal_login(body),
    )
    .await
    .context("GeodomDesk account sign-in timed out")??;
    parse_account_response(status, &response)
}

fn parse_account_response(status: u16, response: &str) -> ResultType<String> {
    #[derive(serde_derive::Deserialize)]
    struct Reply {
        r#type: Option<String>,
        access_token: Option<String>,
        error: Option<serde_json::Value>,
    }
    if status != 200 {
        bail!("GeodomDesk account sign-in failed (HTTP {status})");
    }
    let reply: Reply =
        serde_json::from_str(response).context("Invalid GeodomDesk sign-in response")?;
    if reply.error.is_some() {
        bail!("GeodomDesk account sign-in was rejected");
    }
    if reply.r#type.as_deref() != Some("access_token") {
        bail!(
            "GeodomDesk account sign-in requires additional verification, sign in in the app first"
        );
    }
    reply
        .access_token
        .filter(|token| !token.is_empty())
        .context("GeodomDesk sign-in returned no token")
}

pub(super) async fn send_admin_login(
    interface: &impl Interface,
    admin: Credentials,
    trust: &mut Session,
    stream: &mut Stream,
) -> ResultType<()> {
    let login = interface.get_lch();
    if !login
        .read()
        .map_err(|_| anyhow!("Login state unavailable"))?
        .has_terminal_device_password()
    {
        bail!("Administrator terminal login requires a device access password, use --ask-device-password or set {DEVICE_PASSWORD}");
    }
    client::handle_login_from_ui(
        login,
        admin.username,
        admin.password,
        String::new(),
        false,
        trust,
        stream,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::parse_account_response;

    #[test]
    fn account_login_accepts_only_a_successful_token_response() {
        let valid = r#"{"type":"access_token","access_token":"test-token"}"#;
        assert_eq!(parse_account_response(200, valid).unwrap(), "test-token");
        assert!(parse_account_response(401, valid).is_err());
        assert!(
            parse_account_response(200, r#"{"type":"tfa_check","access_token":"test-token"}"#)
                .is_err()
        );
        assert!(parse_account_response(
            200,
            r#"{"type":"access_token","access_token":"test-token","error":"invalid"}"#
        )
        .is_err());
        assert!(
            parse_account_response(200, r#"{"type":"access_token","access_token":""}"#).is_err()
        );
    }
}
