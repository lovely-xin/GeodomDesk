pub(super) async fn verify_ssh_account(token: &str) -> ResultType<()> {
    if token.is_empty() || token.bytes().any(|byte| !byte.is_ascii_graphic()) {
        return Err(super::stage::wrap(
            anyhow!("GeodomDesk session is missing or invalid. Run account login"),
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
        bail!("GeodomDesk account could not be verified (HTTP {status}). Run account login again");
    }
    let value: serde_json::Value =
        serde_json::from_str(body).context("Invalid GeodomDesk account verification response")?;
    if value.get("error").is_some()
        || !value["name"]
            .as_str()
            .is_some_and(|name| !name.trim().is_empty())
    {
        bail!("GeodomDesk account was rejected. Run account login again");
    }
    Ok(())
}

pub(super) async fn manage_account(mut args: impl Iterator<Item = String>) -> ResultType<i32> {
    match args.next().as_deref() {
        Some("login") => {
            let mut username = None;
            let mut non_interactive = false;
            while let Some(flag) = args.next() {
                match flag.as_str() {
                    "--user" if username.is_none() => {
                        username = Some(args.next().context("Missing --user value")?);
                    }
                    "--non-interactive" => non_interactive = true,
                    _ => bail!("Unknown account login option: {flag}"),
                }
            }
            let username = username.context("Use account login --user USER")?;
            let token = request_account_token(username, non_interactive, true).await?;
            verify_ssh_account(&token).await?;
            save_cli_account(&token).await?;
            println!("GeodomDesk CLI signed in.");
        }
        Some("logout") if args.next().is_none() => {
            if let Some((saved, token)) = saved_cli_account()? {
                revoke_account(&token).await?;
                let path = account_path()?;
                let _lock = lock_account(&path)?;
                if read_account(&path)?.as_deref() == Some(&saved) {
                    std::fs::remove_file(path)?;
                }
            }
            println!("GeodomDesk CLI signed out.");
        }
        _ => bail!("Use account login --user USER or account logout"),
    }
    Ok(0)
}

async fn revoke_account(token: &str) -> ResultType<()> {
    let (status, body) = tokio::time::timeout(
        Duration::from_secs(10),
        crate::common::post_terminal_account("logout", "{}".into(), Some(token)),
    )
    .await
    .context("GeodomDesk account sign-out timed out")??;
    if status == 401 || status == 204 {
        return Ok(());
    }
    if status != 200
        || serde_json::from_str::<serde_json::Value>(&body)
            .context("Invalid GeodomDesk sign-out response")?
            .get("error")
            .is_some()
    {
        bail!("GeodomDesk account sign-out failed (HTTP {status})");
    }
    Ok(())
}

const MAX_ACCOUNT_BYTES: usize = 32 * 1024;

fn account_path() -> ResultType<std::path::PathBuf> {
    let path = hbb_common::config::Config::path("cli-account");
    if !path.is_absolute() {
        bail!("No private client configuration directory");
    }
    Ok(path)
}

fn account_authority() -> (String, String) {
    (
        ui_interface::get_api_server(),
        hbb_common::config::Config::get_option("key"),
    )
}

fn read_account(path: &std::path::Path) -> ResultType<Option<Vec<u8>>> {
    use std::io::Read;
    let info = match std::fs::symlink_metadata(path) {
        Ok(info) => info,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !info.is_file() {
        bail!("CLI account cache must be a regular file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if info.permissions().mode() & 0o077 != 0 {
            bail!("CLI account cache permissions must be private");
        }
    }
    let mut content = Vec::new();
    std::fs::File::open(path)?
        .take(MAX_ACCOUNT_BYTES as u64 + 1)
        .read_to_end(&mut content)?;
    if content.len() > MAX_ACCOUNT_BYTES {
        bail!("CLI account cache exceeds its size limit");
    }
    Ok(Some(content))
}

fn saved_cli_account() -> ResultType<Option<(Vec<u8>, String)>> {
    read_cli_account(&account_path()?, &account_authority())
}

fn read_cli_account(
    path: &std::path::Path,
    expected: &(String, String),
) -> ResultType<Option<(Vec<u8>, String)>> {
    let Some(content) = read_account(path)? else {
        return Ok(None);
    };
    let decrypted = hbb_common::password_security::symmetric_crypt(&content, false)
        .map_err(|_| anyhow!("Cannot read CLI account cache. Run account login again"))?;
    let (authority, token): ((String, String), String) = serde_json::from_slice(&decrypted)
        .context("Invalid CLI account cache. Run account login again")?;
    if &authority != expected {
        return Ok(None);
    }
    Ok(Some((content, token)))
}

fn private_account_file() -> std::fs::OpenOptions {
    let mut options = std::fs::OpenOptions::new();
    options.read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

fn lock_account(path: &std::path::Path) -> ResultType<std::fs::File> {
    let lock = private_account_file()
        .create(true)
        .truncate(false)
        .open(path.with_extension("account.lock"))?;
    lock.try_lock().context("CLI account cache is busy")?;
    Ok(lock)
}

async fn save_cli_account(token: &str) -> ResultType<()> {
    let path = account_path()?;
    let authority = account_authority();
    std::fs::create_dir_all(path.parent().context("Invalid CLI account cache path")?)?;
    let _lock = lock_account(&path)?;
    if let Ok(Some((_, previous))) = read_cli_account(&path, &authority) {
        if previous != token {
            revoke_account(&previous).await?;
        }
    }
    write_cli_account(&path, &authority, token)
}

fn write_cli_account(
    path: &std::path::Path,
    authority: &(String, String),
    token: &str,
) -> ResultType<()> {
    use std::io::Write;
    let content = serde_json::to_vec(&(authority, token))?;
    let protected = hbb_common::password_security::symmetric_crypt(&content, true)
        .map_err(|_| anyhow!("Cannot protect the CLI account cache"))?;
    if protected.len() > MAX_ACCOUNT_BYTES {
        bail!("CLI account cache exceeds its size limit");
    }
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> ResultType<()> {
        let mut file = private_account_file().create_new(true).open(&temporary)?;
        file.write_all(&protected)?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}

#[cfg(test)]
mod ssh_account_tests {
    use super::{read_cli_account, verified_profile, verify_ssh_account, write_cli_account};

    #[test]
    fn account_cache_is_protected_replaceable_and_scoped() {
        hbb_common::sodiumoxide::init().unwrap();
        let root =
            std::env::temp_dir().join(format!("geodom-cli-account-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("account");
        let authority = (
            "https://fixture.invalid".to_owned(),
            "fixture-key".to_owned(),
        );
        let other = (authority.0.clone(), "other-key".to_owned());
        let token = "fixture-token";
        write_cli_account(&path, &authority, token).unwrap();
        let (stored, found) = read_cli_account(&path, &authority).unwrap().unwrap();
        assert_eq!(found, token);
        assert!(!stored
            .windows(token.len())
            .any(|bytes| bytes == token.as_bytes()));
        assert!(read_cli_account(&path, &other).unwrap().is_none());
        write_cli_account(&path, &authority, "replacement-token").unwrap();
        assert_eq!(
            read_cli_account(&path, &authority).unwrap().unwrap().1,
            "replacement-token"
        );
        std::fs::write(&path, token).unwrap();
        assert!(read_cli_account(&path, &authority).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

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
