"""Explicit UI opt-in for device data; application settings remain isolated."""

import json


def apply(changes, root):
    change = changes.replace
    change("src/lib.rs", "mod updater;", "mod updater;\nmod geodomdesk_import;")
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
        "Data migration": "数据迁移",
        "Import RustDesk device records": "导入 RustDesk 设备记录",
        "Import RustDesk device records only; existing records and settings are preserved.": "仅导入 RustDesk 设备资料，保留现有记录与设置。",
        "Only recent devices, favorites and aliases will be imported. Server settings, passwords, login tokens and display preferences will not be imported.": "只导入最近连接、收藏与设备备注。不导入服务器设置、密码、登录令牌及显示偏好。",
        "Import complete": "导入完成",
        "Added": "新增",
        "Existing": "已存在",
        "Skipped": "已跳过",
        "Unable to import RustDesk device records. Check that RustDesk user data exists for this user.": "无法导入设备记录，请确认当前用户存在 RustDesk 用户数据。",
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
