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
         '''        let request = reqwest::Client::builder()
            .use_rustls_tls()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::none())
            .build()?
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

    edit(files, f"{terminal}/cli.rs", [
        ("""  --transport MODE        auto (default): Bash uses SSH over a managed TCP relay
                          terminal: use GeodomDesk terminal; ssh: require SSH Bash
                          Session without --shell keeps the terminal transport
  --ssh-user USER         Target SSH login (default: authenticated peer username)
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
        ("GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE, GEODOMDESK_SSH_PASSWORD",
         "GEODOMDESK_ADMIN_PASSWORD, GEODOMDESK_DEVICE_CODE, GEODOMDESK_SSH_PASSWORD,\n"
         "             GEODOMDESK_SSH_KEY_PASSPHRASE"),
        ("  --ssh-strict-host-key", "  --ssh-host-fingerprint SHA256:HASH  Verify the destination key against a known fingerprint\n"
         "  --ssh-strict-host-key"),
        ("""        if let Some(ssh)=&self.ssh {
            return tokio::time::timeout(Duration::from_secs(self.timeout.into()),super::ssh::open(self,ssh)).await
                .context("SSH setup timed out before script dispatch").and_then(|v|v)
                .map_err(|error|super::stage::wrap(error,"ssh","not_started"));
        }
        let started = Instant::now();
        let (mut connection, history) =""",
         """        let started = Instant::now();
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
        let (mut connection, history) ="""),
        ("super::stage::connect_with_recovery(Duration::from_secs(self.timeout.into()), self.credential_bound(), || {\n                connect(",
         "super::stage::connect_with_recovery(remaining, self.credential_bound(), || {\n                connect("),
        ("        connection.setup_errors = history;", "        prior_errors.extend(history);\n        connection.setup_errors = prior_errors;"),
        ("async fn connect_for(\n", '''#[derive(Debug)]
pub(super) struct ForwardUnavailable(String);

impl std::fmt::Display for ForwardUnavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ForwardUnavailable {}

fn forward_unavailable(error: &str, host: &str, port: u16) -> bool {
    // Both spellings are used by supported peers for a failed destination TCP connect.
    let address = format!("{host}:{port}");
    error == format!("Failed to access remote {address}, please make sure if it is open")
        || error == format!("Failed to access remote {address}. Please make sure it is reachable/open.")
}

async fn connect_for(
'''),
        ('''                        _ => return Err(super::stage::wrap(anyhow!("{error}"),"authentication","not_started")),
''',
         '''                        _ if forward.is_some_and(|(host, port)| forward_unavailable(&error, host, port)) => {
                            return Err(super::stage::wrap(ForwardUnavailable(error).into(), "forward_target", "not_started"));
                        }
                        _ => return Err(super::stage::wrap(anyhow!("{error}"),"authentication","not_started")),
'''),
        ("    async fn ambiguous_targets_and_payloads_are_rejected_before_authentication() {",
         '''    async fn ambiguous_targets_and_payloads_are_rejected_before_authentication() {
        assert!(forward_unavailable(
            "Failed to access remote 127.0.0.1:22, please make sure if it is open", "127.0.0.1", 22,
        ));
        assert!(forward_unavailable(
            "Failed to access remote 127.0.0.1:22. Please make sure it is reachable/open.", "127.0.0.1", 22,
        ));
        assert!(!forward_unavailable("No permission of IP tunneling", "127.0.0.1", 22));
        assert!(!forward_unavailable(
            "Failed to access remote 127.0.0.1:23, please make sure if it is open", "127.0.0.1", 22,
        ));
'''),
        ("    let mut ssh_user=None;", "    let mut ssh_user=None;\n    let mut ssh_host=None;\n    let mut ssh_fingerprint=None;"),
        ('                    "--ssh-port"=>ssh_port=value.parse().context("Invalid SSH port")?,',
         '''                    "--ssh-host" => {
                        if ssh_host.is_some() { bail!("Specify --ssh-host only once"); }
                        if value.is_empty() || value.len() > 253
                            || value.chars().any(|ch: char| ch.is_control() || ch.is_whitespace())
                        { bail!("Invalid SSH destination host"); }
                        ssh_host = Some(value);
                    },
                    "--ssh-host-fingerprint" => {
                        if ssh_fingerprint.is_some() { bail!("Specify --ssh-host-fingerprint only once"); }
                        let hash = value.strip_prefix("SHA256:").context("Expected an SSH SHA256 fingerprint")?;
                        if hash.len() != 43 || !hash.bytes().all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, b'+' | b'/')) {
                            bail!("Expected an SSH SHA256 fingerprint");
                        }
                        ssh_fingerprint = Some(value);
                    },
                    "--ssh-port"=>ssh_port=value.parse().context("Invalid SSH port")?,'''),
        ('    if probe && shell.is_some() {\n        bail!("probe does not execute a shell");\n    }',
         '''    if probe && shell.is_some() && command_transport != "ssh" {
        bail!("Select --transport ssh to probe a specific SSH shell");
    }'''),
        ('''    let use_ssh=!tunnel && (command_transport=="ssh" || (command_transport=="auto" && shell==Some(ExecShell::BASH)));
    if use_ssh && (ssh_port==0 || admin_user.is_some() || shell.is_some_and(|shell|shell!=ExecShell::BASH)) {bail!("SSH transport requires Bash, a valid SSH port and a Linux login");}
    if !use_ssh && (ssh_user.is_some() || ssh_identity.is_some() || ssh_known_hosts.is_some() || ssh_strict || ask_ssh_password || ssh_port!=22) {bail!("SSH options require --shell bash or --transport ssh");}
    if use_ssh && shell.is_none(){shell=Some(ExecShell::BASH);}
    let ssh=if use_ssh {
        let password=match std::env::var("GEODOMDESK_SSH_PASSWORD") {
            Ok(value)=>Some(value),Err(std::env::VarError::NotPresent) if ask_ssh_password=>{
                if non_interactive {bail!("Set GEODOMDESK_SSH_PASSWORD for non-interactive use");}
                Some(rpassword::prompt_password("Target SSH password: ")?)
            },Err(std::env::VarError::NotPresent)=>None,Err(std::env::VarError::NotUnicode(_))=>bail!("GEODOMDESK_SSH_PASSWORD must be valid Unicode"),
        };
        let mut askpass=std::env::current_exe()?;askpass.set_file_name(if cfg!(windows){"geodomdesk-cli.exe"}else{"geodomdesk-cli"});
        Some(super::ssh::Options{username:ssh_user,port:ssh_port,identity:ssh_identity,
            known_hosts:ssh_known_hosts.unwrap_or_else(||hbb_common::config::Config::path("cli-ssh-known_hosts")),strict_host_key:ssh_strict,password,askpass})
    } else {None};''',
         '''    if ssh_host.is_some() && (shell.is_none() || ssh_user.is_none()) {
        bail!("--ssh-host requires an explicit --ssh-user and --shell");
    }
    let use_ssh = !tunnel && (command_transport == "ssh"
        || (command_transport == "auto" && shell.is_some() && admin_user.is_none()));
    if use_ssh && (ssh_port == 0 || admin_user.is_some()) {
        bail!("SSH requires a valid port and uses --ssh-user for operating system authentication");
    }
    if !use_ssh && (ssh_user.is_some() || ssh_host.is_some() || ssh_identity.is_some()
        || ssh_known_hosts.is_some() || ssh_fingerprint.is_some() || ssh_strict || ask_ssh_password || ssh_port != 22)
    {
        bail!("SSH options require a shell and SSH transport");
    }
    if use_ssh && shell.is_none() {
        shell = Some(ExecShell::BASH);
    }
    let ssh = if use_ssh {
        let allow_fallback = command_transport == "auto" && ssh_host.is_none() && ssh_user.is_none()
            && ssh_port == 22 && ssh_identity.is_none() && ssh_known_hosts.is_none()
            && ssh_fingerprint.is_none() && !ssh_strict && !ask_ssh_password;
        let strict_host_key = ssh_strict || ssh_host.is_some();
        Some(super::ssh::Options {
            username: ssh_user,
            allow_fallback,
            host: ssh_host,
            port: ssh_port,
            identity: ssh_identity,
            known_hosts: ssh_known_hosts
                .unwrap_or_else(|| hbb_common::config::Config::path("cli-ssh-known_hosts")),
            strict_host_key,
            fingerprint: ssh_fingerprint,
            ask_password: ask_ssh_password,
            non_interactive,
            shell: shell.context("Select an SSH shell")?,
        })
    } else {
        None
    };'''),
        ('''            (vec!["exec","--id","1","--transport","ssh","--shell","powershell"],"SSH transport requires Bash"),''',
         '''            (vec!["exec","--id","1","--ssh-host","jump","--shell","powershell"],"--ssh-host requires an explicit --ssh-user and --shell"),'''),
        ('''        info["command_transport"]=serde_json::json!("ssh-relay");''',
         '''        info["command_transport"]=serde_json::json!("ssh-relay");
        info["ssh_destination"]=session.destination();'''),
    ])
    edit(files, f"{terminal}/session.rs", [
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
