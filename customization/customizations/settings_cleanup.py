"""Expose applicable controls and keep user-facing links on owned resources."""
GUIDE = "https://github.com/lovely-xin/GeodomDesk/blob/main/USER_GUIDE.md"
LICENSE = "https://github.com/lovely-xin/GeodomDesk/blob/main/LICENSE"


def apply(changes):
    change = changes.replace
    desktop = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    mobile = "flutter/lib/mobile/pages/settings_page.dart"
    # Connection notes require the upstream management audit API, which is not used here.
    text = changes[desktop]
    start = text.index("    if (!bind.isDisableAccount()) {\n      children.add(_OptionCheckBox(")
    end = text.index("\n    children.add(_OptionCheckBox(", start) + 1
    change(desktop, text[start:end], "")
    text = changes[mobile]
    start = text.index("          if (!bind.isDisableAccount())\n            SettingsTile.switchTile(\n              title: Text(translate('note-at-conn-end-tip')),")
    end = text.index("          if (!incomingOnly)", start)
    change(mobile, text[start:end], "")
    change(mobile, "  var _allowAskForNoteAtEndOfConnection = false;\n", "")
    change(mobile, "    _allowAskForNoteAtEndOfConnection =\n        mainGetLocalBoolOptionSync(kOptionAllowAskForNoteAtEndOfConnection);\n", "")
    common = "flutter/lib/common.dart"
    change(common, "bool mainGetLocalBoolOptionSync(String key) {",
           "bool mainGetLocalBoolOptionSync(String key) {\n  if (key == kOptionAllowAskForNoteAtEndOfConnection) return false;")
    # Existing bot users retain their management switch; new accounts do not see token setup.
    change(desktop, "          tfa,\n          bot,\n          trust,", "          tfa,\n          if (hasBot.value) bot,\n          trust,")
    change(mobile, "      if (enable2fa)\n        SettingsTile.switchTile(\n          title: Text(translate('Telegram bot'))",
           "      if (enable2fa && bind.mainHasValidBotSync())\n        SettingsTile.switchTile(\n          title: Text(translate('Telegram bot'))")
    for name in [desktop, mobile]:
        change(name, "https://rustdesk.com/privacy.html", GUIDE + "#data-and-permissions")
        change(name, "Privacy Statement", "Data and permissions")
    install = "flutter/lib/desktop/pages/install_page.dart"
    changes[install] = changes[install].replace("https://rustdesk.com/privacy.html", LICENSE)
    change(common, "launchUrl(Uri.parse('https://rustdesk.com'));",
           "launchUrl(Uri.parse('https://www.geodom.cn'));")
    connection = "flutter/lib/desktop/pages/connection_page.dart"
    change(connection, "            if (!isIncomingOnly) setupServerWidget(),", "")
    # Remove the unused marketing widget and handler rather than retaining dead controls.
    text = changes[connection]
    start = text.index("  void onUsePublicServerGuide() {")
    end = text.index("  @override", start)
    change(connection, text[start:end], "")
    change(connection, "  final _svcIsUsingPublicServer = true.obs;\n", "")
    change(connection, "    _svcIsUsingPublicServer.value = await bind.mainIsUsingPublicServer();\n", "")
    change(connection, "import 'package:url_launcher/url_launcher_string.dart';\n", "")
    text = changes[connection]
    start = text.index("    setupServerWidget() => Flexible(")
    end = text.index("    basicWidget() => Row(", start)
    change(connection, text[start:end], "")
    home = "flutter/lib/desktop/pages/desktop_home_page.dart"
    for section in ["permissions-issue", "x11-required", "login-screen"]:
        change(home, "https://rustdesk.com/docs/en/client/linux/#" + section, GUIDE + "#ubuntu")
    client = "src/client.rs"
    change(client, "https://rustdesk.com/docs/en/manual/linux/#x11-required", GUIDE + "#ubuntu")
    change(client, "https://rustdesk.com/docs/en/manual/linux/#login-screen", GUIDE + "#ubuntu")
    en = "src/lang/en.rs"
    change(en, "https://rustdesk.com/docs/en/client/linux/#x11-required", GUIDE + "#ubuntu")
    changes.replace("src/lang/cn.rs", "    [\n",
                    '    [\n        ("Data and permissions", "数据与权限"),\n')
