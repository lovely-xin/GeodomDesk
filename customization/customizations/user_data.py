"""First-run and explicit same-server import; application settings remain isolated."""

import json


def apply(changes, root):
    change = changes.replace
    change("src/lib.rs", "mod updater;", "mod updater;\nmod geodomdesk_import;")
    change(
        "src/core_main.rs",
        '    if args.is_empty() || crate::common::is_empty_uni_link(&args[0]) {',
        '    if args.is_empty() || crate::common::is_empty_uni_link(&args[0]) {\n'
        '        #[cfg(any(target_os = "windows", target_os = "linux"))]\n'
        "        crate::geodomdesk_import::import_on_first_run();",
    )
    change(
        "flutter/lib/web/bridge.dart",
        "  Future<void> mainChangeId({required String newId, dynamic hint}) {",
        """  Future<String> mainImportOfficialUserData({dynamic hint}) {
    throw UnsupportedError('Device import requires native user data');
  }

  Future<void> mainChangeId({required String newId, dynamic hint}) {""",
    )
    change(
        "src/flutter_ffi.rs",
        "pub fn main_get_fav() -> Vec<String> {",
        """pub fn main_import_official_user_data() -> hbb_common::anyhow::Result<String> {
    Ok(serde_json::to_string(&crate::geodomdesk_import::import()?)?)
}

pub fn main_get_fav() -> Vec<String> {""",
    )
    name = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    change(
        name,
        "  final GlobalKey _minToolbarOptionKey = GlobalKey();",
        """  final GlobalKey _minToolbarOptionKey = GlobalKey();
  bool _importingUserData = false;
  String _importUserDataStatus = '';""",
    )
    change(
        name,
        "        _Card(title: 'Language', children: [language()]),",
        "        _Card(title: 'Language', children: [language()]),\n        if (!isWeb) importOfficialUserData(),",
    )
    change(
        name,
        "  Widget keyboardShortcuts() {",
        (root / "client/import_user_data.dart.in").read_text(encoding="utf-8")
        + "  Widget keyboardShortcuts() {",
    )
    translations = {
        "Import": "导入",
        "Data migration": "数据迁移",
        "Import %upstream%": "导入 %upstream%",
        "Import matching-server devices and saved connection passwords from %upstream%.": "从 %upstream% 导入同公钥设备的记录、收藏、备注和已保存的连接密码。",
        "Import matching-server devices and saved connection passwords. Existing passwords, host credentials and account sign-ins are preserved.": "导入同公钥设备及已保存的连接密码，已有记录只补齐空密码。本机访问密码、账号登录状态和其他设置保留。",
        "Saved passwords": "连接密码",
        "Passwords not imported": "密码未迁移",
        "Import complete": "导入完成",
        "Added": "新增",
        "Existing": "已存在",
        "Skipped": "已跳过",
        "Unable to import %upstream% device records. Check that %upstream% user data exists for this user.": "无法导入设备记录，请确认当前用户存在 %upstream% 用户数据。",
    }
    entries = "".join(
        "        ("
        + json.dumps(k, ensure_ascii=False)
        + ", "
        + json.dumps(v, ensure_ascii=False)
        + "),\n"
        for k, v in translations.items()
        if "(" + json.dumps(k, ensure_ascii=False) + "," not in changes["src/lang/cn.rs"]
    )
    change("src/lang/cn.rs", "    [\n", "    [\n" + entries)
