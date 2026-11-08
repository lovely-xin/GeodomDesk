"""Compose the terminal command protocol and TCP recovery with the reviewed baseline."""

import subprocess
import tempfile
from pathlib import Path
from .edits import replace_once


SOURCES = (
    "bin/geodomdesk-cli.rs",
    "terminal_exec.rs",
    "terminal_exec/auth.rs",
    "terminal_exec/cli.rs",
    "terminal_exec/exec_bootstrap.ps1",
    "terminal_exec/legacy.rs",
    "terminal_exec/legacy_bootstrap.ps1",
    "terminal_exec/legacy_bootstrap.sh",
    "terminal_exec/windows.rs",
)


def apply(changes, root):
    patch = Path(__file__).with_suffix(".patch")
    paths = [
        line.split(" b/", 1)[1]
        for line in patch.read_text(encoding="utf-8").splitlines()
        if line.startswith("diff --git ")
    ]
    with tempfile.TemporaryDirectory(prefix="geodomdesk-terminal-") as directory:
        staging = Path(directory)
        for name in paths:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(changes[name].encode("utf-8"))
        subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=staging, check=True)
        for name in paths:
            changes[name] = (staging / name).read_text(encoding="utf-8")

    # Run commands in the main executable so service IPC retains its identity check.
    changes["src/core_main.rs"] = replace_once(
        changes["src/core_main.rs"],
        "    crate::load_custom_client();\n",
        "    crate::load_custom_client();\n" + """    #[cfg(any(windows, target_os = "linux"))]
    if std::env::args().nth(1).as_deref() == Some("--terminal-cli") {
        use hbb_common::tokio::runtime::Builder;
        let result = Builder::new_multi_thread()
            .enable_all()
            .build()
            .map_err(hbb_common::anyhow::Error::from)
            .and_then(|runtime| {
                runtime.block_on(crate::terminal_exec::run_cli(std::env::args().skip(2)))
            });
        crate::common::global_clean();
        std::process::exit(match result {
            Ok(code) => code,
            Err(error) => {
                eprintln!("geodomdesk-cli: {error:#}");
                1
            }
        });
    }
""",
    )
    changes["flutter/windows/runner/main.cpp"] = replace_once(
        changes["flutter/windows/runner/main.cpp"],
        "  int args_len = 0;",
        """  if (!command_line_arguments.empty() && command_line_arguments.front() == "--terminal-cli") {
    // No console is expected when an agent supplies redirected standard handles.
    AttachConsole(ATTACH_PARENT_PROCESS);
  }
  int args_len = 0;""",
    )

    client = changes["src/client.rs"]
    for old, new in (
        (
            "    pub is_terminal_admin: bool,\n",
            "    pub is_terminal_admin: bool,\n"
            "    // A command invocation owns its terminal and cannot resume a GUI session.\n"
            "    pub(crate) terminal_one_shot: bool,\n",
        ),
        (
            "            if self.get_toggle_option(keys::OPTION_TERMINAL_PERSISTENT) {",
            "            if !self.terminal_one_shot\n"
            "                && self.get_toggle_option(keys::OPTION_TERMINAL_PERSISTENT)\n"
            "            {",
        ),
        (
            "                terminal.service_id = self.get_option(self.get_key_terminal_service_id());",
            "                if !self.terminal_one_shot {\n"
            "                    terminal.service_id = self.get_option(self.get_key_terminal_service_id());\n"
            "                }",
        ),
        (
            "    pub fn get_key_terminal_service_id(&self) -> &'static str {",
            "    #[cfg(any(windows, target_os = \"linux\"))]\n"
            "    pub(crate) fn has_terminal_device_password(&self) -> bool {\n"
            "        !self.password.is_empty()\n"
            "    }\n\n"
            "    pub fn get_key_terminal_service_id(&self) -> &'static str {",
        ),
    ):
        client = replace_once(client, old, new)
    changes["src/client.rs"] = client

    changes["Cargo.toml"] = replace_once(
        changes["Cargo.toml"],
        "[target.'cfg(any(target_os = \"windows\", target_os = \"linux\"))'.dependencies]\n",
        "[target.'cfg(any(target_os = \"windows\", target_os = \"linux\"))'.dependencies]\n"
        'rpassword = "=7.3.1"\n',
    )
    lock = changes["Cargo.lock"]
    for old, new in (
        (' "rpassword",\n', ' "rpassword 2.1.0",\n'),
        (' "ringbuf",\n "rubato",\n', ' "ringbuf",\n "rpassword 7.3.1",\n "rubato",\n'),
        (
            '[[package]]\nname = "rtcp"',
            '''[[package]]
name = "rpassword"
version = "7.3.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "80472be3c897911d0137b2d2b9055faf6eeac5b14e324073d83bc17b191d7e3f"
dependencies = [
 "libc",
 "rtoolbox",
 "windows-sys 0.48.0",
]

[[package]]
name = "rtcp"''',
        ),
        (
            '[[package]]\nname = "rtp"',
            '''[[package]]
name = "rtoolbox"
version = "0.0.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c247d24e63230cdb56463ae328478bd5eac8b8faa8c69461a77e8e323afac90e"
dependencies = [
 "libc",
 "windows-sys 0.48.0",
]

[[package]]
name = "rtp"''',
        ),
    ):
        lock = replace_once(lock, old, new)
    changes["Cargo.lock"] = lock

    common = changes["src/common.rs"]
    for old, new in (
        (
            "    headers: Vec<HeaderEntry>,\n) -> ResultType<HttpProxyResponse> {",
            "    headers: Vec<HeaderEntry>,\n    require_encryption: bool,\n) -> ResultType<HttpProxyResponse> {",
        ),
        (
            "        secure_tcp_silent(&mut conn, &key).await?;",
            "        if require_encryption {\n"
            "            if !key_exchange(&mut conn, &key, false).await? {\n"
            '                bail!("The sign-in server did not establish encryption");\n'
            "            }\n"
            "        } else {\n"
            "            secure_tcp_silent(&mut conn, &key).await?;\n"
            "        }",
        ),
        (
            "    let resp = tcp_proxy_request(method, url, body_bytes, headers).await?;",
            "    let resp = tcp_proxy_request(method, url, body_bytes, headers, false).await?;",
        ),
        (
            "/// POST request via TCP proxy, preserving the HTTP status code.",
            '''#[cfg(any(windows, target_os = "linux"))]
pub(crate) async fn post_terminal_login(body: String) -> ResultType<(u16, String)> {
    let server = ui_get_api_server();
    let url = url::Url::parse(&format!("{server}/api/login")).context("No account API configured")?;
    if url.scheme() == "https" {
        let mut response = reqwest::Client::builder()
            .use_rustls_tls()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::none())
            .build()?
            .post(url)
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;
        let status = response.status().as_u16();
        let mut data = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if data.len() + chunk.len() > 64 * 1024 {
                bail!("Account sign-in response exceeds 64 KiB");
            }
            data.extend_from_slice(&chunk);
        }
        return Ok((status, String::from_utf8(data)?));
    }
    if url.scheme() != "http" || !should_use_tcp_proxy_for_api_url(url.as_str(), &server) {
        bail!("Account sign-in requires HTTPS or an encrypted rendezvous connection");
    }
    let headers = parse_simple_header("Content-Type: application/json");
    let response = tcp_proxy_request("POST", url.as_str(), body.as_bytes(), headers, true).await?;
    if !response.error.is_empty() {
        bail!("Account sign-in server could not process the request");
    }
    if response.body.len() > 64 * 1024 {
        bail!("Account sign-in response exceeds 64 KiB");
    }
    Ok((response.status as u16, String::from_utf8(response.body.into())?))
}

/// POST request via TCP proxy, preserving the HTTP status code.''',
        ),
    ):
        common = replace_once(common, old, new)
    old = 'tcp_proxy_request("POST", url, body.as_bytes(), headers).await?'
    if common.count(old) != 2:
        raise ValueError("Upstream TCP proxy calls changed")
    changes["src/common.rs"] = common.replace(old, old.replace("headers)", "headers, false)"))

    for name in SOURCES:
        changes["src/" + name] = (root / "client" / name).read_text(encoding="utf-8")
