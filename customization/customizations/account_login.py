"""Account-login errors stop connection retries and open the existing login dialog."""


def apply(changes):
    change = changes.replace
    change("src/lib.rs", "mod updater;", "mod updater;\nmod geodomdesk_auth;")
    change(
        "src/client.rs",
        """    fn on_establish_connection_error(&self, err: String) {
        let title""",
        """    fn on_establish_connection_error(&self, err: String) {
        if crate::geodomdesk_auth::requires_login(&err) {
            self.msgbox("account-login-required", "Connection Error", &err, "");
            return;
        }
        let title""",
    )
    change(
        "src/client.rs",
        """pub fn check_if_retry(msgtype: &str, title: &str, text: &str, retry_for_relay: bool) -> bool {
    msgtype""",
        """pub fn check_if_retry(msgtype: &str, title: &str, text: &str, retry_for_relay: bool) -> bool {
    !crate::geodomdesk_auth::requires_login(text)
        && msgtype""",
    )
    change(
        "flutter/lib/models/model.dart",
        "import '../common/widgets/dialog.dart';",
        "import '../common/widgets/dialog.dart';\nimport '../common/widgets/login.dart';",
    )
    change(
        "flutter/lib/models/model.dart",
        "  var _reconnects = 1;",
        "  var _reconnects = 1;\n  bool _accountLoginPending = false;",
    )
    change(
        "flutter/lib/models/model.dart",
        """    if (type == 're-input-password') {""",
        """    if (type == 'account-login-required' ||
        (title == 'Connection Error' &&
            const {
              'Please log in before starting a remote connection',
              'Invalid account or password',
            }.contains(text is String ? text.trim() : text))) {
      _requestAccountLogin(sessionId, dialogManager);
      return;
    }

    if (type == 're-input-password') {""",
    )
    change(
        "flutter/lib/models/model.dart",
        "  void resetRestartReconnectState() {",
        """  Future<void> _requestAccountLogin(
      SessionID sessionId, OverlayDialogManager dialogManager) async {
    _timer?.cancel();
    _timer = null;
    waitForImageTimer?.cancel();
    waitForImageTimer = null;
    resetRestartReconnectState();
    _reconnects = 1;
    _offlineReconnectStartTime = null;
    if (_accountLoginPending) return;
    _accountLoginPending = true;
    dialogManager.dismissAll();
    try {
      final authenticated = await loginDialog().catchError((_) => false);
      if (authenticated == true && parent.target?.closed == false) {
        if (isDesktop) {
          rustDeskWinManager.call(WindowType.Main, kWindowRefreshCurrentUser, '');
        }
        // Tunnel listeners read the current token for each new local connection.
        if (parent.target?.connType == ConnType.portForward) return;
        // A new connection reads the newly stored account token. Do not use a retry timer.
        reconnect(dialogManager, sessionId, false);
      } else if (parent.target?.closed == false) {
        showMsgBox(sessionId, 'error', 'Connection Error',
            'Please log in before starting a remote connection', '', false, dialogManager);
      }
    } finally {
      _accountLoginPending = false;
    }
  }

  void resetRestartReconnectState() {""",
    )

    change(
        "flutter/lib/common/widgets/login.dart",
        "  if (res != null) {\n    await UserModel.updateOtherModels();\n  }",
        """  if (res == true) {
    try {
      await UserModel.updateOtherModels();
    } catch (_) {
      // Account-data refresh must not discard a successful authentication.
      debugPrint('Failed to refresh account data after login');
    }
  }""",
    )

    name = "src/port_forward.rs"
    change(
        name,
        "    key: &str,\n    token: &str,\n    lc: Arc<RwLock<LoginConfigHandler>>,",
        "    key: &str,\n    lc: Arc<RwLock<LoginConfigHandler>>,",
    )
    change(
        name,
        '                log::info!("new connection from {:?}", addr);',
        '                log::info!("new connection from {:?}", addr);\n'
        '                let token = hbb_common::config::LocalConfig::get_option("access_token");',
    )
    change(
        name,
        "forward, addr, key, token, is_rdp, &remote_host, remote_port).await",
        "forward, addr, key, &token, is_rdp, &remote_host, remote_port).await",
    )
    change(
        name,
        "&mut forward, key, token, is_rdp, &mut close_port_forward, &remote_host, remote_port).await",
        "&mut forward, key, &token, is_rdp, &mut close_port_forward, &remote_host, remote_port).await",
    )
    name = "src/ui_session_interface.rs"
    change(
        name,
        'start_one_port_forward(handler, 0, "".to_owned(), port, receiver, &key, &token).await;',
        'start_one_port_forward(handler, 0, "".to_owned(), port, receiver, &key).await;',
    )
    change(name, "                        let token = token.clone();\n", "")
    change(name, "                                &key,\n                                &token,\n", "                                &key,\n")
    change(name, "                &key,\n                &token,\n", "                &key,\n")
    change(
        name,
        "    key: &str,\n    token: &str,\n) {\n    if let Err(err) = crate::port_forward::listen(",
        "    key: &str,\n) {\n    if let Err(err) = crate::port_forward::listen(",
    )
    change(name, "        key,\n        token,\n        handler.lc.clone(),", "        key,\n        handler.lc.clone(),")
