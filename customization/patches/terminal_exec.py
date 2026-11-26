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
    "terminal_exec/input.rs",
    "terminal_exec/output.rs",
    "terminal_exec/exec_bootstrap.ps1",
    "terminal_exec/legacy.rs",
    "terminal_exec/session.rs",
    "terminal_exec/stage.rs",
    "terminal_exec/tunnel.rs",
    "terminal_exec/ssh.rs",
    "terminal_exec/ssh_worker.sh",
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
    if std::env::args().nth(1).as_deref() == Some("--terminal-exec-helper") {
        #[cfg(windows)]
        {
            let helper_args: Vec<String> = std::env::args().skip(2).collect();
            if let Err(error) = crate::terminal_exec::run_helper(&helper_args) {
                log::error!("Terminal exec helper failed: {error}");
            }
        }
        crate::common::global_clean();
        return None;
    }
    #[cfg(any(windows, target_os = "linux"))]
    if std::env::args().nth(1).as_deref() == Some("--terminal-cli") {
        let result = crate::terminal_exec::run_cli_process();
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
    conn_rs = changes["src/server/connection.rs"]
    for old, new in (
        (
            '#[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "impl TerminalUserToken {\n"
            "    fn to_terminal_service_token(&self) -> Option<crate::terminal_service::UserToken> {\n"
            "        match self {\n"
            "            TerminalUserToken::SelfUser => None,\n"
            '            #[cfg(target_os = "windows")]\n'
            "            TerminalUserToken::CurrentLogonUser(token) => Some(*token),\n"
            "        }\n"
            "    }\n"
            "}\n",
            '#[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "impl TerminalUserToken {\n"
            "    fn to_terminal_service_token(&self) -> Option<crate::terminal_service::UserToken> {\n"
            "        match self {\n"
            "            TerminalUserToken::SelfUser => None,\n"
            '            #[cfg(target_os = "windows")]\n'
            "            TerminalUserToken::CurrentLogonUser(token) => Some(*token),\n"
            "        }\n"
            "    }\n"
            "}\n"
            '#[cfg(target_os = "windows")]\n'
            "struct OwnedWindowsToken(crate::terminal_service::UserToken);\n"
            '#[cfg(target_os = "windows")]\n'
            "impl Drop for OwnedWindowsToken {\n"
            "    fn drop(&mut self) {\n"
            "        if self.0.as_raw() != 0 {\n"
            "            unsafe {\n"
            "                hbb_common::allow_err!(CloseHandle(HANDLE(self.0.as_raw() as _)));\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n",
        ),
        (
            '    #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "    terminal_user_token: Option<TerminalUserToken>,\n"
            "    terminal_generic_service: Option<Box<GenericService>>,\n",
            '    #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "    terminal_user_token: Option<TerminalUserToken>,\n"
            "    terminal_generic_service: Option<Box<GenericService>>,\n"
            '    #[cfg(any(windows, target_os = "linux"))]\n'
            "    terminal_exec_task: Option<hbb_common::tokio::task::JoinHandle<()>>,\n"
            '    #[cfg(target_os = "windows")]\n'
            "    terminal_exec_token_guard: Option<Arc<OwnedWindowsToken>>,\n",
        ),
        (
            '            #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "            terminal_user_token: None,\n"
            "            terminal_generic_service: None,\n",
            '            #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "            terminal_user_token: None,\n"
            "            terminal_generic_service: None,\n"
            '            #[cfg(any(windows, target_os = "linux"))]\n'
            "            terminal_exec_task: None,\n"
            '            #[cfg(target_os = "windows")]\n'
            "            terminal_exec_token_guard: None,\n",
        ),
        (
            "        if let Some(s) = self.terminal_generic_service.as_ref() {\n"
            "            s.join();\n"
            "        }\n"
            "\n"
            '        #[cfg(target_os = "windows")]\n'
            "        if let Some(TerminalUserToken::CurrentLogonUser(token)) = self.terminal_user_token.take() {\n"
            "            if token.as_raw() != 0 {\n"
            "                unsafe {\n"
            "                    hbb_common::allow_err!(CloseHandle(HANDLE(token.as_raw() as _)));\n"
            "                };\n"
            "            }\n"
            "        }\n",
            "        if let Some(s) = self.terminal_generic_service.as_ref() {\n"
            "            s.join();\n"
            "        }\n"
            '        #[cfg(any(windows, target_os = "linux"))]\n'
            "        if let Some(task) = self.terminal_exec_task.take() {\n"
            "            task.abort();\n"
            "        }\n"
            "\n"
            '        #[cfg(target_os = "windows")]\n'
            "        {\n"
            "            let guarded = self.terminal_exec_token_guard.take();\n"
            "            if let Some(TerminalUserToken::CurrentLogonUser(token)) = self.terminal_user_token.take() {\n"
            "                if guarded.is_none() && token.as_raw() != 0 {\n"
            "                    unsafe {\n"
            "                        hbb_common::allow_err!(CloseHandle(HANDLE(token.as_raw() as _)));\n"
            "                    };\n"
            "                }\n"
            "            }\n"
            "        }\n",
        ),
        (
            "                Some(message::Union::TerminalAction(action)) => {\n"
            '                    #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "                    allow_err!(self.handle_terminal_action(action).await);\n",
            "                Some(message::Union::TerminalAction(action)) => {\n"
            '                    #[cfg(not(any(target_os = "android", target_os = "ios")))]\n'
            "                    if let Err(err) = self.handle_terminal_action(action).await {\n"
            '                        log::error!("Terminal action rejected: {err:#}");\n'
            "                        return false;\n"
            "                    }\n",
        ),
        (
            '        #[cfg(any(windows, target_os = "linux"))]\n'
            "        if action.has_exec() {\n"
            "            if self.authed_conn_type() != Some(AuthConnType::Terminal) || !self.stream.is_secured() {\n"
            '                bail!("Command execution requires an authenticated encrypted terminal");\n'
            "            }\n"
            "            let mut action = action;\n"
            "            return crate::terminal_exec::serve(\n"
            "                &mut self.stream,\n"
            "                action.take_exec(),\n"
            "                &mut self.last_test_delay,\n"
            "                #[cfg(windows)]\n"
            "                user_token.to_terminal_service_token(),\n"
            "            ).await;\n"
            "        }\n",
            '        #[cfg(any(windows, target_os = "linux"))]\n'
            "        if action.has_exec() {\n"
            "            if self.authed_conn_type() != Some(AuthConnType::Terminal) || !self.stream.is_secured() {\n"
            '                bail!("Command execution requires an authenticated encrypted terminal");\n'
            "            }\n"
            "            if self.terminal_exec_task.as_ref().is_some_and(|task| !task.is_finished()) {\n"
            "                let mut response = TerminalResponse::new();\n"
            "                let mut error = TerminalError::new();\n"
            '                error.message = "Another terminal command is already running on this connection".to_owned();\n'
            "                response.set_error(error);\n"
            "                let mut msg_out = Message::new();\n"
            "                msg_out.set_terminal_response(response);\n"
            "                self.send(msg_out).await;\n"
            "                return Ok(());\n"
            "            }\n"
            "            let mut action = action;\n"
            "            let request = action.take_exec();\n"
            "            let mut inner = self.inner.clone();\n"
            "            #[cfg(windows)]\n"
            "            let (token, token_guard): (\n"
            "                Option<crate::terminal_service::UserToken>,\n"
            "                Option<Arc<dyn std::any::Any + Send + Sync>>,\n"
            "            ) = match &user_token {\n"
            "                TerminalUserToken::SelfUser => (None, None),\n"
            "                TerminalUserToken::CurrentLogonUser(raw_token) => {\n"
            "                    let guard = match self.terminal_exec_token_guard.as_ref() {\n"
            "                        Some(existing) if existing.0 == *raw_token => existing.clone(),\n"
            "                        _ => {\n"
            "                            let created = Arc::new(OwnedWindowsToken(*raw_token));\n"
            "                            self.terminal_exec_token_guard = Some(created.clone());\n"
            "                            created\n"
            "                        }\n"
            "                    };\n"
            "                    (\n"
            "                        Some(*raw_token),\n"
            "                        Some(guard as Arc<dyn std::any::Any + Send + Sync>),\n"
            "                    )\n"
            "                }\n"
            "            };\n"
            "            self.terminal_exec_task = Some(hbb_common::tokio::spawn(async move {\n"
            "                let result = crate::terminal_exec::execute(\n"
            "                    request,\n"
            "                    #[cfg(windows)]\n"
            "                    token,\n"
            "                    #[cfg(windows)]\n"
            "                    token_guard,\n"
            "                )\n"
            "                .await;\n"
            "                let mut response = TerminalResponse::new();\n"
            "                response.set_exec_result(result);\n"
            "                let mut msg = Message::new();\n"
            "                msg.set_terminal_response(response);\n"
            "                inner.send(Arc::new(msg));\n"
            "            }));\n"
            "            return Ok(());\n"
            "        }\n",
        ),
    ):
        conn_rs = replace_once(conn_rs, old, new)
    changes["src/server/connection.rs"] = conn_rs
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
        changes["src/" + name] = (root / "client/rust" / name).read_text(encoding="utf-8")

    # CLI mappings remain process-owned: listing them never starts a second GUI listener.
    ffi = changes["src/flutter_ffi.rs"]
    ffi = replace_once(ffi, "pub fn main_get_peer_sync(id: String) -> SyncReturn<String> {", r'''#[cfg(any(windows, target_os = "linux"))]
fn cli_tunnel_runtime() -> hbb_common::anyhow::Result<&'static hbb_common::tokio::runtime::Runtime> {
    static RUNTIME: std::sync::OnceLock<Result<hbb_common::tokio::runtime::Runtime, String>> =
        std::sync::OnceLock::new();
    match RUNTIME.get_or_init(|| {
        hbb_common::tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|error| error.to_string())
    }) {
        Ok(runtime) => Ok(runtime),
        Err(error) => Err(hbb_common::anyhow::anyhow!("{error}")),
    }
}

pub fn main_list_cli_tunnels(id: String) -> String {
    #[cfg(any(windows, target_os = "linux"))]
    {
        let dir = hbb_common::config::Config::path("cli-tunnels");
        let has_records = std::fs::read_dir(&dir).is_ok_and(|entries| {
            entries.flatten().any(|entry| {
                entry.path().extension().and_then(|ext| ext.to_str()) == Some("json")
            })
        });
        if !has_records {
            return "[]".to_owned();
        }
        // FRB 1.x runs ordinary functions on its worker pool and returns a Dart Future.
        return match cli_tunnel_runtime()
            .and_then(|runtime| runtime.block_on(crate::terminal_exec::list_tunnels(&id))) {
            Ok(value) => value.to_string(),
            Err(error) => serde_json::json!({"error":format!("{error:#}")}).to_string(),
        };
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    "[]".to_owned()
}

pub fn main_close_cli_tunnel(id: String, tunnel_id: String) -> String {
    #[cfg(any(windows, target_os = "linux"))]
    // FRB 1.x runs ordinary functions on its worker pool and returns a Dart Future.
    return match cli_tunnel_runtime()
        .and_then(|runtime| runtime.block_on(crate::terminal_exec::close_tunnel(&id, &tunnel_id))) {
        Ok(value) => value.to_string(),
        Err(error) => serde_json::json!({"error":format!("{error:#}")}).to_string(),
    };
    #[cfg(not(any(windows, target_os = "linux")))]
    "{\"error\":\"CLI tunnels require Windows or Linux\"}".to_owned()
}

pub fn main_get_peer_sync(id: String) -> SyncReturn<String> {''')
    changes["src/flutter_ffi.rs"] = ffi
    changes["flutter/lib/web/bridge.dart"] = replace_once(
        changes["flutter/lib/web/bridge.dart"],
        "  String mainGetPeerSync({required String id, dynamic hint}) {",
        """  Future<String> mainListCliTunnels({required String id, dynamic hint}) async => '[]';
  Future<String> mainCloseCliTunnel({required String id, required String tunnelId, dynamic hint}) async =>
      '{"error":"CLI tunnels require Windows or Linux"}';

  String mainGetPeerSync({required String id, dynamic hint}) {""")
    page = changes["flutter/lib/desktop/pages/port_forward_page.dart"]
    for old, new in (
        ("import 'dart:convert';", "import 'dart:convert';\nimport 'dart:async';"),
        ("  late FFI _ffi;", """  late FFI _ffi;
  final ScrollController _scrollController = ScrollController();
  final RxList<Map<String, dynamic>> cliTunnels = <Map<String, dynamic>>[].obs;
  Timer? _cliTimer;
  bool _refreshingCli = false;
  bool _refreshPending = false;
  String _lastCliJson = '[]';
  final RxnString _cliError = RxnString();
  final RxSet<String> _closingIds = <String>{}.obs;

  bool get _hasLiveCliTunnels =>
      cliTunnels.any((t) => t['state'] == 'listening' || t['state'] == 'active');

  void _scheduleCliRefresh() {
    _cliTimer?.cancel();
    if (!mounted || widget.isRDP) return;
    _cliTimer = Timer(
      Duration(seconds: _hasLiveCliTunnels ? 2 : 5),
      refreshCliTunnels,
    );
  }
"""),
        ('    Get.put<FFI>(_ffi, tag:', """    if (!widget.isRDP) {
      refreshCliTunnels();
    }
    Get.put<FFI>(_ffi, tag:"""),
        ("    _ffi.close();", "    _cliTimer?.cancel();\n    _scrollController.dispose();\n    _ffi.close();"),
        ("              height: 45,", "              height: cliTunnels.isEmpty && _cliError.value == null ? 45 : null,\n              constraints: const BoxConstraints(minHeight: 45),"),
        ("                  mainAxisAlignment: MainAxisAlignment.center,", "                  mainAxisSize: MainAxisSize.min,\n                  mainAxisAlignment: MainAxisAlignment.center,"),
        ("offstage: pfs.isEmpty && !widget.isRDP,", "offstage: pfs.isEmpty && !_hasLiveCliTunnels && !widget.isRDP,"),
        ("translate('not_close_tcp_tip')", "translate(pfs.isEmpty && _hasLiveCliTunnels ? 'CLI tunnel lifetime' : 'not_close_tcp_tip')"),
        ("      child: Obx(() => ListView.builder(\n          controller: ScrollController(),", "      child: Obx(() => ListView.builder(\n          controller: _scrollController,"),
        ("itemCount: pfs.length + 2,", "itemCount: pfs.length + cliTunnels.length + 2 + (_cliError.value == null ? 0 : 1),"),
        ("          itemCount: pfs.length + cliTunnels.length + 2 + (_cliError.value == null ? 0 : 1),\n          itemBuilder: ((context, index) {\n            if (index == 0) {", """          itemCount: pfs.length + cliTunnels.length + 2 + (_cliError.value == null ? 0 : 1),
          itemBuilder: ((context, index) {
            if (_cliError.value != null && index == pfs.length + cliTunnels.length + 2) {
              return ListTile(title: Text(translate('CLI tunnel status unavailable')),
                subtitle: Text(_cliError.value!, maxLines: 2, overflow: TextOverflow.ellipsis),
                trailing: IconButton(tooltip: translate('Refresh tunnel status'), icon: const Icon(Icons.refresh), onPressed: () => refreshCliTunnels(force: true)));
            }
            if (index == 0) {"""),
        ("              return buildTunnelDataRow(context, pfs[index - 2], index - 2);", """              if (index - 2 < pfs.length) {
                return buildTunnelDataRow(context, pfs[index - 2], index - 2);
              }
              return buildCliTunnelRow(context, cliTunnels[index - 2 - pfs.length]);"""),
        ("  void refreshTunnelConfig() async {", """  Future<void> refreshCliTunnels({bool force = false}) async {
    if (!mounted) return;
    if (_refreshingCli) {
      _refreshPending = true;
      return;
    }
    _refreshingCli = true;
    try {
      final raw = await bind.mainListCliTunnels(id: widget.id);
      if (!mounted) return;
      if (!force && raw == _lastCliJson && _cliError.value == null && raw.startsWith('[')) {
        return;
      }
      final value = jsonDecode(raw);
      if (value is! List) throw Exception(value['error'] ?? 'Cannot list CLI tunnels');
      _lastCliJson = raw;
      cliTunnels.assignAll(value.cast<Map<String, dynamic>>());
      _cliError.value = null;
    } catch (error) {
      // Keep the last observed rows; a failed refresh does not prove that a mapping closed.
      if (mounted && _cliError.value != error.toString()) {
        _cliError.value = error.toString();
        debugPrint('CLI tunnel refresh: $error');
      }
    } finally {
      _refreshingCli = false;
      if (_refreshPending && mounted) {
        _refreshPending = false;
        _cliTimer?.cancel();
        _cliTimer = Timer(Duration.zero, refreshCliTunnels);
      } else {
        _scheduleCliRefresh();
      }
    }
  }

  Widget buildCliTunnelRow(BuildContext context, Map<String, dynamic> tunnel) {
    Widget cell(String value) => Expanded(
        child: Tooltip(message: value, child: Text(value, style: const TextStyle(fontSize: 20), maxLines: 1, overflow: TextOverflow.ellipsis)).marginOnly(left: _kTextLeftMargin));
    final id = tunnel['tunnel_id'] as String;
    final localPort = tunnel['local_port'] is int
        ? tunnel['local_port'] as int
        : int.tryParse('${tunnel['local_port']}') ?? 0;
    final endpointLabel = localPort > 0 ? '$localPort' : 'SSH';
    final state = tunnel['state'] == 'active' ? translate('Active') : tunnel['state'] == 'listening' ? translate('Listening ...') : translate('Status unavailable');
    final transportRaw = (tunnel['transport'] ?? '').toString();
    final transport = transportRaw.isNotEmpty
        ? transportRaw
        : translate(tunnel['direct'] == true ? 'Direct' : 'Relay');
    final routeTag = tunnel['multiplexed'] == true ? '$transport · mux' : transport;
    final activeCount = tunnel['active_connections'];
    final connSuffix = activeCount is int && activeCount > 0 ? ' · $activeCount' : '';
    final purpose = (tunnel['purpose'] ?? '').toString().trim();
    final lastError = (tunnel['last_error'] ?? '').toString().trim();
    final tooltipParts = <String>[
      '${translate('CLI tunnel')} · $id · PID ${tunnel['owner_pid']}',
      routeTag,
      if (purpose.isNotEmpty) purpose,
      if (lastError.isNotEmpty) lastError,
    ];
    final subtitle = lastError.isNotEmpty
        ? '${translate('CLI tunnel')} · $state · $routeTag$connSuffix · $lastError'
        : purpose.isNotEmpty
            ? '${translate('CLI tunnel')} · $state · $routeTag$connSuffix · $purpose'
            : '${translate('CLI tunnel')} · $state · $routeTag$connSuffix';
    return Container(
      constraints: const BoxConstraints(minHeight: _kRowHeight),
      child: Row(children: [
        Expanded(child: Tooltip(
          message: tooltipParts.join(' · '),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(mainAxisSize: MainAxisSize.min, children: [
              Flexible(child: Text(endpointLabel, style: const TextStyle(fontSize: 20), maxLines: 1, overflow: TextOverflow.ellipsis)),
              if (localPort > 0)
                IconButton(
                  tooltip: translate('Copy local address'),
                  icon: const Icon(Icons.content_copy, size: 16),
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                  visualDensity: VisualDensity.compact,
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: '127.0.0.1:$localPort'));
                    showToast(translate('Copied'));
                  },
                ),
            ]),
            Text(subtitle, style: Theme.of(context).textTheme.bodySmall, maxLines: 1, overflow: TextOverflow.ellipsis),
          ]).marginOnly(left: _kTextLeftMargin),
        )),
        const SizedBox(width: _kColumn1Width, child: Icon(Icons.arrow_forward_sharp)),
        cell('${tunnel['remote_host']}'),
        cell('${tunnel['remote_port']}'),
        SizedBox(width: _kColumn4Width, child: Obx(() {
          final closing = _closingIds.contains(id);
          return IconButton(
            tooltip: translate('Close CLI tunnel'),
            icon: const Icon(Icons.close),
            onPressed: closing ? null : () async {
              _closingIds.add(id);
              try {
                final value = jsonDecode(await bind.mainCloseCliTunnel(id: widget.id, tunnelId: id));
                if (value['error'] != null) throw Exception(value['error']);
                cliTunnels.removeWhere((item) => item['tunnel_id'] == id);
                _lastCliJson = '';
                await refreshCliTunnels(force: true);
              } catch (error) {
                if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.toString())));
              } finally {
                if (mounted) _closingIds.remove(id);
              }
            },
          );
        })),
      ]),
    );
  }

  void refreshTunnelConfig() async {"""),
    ):
        page = replace_once(page, old, new)
    page = replace_once(page,
        "                height: 25,\n                color: Theme.of(context).scaffoldBackgroundColor,\n                child: Row(children: [\n                  text('Local Port'),",
        "                constraints: const BoxConstraints(minHeight: 25),\n                color: Theme.of(context).scaffoldBackgroundColor,\n                child: Row(children: [\n                  text('Local Port'),")
    changes["flutter/lib/desktop/pages/port_forward_page.dart"] = page

    entries = {'CLI tunnel': 'CLI 隧道', 'Close CLI tunnel': '关闭 CLI 隧道', 'CLI tunnel lifetime': '由 CLI 管理，关闭此窗口不会停止该隧道', 'Active': '使用中', 'CLI tunnel status unavailable': 'CLI 隧道状态暂不可用', 'Status unavailable': '状态暂不可用', 'Refresh tunnel status': '刷新隧道状态', 'Copy local address': '复制本地地址', 'Direct': '直连', 'Relay': '中继'}
    import json
    cn = changes["src/lang/cn.rs"]
    text = "".join("        (" + json.dumps(k, ensure_ascii=False) + ", " + json.dumps(v, ensure_ascii=False) + "),\n" for k,v in entries.items() if "(" + json.dumps(k) + "," not in cn)
    changes["src/lang/cn.rs"] = replace_once(cn, "    [\n", "    [\n" + text)
