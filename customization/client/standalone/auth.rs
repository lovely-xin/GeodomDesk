static SSH_ACCOUNT_CACHE: std::sync::OnceLock<
    std::sync::Mutex<Option<([u8; 32], std::time::Instant)>>,
> = std::sync::OnceLock::new();

pub(super) async fn verify_ssh_account(token: &str) -> ResultType<()> {
    if token.is_empty() || token.bytes().any(|byte| !byte.is_ascii_graphic()) {
        return Err(super::stage::wrap(
            anyhow!("GeodomDesk session is missing or invalid. Run account login"),
            "account_auth",
            "not_started",
        ));
    }
    let digest = hbb_common::sodiumoxide::crypto::hash::sha256::hash(token.as_bytes()).0;
    let cache = SSH_ACCOUNT_CACHE.get_or_init(|| std::sync::Mutex::new(None));
    {
        let guard = cache.lock().unwrap_or_else(|poison| poison.into_inner());
        if let Some((cached_digest, verified_at)) = *guard {
            if cached_digest == digest && verified_at.elapsed() < Duration::from_secs(60) {
                return Ok(());
            }
        }
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
    if checked.is_ok() {
        let mut guard = cache.lock().unwrap_or_else(|poison| poison.into_inner());
        *guard = Some((digest, std::time::Instant::now()));
    } else {
        let mut guard = cache.lock().unwrap_or_else(|poison| poison.into_inner());
        if guard
            .as_ref()
            .is_some_and(|(cached_digest, _)| *cached_digest == digest)
        {
            *guard = None;
        }
    }
    checked.map_err(|error| super::stage::wrap(error, "account_auth", "not_started"))
}

fn verified_profile(status: u16, body: &str) -> ResultType<()> {
    fetch_profile(status, body).map(|_| ())
}

fn fetch_profile(status: u16, body: &str) -> ResultType<serde_json::Value> {
    if status == 401 || status == 403 {
        bail!("GeodomDesk session expired or rejected (HTTP {status}). Run account login again");
    }
    if status != 200 {
        bail!("GeodomDesk account service returned HTTP {status}; retry later or check server connectivity");
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
    Ok(value)
}

const ACCOUNT_USAGE: &str = "\
Usage: geodomdesk-cli account login [--user USER] [--non-interactive]
       geodomdesk-cli account status [--json] [--pretty]
       geodomdesk-cli account whoami [--json] [--pretty]
       geodomdesk-cli account logout

Subcommands:
  login    Sign in and save an encrypted CLI token for this account server
  status   Verify the active saved CLI or desktop token and print account details
  logout   Revoke and remove the saved standalone CLI token";

const ACCOUNT_LOGIN_USAGE: &str = "\
Usage: geodomdesk-cli account login [--user USER] [--non-interactive]

Options:
  --user USER         GeodomDesk account username (prompted interactively if omitted)
  --non-interactive   Read password from GEODOMDESK_ACCOUNT_PASSWORD; require --user
  -h, --help          Show this help message";

pub(super) async fn manage_account(mut args: impl Iterator<Item = String>) -> ResultType<i32> {
    match args.next().as_deref() {
        None | Some("--help" | "-h" | "help") => {
            println!("{ACCOUNT_USAGE}");
        }
        Some("login") => {
            let mut username = None;
            let mut non_interactive = false;
            while let Some(arg) = args.next() {
                let (flag, inline) = match arg.split_once('=') {
                    Some((name, val)) => (name, Some(val.to_owned())),
                    None => (arg.as_str(), None),
                };
                match flag {
                    "--help" | "-h" => {
                        if inline.is_some() {
                            bail!("Option {flag} does not take a value");
                        }
                        println!("{ACCOUNT_LOGIN_USAGE}");
                        return Ok(0);
                    }
                    "--user" if username.is_none() => {
                        let val = match inline {
                            Some(v) => v,
                            None => args.next().context("Missing --user value")?,
                        };
                        if val.trim().is_empty() {
                            bail!("--user must not be empty");
                        }
                        username = Some(val);
                    }
                    "--user" => bail!("Specify --user only once"),
                    "--non-interactive" => {
                        if inline.is_some() {
                            bail!("Option {flag} does not take a value");
                        }
                        non_interactive = true;
                    }
                    _ => bail!("Unknown account login option: {flag}"),
                }
            }
            let username = match username {
                Some(name) => name,
                None => {
                    use std::io::{IsTerminal, Write};
                    if non_interactive || !std::io::stdin().is_terminal() {
                        bail!("Use account login --user USER");
                    }
                    eprint!("GeodomDesk account username: ");
                    std::io::stderr().flush()?;
                    let mut input = String::new();
                    std::io::stdin().read_line(&mut input)?;
                    let trimmed = input.trim().to_owned();
                    if trimmed.is_empty() {
                        bail!("Use account login --user USER");
                    }
                    trimmed
                }
            };
            let token = request_account_token(username, non_interactive, true).await?;
            verify_ssh_account(&token).await?;
            save_cli_account(&token).await?;
            println!("GeodomDesk CLI signed in.");
        }
        Some("status" | "whoami") => {
            let mut json = false;
            let mut pretty = false;
            while let Some(flag) = args.next() {
                match flag.as_str() {
                    "--help" | "-h" => {
                        println!("{ACCOUNT_USAGE}");
                        return Ok(0);
                    }
                    "--json" => json = true,
                    "--pretty" => {
                        json = true;
                        pretty = true;
                    }
                    _ => bail!("Unknown account status option: {flag}"),
                }
            }
            let (source, token) = if let Some((_, token)) = saved_cli_account()? {
                ("cli", token)
            } else {
                let desktop = LocalConfig::get_option("access_token");
                if desktop.is_empty() {
                    bail!("Not signed in. Run geodomdesk-cli account login --user USER");
                }
                ("desktop", desktop)
            };
            let (status, body) = tokio::time::timeout(
                Duration::from_secs(10),
                crate::common::post_terminal_account("currentUser", "{}".into(), Some(&token)),
            )
            .await
            .context("GeodomDesk account verification timed out")??;
            let profile = fetch_profile(status, &body)?;
            let name = profile["name"].as_str().unwrap_or_default().trim();
            let display_name = profile["display_name"].as_str().unwrap_or_default().trim();
            if json {
                let payload = serde_json::json!({
                    "ok": true,
                    "source": source,
                    "name": name,
                    "display_name": display_name,
                });
                if pretty {
                    super::output::write_pretty(payload)?;
                } else {
                    super::output::write(payload)?;
                }
            } else if !display_name.is_empty() && display_name != name {
                println!("Signed in as {name} ({display_name}) via {source} session.");
            } else {
                println!("Signed in as {name} via {source} session.");
            }
        }
        Some("logout") => {
            while let Some(flag) = args.next() {
                match flag.as_str() {
                    "--help" | "-h" => {
                        println!("{ACCOUNT_USAGE}");
                        return Ok(0);
                    }
                    _ => bail!("Unknown account logout option: {flag}"),
                }
            }
            if let Some((saved, token)) = saved_cli_account()? {
                {
                    let path = account_path()?;
                    let _lock = lock_account(&path)?;
                    if read_account(&path)?.as_deref() == Some(&saved) {
                        std::fs::remove_file(path)?;
                    }
                }
                revoke_account(&token).await.context(
                    "CLI account cache cleared. The server did not confirm token revocation",
                )?;
            }
            println!("GeodomDesk CLI signed out.");
        }
        _ => bail!("Use account login, account status, or account logout"),
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
    let previous = {
        let _lock = lock_account(&path)?;
        let previous = read_cli_account(&path, &authority)
            .ok()
            .flatten()
            .map(|(_, prev)| prev)
            .filter(|prev| prev != token);
        write_cli_account(&path, &authority, token)?;
        previous
    };
    if let Some(previous) = previous {
        let _ = revoke_account(&previous).await;
    }
    Ok(())
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
        for attempt in 0..3 {
            match std::fs::rename(&temporary, path) {
                Ok(()) => return Ok(()),
                Err(error) => {
                    if !cfg!(windows) || attempt == 2 {
                        return Err(error.into());
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
            }
        }
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
