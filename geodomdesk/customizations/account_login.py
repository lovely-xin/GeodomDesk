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
    !crate::geodomdesk_auth::requires_login(text) && msgtype""",
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
            (text == 'Please log in before starting a remote connection' ||
             text == 'Invalid account or password'))) {
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
    resetRestartReconnectState();
    _reconnects = 1;
    _offlineReconnectStartTime = null;
    if (_accountLoginPending) return;
    _accountLoginPending = true;
    dialogManager.dismissAll();
    try {
      final authenticated = await loginDialog();
      if (authenticated == true && parent.target?.closed == false) {
        if (isDesktop) {
          rustDeskWinManager.call(WindowType.Main, kWindowRefreshCurrentUser, '');
        }
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
