"""Build the independent CLI using the same authenticated transport as the desktop."""

from pathlib import Path
import subprocess
import tempfile

from .edits import replace_once


def edit(files, path, substitutions):
    value = files[path].decode()
    for old, new in substitutions:
        value = replace_once(value, old, new)
    files[path] = value.encode()


def apply_lockfile(files, patch):
    with tempfile.TemporaryDirectory(prefix="geodomdesk-cli-lock-") as directory:
        lockfile = Path(directory) / "Cargo.lock"
        lockfile.write_bytes(files["Cargo.lock"])
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=directory, check=False, capture_output=True, text=True,
        )
        if result.returncode:
            raise ValueError(
                "Refresh the standalone CLI dependency lock for the reviewed upstream: "
                + result.stderr.strip()
            )
        files["Cargo.lock"] = lockfile.read_bytes()


def apply(files, root):
    standalone = root / "standalone"
    terminal = "src/terminal_exec"
    files["src/bin/geodomdesk-cli.rs"] = (standalone / "geodomdesk-cli.rs").read_bytes()
    files[f"{terminal}/ssh.rs"] = (standalone / "ssh.rs").read_bytes()
    files.pop(f"{terminal}/ssh_worker.sh")
    files[f"{terminal}/auth.rs"] += b"\n" + (standalone / "auth.rs").read_bytes()
    apply_lockfile(files, standalone / "Cargo.lock.patch")

    edit(files, "Cargo.toml", [
        ('crate-type = ["cdylib", "staticlib", "rlib"]', 'crate-type = ["rlib"]'),
        ('[features]\n', '[features]\nembedded-ssh = []\n'),
        ('rpassword = "=7.3.1"\n', 'rpassword = "=7.3.1"\n'
         'russh = { version = "=0.65.1", default-features = false, features = ["ring", "rsa"] }\n'),
    ])
    edit(files, f"{terminal}/auth.rs", [
        ("fn environment_password(", "pub(super) fn environment_password("),
        ("fn prompt_password(", "pub(super) fn prompt_password("),
        ('''    let Some(username) = username else {
        let token = LocalConfig::get_option("access_token");
        if token.is_empty() {
            bail!("Sign in to GeodomDesk first or supply --account-user");
        }
        return Ok(token);
    };
    let account = credentials(''',
         '''    if let Some(username) = username {
        return request_account_token(username, non_interactive, false).await;
    }
    if let Some((_, token)) = saved_cli_account()? {
        return Ok(token);
    }
    let token = LocalConfig::get_option("access_token");
    if token.is_empty() {
        bail!("Run geodomdesk-cli account login --user USER or supply --account-user");
    }
    Ok(token)
}

async fn request_account_token(
    username: String,
    non_interactive: bool,
    remember: bool,
) -> ResultType<String> {
    let account = credentials('''),
        ('        "autoLogin": false,', '        "autoLogin": remember,'),
    ])
    edit(files, "src/common.rs", [
        ('''pub(crate) async fn post_terminal_login(body: String) -> ResultType<(u16, String)> {
    let server = ui_get_api_server();
    let url = url::Url::parse(&format!("{server}/api/login")).context("No account API configured")?;''',
         '''pub(crate) async fn post_terminal_login(body: String) -> ResultType<(u16, String)> {
    post_terminal_account("login", body, None).await
}

#[cfg(any(windows, target_os = "linux"))]
pub(crate) async fn post_terminal_account(
    action: &str,
    body: String,
    token: Option<&str>,
) -> ResultType<(u16, String)> {
    let server = ui_get_api_server();
    let url = url::Url::parse(&format!("{server}/api/{action}")).context("No account API configured")?;'''),
        ('''        let mut response = reqwest::Client::builder()
            .use_rustls_tls()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::none())
            .build()?
            .post(url)
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;''',
         '''        static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
        let client = if let Some(client) = CLIENT.get() {
            client.clone()
        } else {
            let built = reqwest::Client::builder()
                .use_rustls_tls()
                .https_only(true)
                .redirect(reqwest::redirect::Policy::none())
                .build()?;
            CLIENT.get_or_init(|| built).clone()
        };
        let request = client
            .post(url)
            .header("Content-Type", "application/json")
            .body(body);
        let request = if let Some(token) = token { request.bearer_auth(token) } else { request };
        let mut response = request.send().await?;'''),
        ('''    let headers = parse_simple_header("Content-Type: application/json");
    let response = tcp_proxy_request("POST", url.as_str(), body.as_bytes(), headers, true).await?;''',
         '''    let mut headers = parse_simple_header("Content-Type: application/json");
    if let Some(token) = token {
        headers.push(HeaderEntry {
            name: "Authorization".into(),
            value: format!("Bearer {token}").into(),
            ..Default::default()
        });
    }
    let response = tcp_proxy_request("POST", url.as_str(), body.as_bytes(), headers, true).await?;'''),
    ])
    files[f"{terminal}.rs"] += b"""
/// Independent command entry point. Loading branding selects the same account
/// configuration and tunnel registry as the desktop without launching its UI.
pub fn run_standalone_cli() -> ResultType<i32> {
    crate::load_custom_client();
    hbb_common::sodiumoxide::init()
        .map_err(|_| hbb_common::anyhow::anyhow!("Cannot initialize GeodomDesk cryptography"))?;
    run_cli_with_args(1)
}
"""


    cli_path = f"{terminal}/cli.rs"
    cli_text = files[cli_path].decode()
    # Use the existing transport and account parser around standalone SSH options.
    select = cli_text.index("    let use_ssh=")
    after = cli_text.index("    let is_admin =", select)
    cli_text = cli_text[:select] + '''    if ssh_host_set && (shell.is_none() || ssh_user.is_none()) {
        bail!("--ssh-host requires an explicit --ssh-user and --shell");
    }
    let has_ssh_opts = ssh_user.is_some() || ssh_host_set || ssh_identity.is_some()
        || ssh_known_hosts.is_some() || ssh_fingerprint.is_some() || ssh_strict
        || ask_ssh_password || ssh_port_set;
    let use_ssh = !tunnel && (command_transport == "ssh"
        || (command_transport == "auto"
            && admin_user.is_none()
            && (shell == Some(ExecShell::BASH) || (shell.is_some() && has_ssh_opts))));
    if use_ssh && (ssh_port == 0 || admin_user.is_some()
        || (ssh_host_set && (ssh_host.trim().is_empty() || ssh_host.len() > 253 || ssh_host.chars().any(char::is_control))))
    {
        bail!("SSH requires a valid port and host, and uses --ssh-user for operating system authentication");
    }
    if !use_ssh && has_ssh_opts {
        bail!("SSH options require a shell and SSH transport");
    }
    if use_ssh && shell.is_none() {
        shell = Some(ExecShell::BASH);
    }
''' + cli_text[after:]
    beginning = cli_text.index("        let mut ssh_fallback_errors = Vec::new();", cli_text.index("pub(super) async fn open_with_keep_alive"))
    boundary = cli_text.index("        let token = self.current_token()?;", beginning)
    cli_text = cli_text[:beginning] + '''        let started = Instant::now();
        let budget = Duration::from_secs(self.timeout.into());
        let mut prior_errors = Vec::new();
        if let Some(ssh) = &self.ssh {
            let attempt = tokio::time::timeout(budget, super::ssh::open(self, ssh)).await
                .context("SSH setup timed out before script dispatch")
                .and_then(|result| result);
            match attempt {
                Ok(connection) => return Ok(connection),
                Err(error) if ssh.allow_fallback && error.is::<super::ssh::Unavailable>() => {
                    let error = super::stage::wrap(error, "ssh", "not_started");
                    prior_errors.push(super::stage::SetupError::new(&error, started.elapsed()));
                }
                Err(error) if error.downcast_ref::<super::stage::Stage>().is_some() => return Err(error),
                Err(error) => return Err(super::stage::wrap(error, "ssh", "not_started")),
            }
        }
        let remaining = budget.checked_sub(started.elapsed()).context("Connection budget exhausted")?;
''' + cli_text[boundary:]
    connect = cli_text.index("        let token = self.current_token()?;", beginning)
    body = cli_text[connect:]
    body = body.replace("        ssh_fallback_errors.extend(history);\n        connection.setup_errors = ssh_fallback_errors;",
                        "        prior_errors.extend(history);\n        connection.setup_errors = prior_errors;", 1)
    files[cli_path] = (cli_text[:connect] + body).encode()
    edit(files, f"{terminal}/auth.rs", [("fn saved_cli_account(", "pub(super) fn saved_cli_account(")])
    edit(files, f"{terminal}/input.rs", [
        ('    "--ssh-known-hosts",\n', '    "--ssh-known-hosts",\n    "--ssh-host-fingerprint",\n'),
        ('        assert!(!wants_json(&["exec","--ssh-host","--json"].into_iter().map(str::to_owned).collect::<Vec<_>>()));',
         '        assert!(!wants_json(&["exec","--ssh-host","--json"].into_iter().map(str::to_owned).collect::<Vec<_>>()));\n'
         '        assert!(!wants_json(&["exec","--ssh-host-fingerprint","--json"].into_iter().map(str::to_owned).collect::<Vec<_>>()));'),
    ])
    edit(files, cli_path, [
        ("       geodomdesk-cli probe --id ID [authentication options]\n",
         "       geodomdesk-cli probe --id ID [authentication options]\n"
         "       geodomdesk-cli account login [--user USER] [--non-interactive]\n"
         "       geodomdesk-cli account status [--json] [--pretty]\n"
         "       geodomdesk-cli account logout\n"),
        ("""  --transport MODE        auto (default): Bash uses SSH over a managed TCP relay
                          terminal: use GeodomDesk terminal; ssh: require SSH Bash
                          Session without --shell keeps the terminal transport
  --ssh-user USER         Target SSH login (default: authenticated peer username)
  --ssh-host HOST         Target SSH host inside peer relay (default 127.0.0.1)
  --ssh-port PORT         Target SSH service port (default 22)
  --ssh-identity PATH     Use an SSH private key; otherwise agent/default keys or password""",
         """  --transport MODE        auto (default): embedded SSH over TCP, then GeodomDesk terminal
                          terminal: require GeodomDesk terminal; ssh: require SSH
                          Session without --shell uses the GeodomDesk terminal
  --ssh-user USER         SSH login (default: authenticated peer username)
  --ssh-host HOST         Destination as reached from the device selected by --id
                          Requires --ssh-user, --shell and a trusted destination host key
  --ssh-port PORT         Target SSH service port (default 22)
  --ssh-identity PATH     Use an SSH private key; otherwise default key or password"""),
        ('            (vec!["exec","--id","1","--transport","ssh","--shell","powershell"],"SSH transport requires Bash"),',
         '            (vec!["exec","--id","1","--transport","ssh","--ssh-host","10.0.0.2","--shell","powershell"],"--ssh-host requires an explicit --ssh-user and --shell"),\n'
         '            (vec!["exec","--id","1","--transport","ssh","--ssh-host-fingerprint","bad"],"Expected an SSH SHA256 fingerprint"),'),
        ('    let tunnel=first.as_deref()==Some("tunnel");',
         '    if first.as_deref() == Some("account") { return auth::manage_account(args).await; }\n'
         '    let tunnel=first.as_deref()==Some("tunnel");'),
        ("    fn current_token(&self)", "    pub(super) fn current_token(&self)"),
        ('        let token = hbb_common::config::LocalConfig::get_option_from_file("access_token");\n'
         '        if token.is_empty() {\n'
         '            bail!("Sign in to GeodomDesk first or supply --account-user");\n'
         '        }',
         '        if let Some((_, token)) = auth::saved_cli_account()? { return Ok(token); }\n'
         '        let token = hbb_common::config::LocalConfig::get_option_from_file("access_token");\n'
         '        if token.is_empty() {\n'
         '            bail!("Run geodomdesk-cli account login --user USER or supply --account-user");\n'
         '        }'),
        ('    let mut ssh_host = None;', '    let mut ssh_host = None;\n    let mut ssh_fingerprint = None;'),
        ('                    "--ssh-port" => {',
         '''                    "--ssh-host-fingerprint" => {
                        if ssh_fingerprint.is_some() { bail!("Specify --ssh-host-fingerprint only once"); }
                        let hash = value.strip_prefix("SHA256:").context("Expected an SSH SHA256 fingerprint")?;
                        if hash.len() != 43 || !hash.bytes().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, b'+' | b'/')) {
                            bail!("Expected an SSH SHA256 fingerprint");
                        }
                        ssh_fingerprint = Some(value);
                    }
                    "--ssh-port" => {'''),
        ('    if probe && shell.is_some() {\n        bail!("probe does not execute a shell");\n    }',
         '    if probe && shell.is_some() && command_transport.as_str() != "ssh" {\n'
         '        bail!("Select --transport ssh to probe a specific SSH shell");\n    }'),
        ('    let admin = auth::admin_credentials(admin_user, non_interactive)?;',
         '''    let admin = auth::admin_credentials(admin_user, non_interactive)?;
    let ssh = if use_ssh {
        if !non_interactive && (ask_ssh_password || ssh_identity.is_some()) {
            let token = match &token_override {
                Some(token) => token.clone(),
                None => auth::account_token(None, true).await?,
            };
            auth::verify_ssh_account(&token).await?;
        }
        let allow_fallback = command_transport == "auto" && !ssh_host_set && ssh_user.is_none()
            && ssh_port == 22 && ssh_identity.is_none() && ssh_known_hosts.is_none()
            && ssh_fingerprint.is_none() && !ssh_strict && !ask_ssh_password;
        let authentication = super::ssh::Authentication::prepare(
            ssh_identity.as_deref(), ask_ssh_password, non_interactive,
        ).map_err(|error| super::stage::wrap(error, "ssh", "not_started"))?;
        Some(super::ssh::Options {
            username: ssh_user,
            allow_fallback,
            host: ssh_host_set.then_some(ssh_host),
            port: ssh_port,
            authentication,
            known_hosts: ssh_known_hosts
                .unwrap_or_else(|| hbb_common::config::Config::path("cli-ssh-known_hosts")),
            strict_host_key: ssh_strict || ssh_host_set,
            fingerprint: ssh_fingerprint,
            shell: shell.context("Select an SSH shell")?,
        })
    } else {
        None
    };'''),
        ('    if tunnel {\n        let port = remote_port.context("Missing --remote-port")?;',
         '    if tunnel {\n        auth::verify_ssh_account(&options.current_token()?).await?;\n'
         '        let port = remote_port.context("Missing --remote-port")?;'),
        ('        info["ssh_destination"]=serde_json::json!(session.destination);',
         '        info["ssh_destination"]=session.destination();'),
        ('  --ssh-strict-host-key', '  --ssh-host-fingerprint SHA256:HASH  Verify the destination host key\n'
         '  --ssh-strict-host-key'),
        ('             GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE, GEODOMDESK_SSH_PASSWORD',
         '             GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE, GEODOMDESK_SSH_PASSWORD,\n'
         '             GEODOMDESK_SSH_KEY_PASSPHRASE'),
    ])
    edit(files, f"{terminal}/session.rs", [
        ("    let error = match execute(connection, request.clone(), terminal_id).await {",
         """    if matches!(&connection.backend, cli::Backend::Terminal(_)) {
        super::auth::verify_ssh_account(&options.current_token()?).await?;
    }
    let error = match execute(connection, request.clone(), terminal_id).await {"""),
        ("    execute(connection, request, terminal_id).await\n}",
         """    if matches!(&connection.backend, cli::Backend::Terminal(_)) {
        super::auth::verify_ssh_account(&options.current_token()?).await?;
    }
    execute(connection, request, terminal_id).await
}"""),
        ("""                    if options.ssh.is_some() && request.shell.enum_value()!=Ok(ExecShell::BASH) {
                        had_failure=true;output::write(rejected(id,"invalid_request","An SSH Bash session accepts only shell bash"))?;continue;
                    }
""", """                    if options.ssh.as_ref().is_some_and(|ssh| request.shell.enum_value() != Ok(ssh.shell)) {
                        had_failure = true;
                        output::write(rejected(id, "invalid_request", "Use the shell selected when starting the SSH session"))?;
                        continue;
                    }
"""),
    ])
