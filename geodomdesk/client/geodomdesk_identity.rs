//! Restore the host service's saved default ID through the normal registration protocol.
#[cfg(any(target_os = "android", target_os = "ios"))]
use hbb_common::config::Config;
use hbb_common::ResultType;

pub(crate) async fn default_id() -> ResultType<String> {
    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let id = crate::ipc::get_config_async("geodomdesk-default-id", 1_000)
        .await?
        .unwrap_or_default();
    #[cfg(any(target_os = "android", target_os = "ios"))]
    let id = Config::get_default_id();
    hbb_common::anyhow::ensure!(!id.is_empty(), "Unable to read default ID");
    Ok(id)
}

pub(crate) async fn restore() -> &'static str {
    let Ok(id) = default_id().await else {
        return "Unable to read default ID";
    };
    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let old_id = match crate::ipc::get_config_async("id", 1_000).await {
        Ok(Some(id)) if !id.is_empty() => id,
        _ => return "Failed to restore default ID",
    };
    #[cfg(any(target_os = "android", target_os = "ios"))]
    let old_id = Config::get_id();
    if old_id == id {
        return "";
    }
    crate::ui_interface::change_id_registered(id, old_id).await
}
