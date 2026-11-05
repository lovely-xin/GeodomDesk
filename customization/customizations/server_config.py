"""Compiled, fixed connection settings; no external preset files or credentials."""

import json


def apply(changes, config):
    server = config["server"]
    settings = {
        "custom-rendezvous-server": server["host"],
        "key": server["key"],
        "websocket-port": server["websocket_port"],
        "api-server": server.get("api", ""),
        "relay-server": server.get("relay", ""),
    }
    entries = ",\n".join(
        "        (" + json.dumps(key) + ".to_owned(), " + json.dumps(value) + ".to_owned())"
        for key, value in settings.items()
    )
    changes.replace(
        "libs/hbb_common/src/config.rs",
        "pub static ref OVERWRITE_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
        "pub static ref OVERWRITE_SETTINGS: RwLock<HashMap<String, String>> = RwLock::new(HashMap::from([\n"
        + entries
        + "\n    ]));",
    )
    changes.replace(
        "libs/hbb_common/src/config.rs",
        "pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
        """pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = RwLock::new(HashMap::from([
        #[cfg(target_os = "windows")]
        ("allow-auto-update".to_owned(), "Y".to_owned())
    ]));""",
    )
    changes.replace(
        "libs/hbb_common/src/config.rs",
        "pub static ref BUILTIN_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
        """pub static ref BUILTIN_SETTINGS: RwLock<HashMap<String, String>> = RwLock::new(HashMap::from([
        ("hide-server-settings".to_owned(), "Y".to_owned()),
        ("hide-powered-by-me".to_owned(), "Y".to_owned()),
        ("allow-deep-link-server-settings".to_owned(), "N".to_owned())
    ]));""",
    )
    changes.replace(
        "flutter/lib/mobile/widgets/dialog.dart",
        "import '../../common.dart';",
        "import '../../common.dart';\nimport '../../consts.dart';",
    )
    changes.replace(
        "flutter/lib/mobile/widgets/dialog.dart",
        """    void Function(VoidCallback)? upSetState) async {
  const labelWidth = 120.0;""",
        """    void Function(VoidCallback)? upSetState) async {
  if (bind.mainGetBuildinOption(key: kOptionHideServerSetting) == 'Y') return;
  const labelWidth = 120.0;""",
    )
    name = "src/common.rs"
    for start, end, replacement in [
        (
            "pub fn load_custom_client() {",
            '\n#[inline]\n#[cfg(target_os = "macos")]',
            "pub fn load_custom_client() {}\n",
        ),
        (
            "pub fn read_custom_client(config: &str) {",
            "\n#[inline]\npub fn is_empty_uni_link",
            "pub fn read_custom_client(_config: &str) {}\n",
        ),
    ]:
        text = changes[name]
        begin = text.index(start)
        finish = text.index(end, begin)
        changes.replace(name, text[begin:finish], replacement)
    name = "src/platform/windows.rs"
    text = changes[name]
    begin = text.index("pub fn get_license_from_exe_name() -> ResultType<CustomServer> {")
    finish = text.index("\n// We can't directly use", begin)
    changes.replace(
        name,
        text[begin:finish],
        """pub fn get_license_from_exe_name() -> ResultType<CustomServer> {
    hbb_common::bail!("GeodomDesk uses compiled server settings")
}
""",
    )
    begin = changes[name].index("// Prepare custom client update by copying staged custom.txt")
    finish = changes[name].index("\npub fn get_license_from_exe_name", begin)
    changes.replace(
        name,
        changes[name][begin:finish],
        "// Connection settings are compiled; updates do not stage external presets.\n"
        "pub fn prepare_custom_client_update() -> ResultType<bool> { Ok(true) }\n",
    )
