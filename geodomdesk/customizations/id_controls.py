"""Keep default-ID metadata with the host identity and use native ID controls."""
import json


def apply(changes, root):
    change = changes.replace
    name = "libs/hbb_common/src/config.rs"
    change(name, "    pub id: String, // use", """    pub id: String, // use
    #[serde(default, skip_serializing_if = "String::is_empty", deserialize_with = "deserialize_string")]
    default_id: String,""")
    change("libs/hbb_common/src/lib.rs", "pub mod compress;",
           "pub mod compress;\nmod geodomdesk_id_policy;")
    change(name, "                config.keys_confirmed.clear();\n                log::info!(\"Device hardware changed; replaced the copied client ID\");",
           "                config.keys_confirmed.clear();\n                config.default_id.clear();\n                log::info!(\"Device hardware changed; replaced the copied client ID\");")
    change(name, """        if store {
            config.store();
        }
        config
    }

    fn validate_or_decrypt_permanent_password_storage""", """        let default_id = crate::geodomdesk_id_policy::default_id(
            &config.id, &config.default_id, || {
                Self::gen_id().filter(|id| crate::geodomdesk_id_policy::is_numeric_id(id))
                    .or_else(|| Some(rand::thread_rng().gen_range(1_000_000_000..2_000_000_000).to_string()))
            },
        );
        if config.default_id != default_id {
            config.default_id = default_id;
            store = true;
        }
        if store {
            config.store();
        }
        config
    }

    fn validate_or_decrypt_permanent_password_storage""")
    change(name, "    pub fn set_id(id: &str) {", """    pub fn get_default_id() -> String {
        CONFIG.read().unwrap().default_id.clone()
    }

    pub fn set_id(id: &str) {""")
    change(name, """    pub fn set(cfg: Config) -> bool {
        let mut lock = CONFIG.write().unwrap();""", """    pub fn set(mut cfg: Config) -> bool {
        let mut lock = CONFIG.write().unwrap();
        // Default identity belongs to this host, not to a synchronized settings snapshot.
        cfg.default_id = lock.default_id.clone();""")
    change(name, """        Config::set_id(&new_id);
        log::info!("id updated from {} to {}", id, new_id);""", """        let mut config = CONFIG.write().unwrap();
        if config.default_id == id {
            config.default_id = new_id.clone();
        }
        config.id = new_id.clone();
        config.store();
        log::info!("id updated from {} to {}", id, new_id);""")
    change("src/lib.rs", "mod updater;", "mod updater;\nmod geodomdesk_identity;")
    change("src/ipc.rs", "                    value = Some(Config::get_id());\n                } else if name == \"temporary-password\" {",
           "                    value = Some(Config::get_id());\n                } else if name == \"geodomdesk-default-id\" {\n                    value = Some(Config::get_default_id());\n                } else if name == \"temporary-password\" {")
    change("src/ipc.rs", "async fn get_config_async(name: &str, ms_timeout: u64)",
           "pub(crate) async fn get_config_async(name: &str, ms_timeout: u64)")
    change("src/ipc.rs", "pub async fn set_config_async(name: &str, value: String) -> ResultType<()> {", """pub(crate) async fn set_id_verified(id: String) -> ResultType<()> {
    let mut connection = connect(1_000, "").await?;
    connection.send_config("id", id.clone()).await?;
    connection.send(&Data::Config(("id".to_owned(), None))).await?;
    let result = connection.next_timeout(1_000).await?;
    hbb_common::anyhow::ensure!(
        matches!(result, Some(Data::Config((name, Some(value)))) if name == "id" && value == id),
        "Host service did not confirm its new ID"
    );
    Ok(())
}

pub async fn set_config_async(name: &str, value: String) -> ResultType<()> {""")
    change("src/ui_interface.rs", """    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let uuid = Bytes::from(""", """    change_id_registered(id, old_id).await
}

pub(crate) async fn change_id_registered(id: String, old_id: String) -> &'static str {
    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let uuid = Bytes::from(""")
    change("src/ui_interface.rs", """        crate::ipc::set_config_async("id", id.to_owned()).await.ok();""", """        if crate::ipc::set_id_verified(id.to_owned()).await.is_err() {
            return "Failed to update local ID";
        }""")
    change("src/flutter_ffi.rs", "pub fn main_change_id(new_id: String) {", """pub async fn main_get_default_id() -> hbb_common::anyhow::Result<String> {
    crate::geodomdesk_identity::default_id().await
}

pub async fn main_restore_default_id() -> String {
    crate::geodomdesk_identity::restore().await.to_owned()
}

pub fn main_change_id(new_id: String) {""")
    name = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    change(name, "class _SafetyState extends State<_Safety> with AutomaticKeepAliveClientMixin {",
           "class _SafetyState extends State<_Safety> with AutomaticKeepAliveClientMixin {\n  bool _restoringDefaultId = false;")
    change(name, """          return _Button('Change ID', changeIdDialog,
              enabled: !locked && model.connectStatus > 0);""", """          final enabled = !locked && model.connectStatus > 0 && !_restoringDefaultId;
          return Wrap(runSpacing: 8, children: [
            _Button('Change ID', changeIdDialog, enabled: enabled, compact: true),
            _Button('Restore', _restoreDefaultId, enabled: enabled, compact: true),
          ]);""")
    change(name, "    {bool enabled = true, String? tip, ButtonStyle? style}) {",
           "    {bool enabled = true, String? tip, ButtonStyle? style, bool compact = false}) {")
    change(name, """  return Row(children: [
    child,
  ]).marginOnly(left: _kContentHMargin);""", """  return Row(mainAxisSize: compact ? MainAxisSize.min : MainAxisSize.max, children: [
    child,
  ]).marginOnly(left: _kContentHMargin);""")
    change(name, "  Widget changeId() {",
           (root / "client/restore_default_id.dart.in").read_text(encoding="utf-8") + "  Widget changeId() {")
    entries = {
        "Restore default ID": "恢复默认 ID",
        "Restore the automatically assigned ID %id%? Your passwords and settings will be kept.": "恢复为本机自动分配的默认 ID %id%？密码和其他设置将保留。",
        "Unable to read default ID": "无法读取默认 ID，请确认服务已启动。",
        "Failed to restore default ID": "恢复默认 ID 失败，请稍后重试。",
        "Failed to update local ID": "本机 ID 更新失败，请确认服务正常运行后重试。",
    }
    text = "".join("        (" + json.dumps(k) + ", " + json.dumps(v, ensure_ascii=False) + "),\n"
                   for k, v in entries.items())
    change("src/lang/cn.rs", "    [\n", "    [\n" + text)
