"""Own signed updates; retain upstream signature and artifact verification."""

import base64
import json
import re


def apply(changes, cfg):
    change = changes.replace
    change("src/lib.rs", "mod updater;", "mod updater;\nmod geodomdesk_update;")
    name = "src/ui_interface.rs"
    start = changes[name].index("pub fn get_new_version() -> String {")
    end = changes[name].index("\n#[inline]", start)
    changes[name] = (
        changes[name][:start]
        + """pub fn get_new_version() -> String {
    let url = SOFTWARE_UPDATE_URL.lock().unwrap().clone();
    crate::geodomdesk_update::release_id(&url).unwrap_or_default()
}
"""
        + changes[name][end:]
    )
    change(
        name,
        """#[inline]
pub fn get_version() -> String {
    crate::VERSION.to_owned()
}""",
        """#[inline]
pub fn get_version() -> String {
    crate::geodomdesk_update::current_release_id(crate::VERSION)
}""",
    )
    name = "src/common.rs"
    change(
        name,
        "    pub static ref SOFTWARE_UPDATE_URL: Arc<Mutex<String>> = Default::default();",
        "    pub static ref SOFTWARE_UPDATE_URL: Arc<Mutex<String>> = Default::default();\n"
        "    static ref SOFTWARE_UPDATE_INFO: Arc<Mutex<crate::geodomdesk_update::SoftwareUpdateInfo>> = Default::default();",
    )
    change(
        name,
        """pub fn check_software_update() {
    if is_custom_client() {
        return;
    }""",
        """pub fn check_software_update() {""",
    )
    change(
        name,
        """fn clear_software_update_url() {
    let mut update_url = SOFTWARE_UPDATE_URL.lock().unwrap();
    *update_url = String::new();
}

fn process_software_update_check_response(bytes: Bytes) -> ResultType<()> {
    let resp: hbb_common::VersionCheckResponse = serde_json::from_slice(&bytes)?;
    let response_url = resp.url;
    let release_id = release_id_from_update_url(&response_url)?;
    let latest_release_version = display_version_from_release_id(&release_id)?;

    if get_version_number(&latest_release_version) > get_version_number(crate::VERSION) {
        #[cfg(feature = "flutter")]
        {
            let mut m = HashMap::new();
            m.insert("name", "check_software_update_finish");
            m.insert("url", &response_url);
            if let Ok(data) = serde_json::to_string(&m) {
                let _ = crate::flutter::push_global_event(crate::flutter::APP_TYPE_MAIN, data);
            }
        }
        *SOFTWARE_UPDATE_URL.lock().unwrap() = response_url;
    } else {
        clear_software_update_url();
    }
    Ok(())
}""",
        """fn clear_software_update_url() {
    *SOFTWARE_UPDATE_URL.lock().unwrap() = String::new();
    *SOFTWARE_UPDATE_INFO.lock().unwrap() = Default::default();
}

pub fn get_software_update_info_json() -> String {
    serde_json::to_string(&*SOFTWARE_UPDATE_INFO.lock().unwrap()).unwrap_or_default()
}

fn process_software_update_check_response(bytes: Bytes) -> ResultType<()> {
    process_software_update_check_response_inner(bytes, true).map(|_| ())
}

fn process_software_update_check_response_inner(
    bytes: Bytes,
    emit_event: bool,
) -> ResultType<Option<crate::geodomdesk_update::SoftwareUpdateInfo>> {
    let resp: hbb_common::VersionCheckResponse = serde_json::from_slice(&bytes)?;
    let response_url = resp.url;
    if response_url.is_empty() {
        clear_software_update_url();
        return Ok(None);
    }
    let release_id = release_id_from_update_url(&response_url)?;
    #[cfg(windows)]
    if crate::updater::current_update_platform() == "windows-server-2008-r2" {
        let pointer: serde_json::Value = serde_json::from_slice(&bytes)?;
        if pointer["windows_server_2008_r2_url"].as_str().is_none() {
            clear_software_update_url();
            return Ok(None);
        }
    }

    if crate::geodomdesk_update::is_newer_release(&release_id, crate::VERSION, crate::geodomdesk_update::BUILD_NUMBER)? {
        let info = crate::geodomdesk_update::parse_pointer_info(&bytes, &response_url, &release_id);
        *SOFTWARE_UPDATE_URL.lock().unwrap() = response_url.clone();
        *SOFTWARE_UPDATE_INFO.lock().unwrap() = info.clone();
        #[cfg(feature = "flutter")]
        if emit_event {
            let payload = serde_json::json!({
                "name": "check_software_update_finish",
                "url": info.url,
                "release_id": info.release_id,
                "version": info.version,
                "changelog": info.changelog,
                "published_at": info.published_at,
            });
            if let Ok(data) = serde_json::to_string(&payload) {
                let _ = crate::flutter::push_global_event(crate::flutter::APP_TYPE_MAIN, data);
            }
        }
        #[cfg(not(feature = "flutter"))]
        let _ = emit_event;
        Ok(Some(info))
    } else {
        clear_software_update_url();
        Ok(None)
    }
}""",
    )
    start = changes[name].index("pub(crate) fn release_id_from_update_url(")
    end = changes[name].index("pub(crate) fn url_has_explicit_port(", start)
    changes[name] = (
        changes[name][:start]
        + """pub(crate) fn release_id_from_update_url(update_url: &str) -> ResultType<String> {
    crate::geodomdesk_update::release_id(update_url)
}

"""
        + changes[name][end:]
    )
    start = changes[name].index("// No need to check `danger_accept_invalid_cert` for now.")
    end = changes[name].index("\n#[inline]\npub fn get_app_name()", start)
    changes[name] = (
        changes[name][:start]
        + """async fn fetch_software_update_pointer() -> hbb_common::ResultType<Bytes> {
    let url = crate::geodomdesk_update::LATEST_URL;
    let proxy_conf = Config::get_socks();
    let tls_url = get_url_for_tls(url, &proxy_conf);
    let cached = get_cached_tls_type(tls_url);
    let tls_type = cached.unwrap_or(TlsType::Rustls);
    let client = create_http_client_async(tls_type, false);
    let request = tokio::time::timeout(
        std::time::Duration::from_secs(8),
        client
            .get(crate::geodomdesk_update::LATEST_URL)
            .header("Cache-Control", "no-cache")
            .header("Pragma", "no-cache")
            .timeout(std::time::Duration::from_secs(15))
            .send(),
    )
    .await;
    let (used_tls, response) = match request {
        Ok(Ok(response)) => (tls_type, response),
        Ok(Err(error)) if error.is_timeout() => bail!("GeodomDesk update check timed out"),
        Ok(Err(error)) if cached.is_none() && error.is_request() => {
            let client = create_http_client_async(TlsType::NativeTls, false);
            let retry = tokio::time::timeout(
                std::time::Duration::from_secs(8),
                client
                    .get(url)
                    .header("Cache-Control", "no-cache")
                    .header("Pragma", "no-cache")
                    .timeout(std::time::Duration::from_secs(15))
                    .send(),
            )
            .await
            .map_err(|_| anyhow!("GeodomDesk update check timed out"))?
            .map_err(|e| {
                if e.is_timeout() {
                    anyhow!("GeodomDesk update check timed out")
                } else {
                    e.into()
                }
            })?;
            (TlsType::NativeTls, retry)
        }
        Ok(Err(error)) => return Err(error.into()),
        Err(_) => bail!("GeodomDesk update check timed out"),
    };
    if response.url().scheme() != "https" {
        bail!("GeodomDesk update redirect must use HTTPS");
    }
    upsert_tls_cache(tls_url, used_tls, false);
    let mut response = response.error_for_status()?;
    let mut bytes = Vec::new();
    while let Some(chunk) = tokio::time::timeout(
        std::time::Duration::from_secs(15),
        response.chunk(),
    )
    .await
    .map_err(|_| anyhow!("GeodomDesk update response timed out"))??
    {
        if bytes.len() + chunk.len() > crate::geodomdesk_update::MAX_POINTER_BYTES {
            bail!("GeodomDesk update response exceeds size limit");
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(Bytes::from(bytes))
}

// The dedicated HTTPS channel returns the upstream VersionCheckResponse shape.
#[tokio::main(flavor = "current_thread")]
pub async fn do_check_software_update() -> hbb_common::ResultType<()> {
    let bytes = fetch_software_update_pointer().await?;
    process_software_update_check_response_inner(bytes, true).map(|_| ())
}

#[tokio::main(flavor = "current_thread")]
async fn run_manual_software_update_check() -> hbb_common::ResultType<Option<crate::geodomdesk_update::SoftwareUpdateInfo>> {
    let bytes = fetch_software_update_pointer().await?;
    process_software_update_check_response_inner(bytes, false)
}

pub fn check_software_update_manual() -> String {
    let outcome = std::thread::Builder::new()
        .name("geodom-update-check".to_owned())
        .spawn(run_manual_software_update_check)
        .map_err(|e| anyhow!("Failed to start update check: {e}"))
        .and_then(|handle| {
            handle
                .join()
                .map_err(|_| anyhow!("Update check worker panicked"))?
        });
    match outcome {
        Ok(Some(info)) => serde_json::json!({
            "status": "update_available",
            "url": info.url,
            "release_id": info.release_id,
            "version": info.version,
            "changelog": info.changelog,
            "published_at": info.published_at,
        })
        .to_string(),
        Ok(None) => serde_json::json!({ "status": "up_to_date" }).to_string(),
        Err(err) => {
            let timed_out = err.to_string().to_lowercase().contains("timed out");
            serde_json::json!({
                "status": "error",
                "error": err.to_string(),
                "timed_out": timed_out,
            })
            .to_string()
        }
    }
}
"""
        + changes[name][end:]
    )
    ffi = "src/flutter_ffi.rs"
    change(
        ffi,
        """    } else if key == "local-permanent-password-set" {
        return ui_interface::is_local_permanent_password_set().to_string();
    } else {""",
        """    } else if key == "local-permanent-password-set" {
        return ui_interface::is_local_permanent_password_set().to_string();
    } else if key == "software-update-info" {
        return crate::common::get_software_update_info_json();
    } else if key == "check-software-update" {
        return crate::common::check_software_update_manual();
    } else {""",
    )
    change(
        ffi,
        """    // Metadata verification performs blocking network requests. Keep this key on
    // the asynchronous FFI path to avoid blocking the calling Flutter isolate.
    if key.starts_with("verified-download-url-") {
        return SyncReturn("error:unsupported".to_owned());
    }""",
        """    // Metadata verification and manual update checks perform blocking network requests.
    // Keep these keys on the asynchronous FFI path to avoid blocking the calling Flutter isolate.
    if key.starts_with("verified-download-url-") || key == "check-software-update" {
        return SyncReturn("error:unsupported".to_owned());
    }""",
    )

    base_url = cfg["base_url"].rstrip("/")
    change(
        "src/updater/artifact.rs",
        """    #[cfg(target_os = "windows")]
    {
        "windows"
    }""",
        """    #[cfg(target_os = "windows")]
    {
        if crate::platform::is_win_10_or_greater() {
            "windows"
        } else {
            "windows-server-2008-r2"
        }
    }""",
    )
    change(
        "src/updater/artifact.rs",
        "https://github.com/rustdesk/rustdesk/releases/download/{release_id}/",
        f"{base_url}/releases/{{release_id}}/",
    )
    change(
        "src/updater/artifact.rs",
        "let display_version = display_version_from_release_id(&release_id)?;",
        "let display_version = crate::geodomdesk_update::display_version(&release_id)?;",
    )
    change(
        "src/updater/artifact.rs",
        "{expected_artifact_url_prefix}rustdesk-update.json",
        "{expected_artifact_url_prefix}geodomdesk-update.json",
    )
    name = "src/updater/artifact.rs"
    start = changes[name].index("pub fn get_update_download_file_from_url(")
    end = changes[name].index("\nfn is_plain_update_filename(", start)
    changes[name] = (
        changes[name][:start]
        + """pub fn get_update_download_file_from_url(url: &str) -> Option<PathBuf> {
    let filename = crate::geodomdesk_update::artifact_file(url)?;
    if !is_plain_update_filename(filename) {
        return None;
    }
    Some(std::env::temp_dir().join(filename))
}
"""
        + changes[name][end:]
    )
    # Upstream's metadata verification, hashed download and privileged update remain intact.
    name = "src/update_metadata.rs"
    change(name, 'b"RustDesk update metadata v1\\n"', 'b"GeodomDesk update metadata v1\\n"')
    change(name, 'const UPDATE_APP: &str = "rustdesk";', 'const UPDATE_APP: &str = "geodomdesk";')
    change(
        name,
        'const PUBLIC_UPDATE_PACKAGE_ID: &str = "rustdesk";',
        'const PUBLIC_UPDATE_PACKAGE_ID: &str = "geodomdesk";',
    )
    change(name, 'key_id: "2026-ed25519-main"', 'key_id: "' + cfg["key_id"] + '"')
    change(
        name,
        """    signature_key_id: String,
    artifacts: Vec<UpdateArtifact>,""",
        """    signature_key_id: String,
    #[serde(default)]
    changelog: String,
    artifacts: Vec<UpdateArtifact>,""",
    )
    change(
        name,
        """pub(crate) struct VerifiedUpdateArtifact {
    pub version: String,
    pub url: String,""",
        """pub(crate) struct VerifiedUpdateArtifact {
    pub version: String,
    #[allow(dead_code)]
    pub changelog: String,
    pub url: String,""",
    )
    change(
        name,
        """    Ok(VerifiedUpdateArtifact {
        version: metadata.version.clone(),
        url: artifact.url.clone(),""",
        """    Ok(VerifiedUpdateArtifact {
        version: metadata.version.clone(),
        changelog: metadata.changelog.trim().to_owned(),
        url: artifact.url.clone(),""",
    )
    key = base64.b64decode(cfg["public_key"], validate=True)
    if len(key) != 32:
        raise ValueError("Expected Ed25519 public key")
    changes[name], count = re.subn(
        r"public_key: \[\s*52, 214,.*?\],",
        "public_key: [" + ", ".join(map(str, key)) + "],",
        changes[name],
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise ValueError("Trusted key block changed")
    # Keep upstream unit tests meaningful with the new app identity/channel.
    name = "src/updater/artifact.rs"
    changes[name] = (
        changes[name]
        .replace(
            "https://github.com/rustdesk/rustdesk/releases/download/1.4.0/rustdesk-1.4.0-x86_64.dmg",
            f"{base_url}/releases/1.4.0/geodomdesk-1.4.0-x86_64.dmg",
        )
        .replace('Some("rustdesk-1.4.0-x86_64.dmg")', 'Some("geodomdesk-1.4.0-x86_64.dmg")')
    )
    name = "src/common.rs"
    start = changes[name].index("    fn parses_stable_rustdesk_release_url()")
    end = changes[name].index("\n    #[test]", start)
    changes[name] = (
        changes[name][:start]
        + changes[name][start:end]
        .replace(
            "https://github.com/rustdesk/rustdesk/releases/tag/v1.4.6",
            f"{base_url}/releases/1.4.6",
        )
        .replace('"v1.4.6"', '"1.4.6"')
        + changes[name][end:]
    )
    name = "src/update_metadata_tests.rs"
    changes[name] = (
        changes[name]
        .replace('"app":"rustdesk"', '"app":"geodomdesk"')
        .replace('"package_id":"rustdesk"', '"package_id":"geodomdesk"')
    )
    # Re-sign the static cross-language fixture with its public, deterministic test key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    fixture = re.search(r'let metadata = br#"(.*?)"#;', changes[name]).group(1).encode()
    test_signature = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).sign(
        b"GeodomDesk update metadata v1\n" + fixture
    )
    changes[name] = changes[name].replace(
        "ZMcod9VNaEGTYK0gIfGmMQ44HAvrYAkYDyIL9JTKjzUH+hVFYs8KpvnGSlteAHwqiJuJDdiKBVCPEdtcRRAJCA==",
        base64.b64encode(test_signature).decode(),
    )
    # The upstream UI hides its public update channel for vendor-signed clients.
    # This fork has its own verified update channel and must keep those controls,
    # popping up the changelog dialog before asking the user to confirm an update.
    change(
        "flutter/lib/common.dart",
        "import 'common/widgets/overlay.dart';\n",
        "import 'common/widgets/overlay.dart';\nimport 'common/widgets/geodom_update_dialog.dart';\n",
    )
    change(
        "flutter/lib/common.dart",
        "  void dismissAll() {\n",
        "  bool get hasAny => _dialogs.isNotEmpty;\n\n  void dismissAll() {\n",
    )
    change(
        "flutter/lib/common.dart",
        """void checkUpdate() {
  if (!isWeb) {
    if (!bind.isCustomClient()) {
      platformFFI.registerEventHandler(
          kCheckSoftwareUpdateFinish, kCheckSoftwareUpdateFinish,
          (Map<String, dynamic> evt) async {
        if (evt['url'] is String) {
          stateGlobal.updateUrl.value = evt['url'];
        }
      });""",
        """void checkUpdate() {
  if (!isWeb) {
    if (appName == 'GeodomDesk') {
      platformFFI.registerEventHandler(
          kCheckSoftwareUpdateFinish, kCheckSoftwareUpdateFinish,
          (Map<String, dynamic> evt) async {
        await handleGeodomUpdateCheckEvent(evt);
      });""",
    )
    home = "flutter/lib/desktop/pages/desktop_home_page.dart"
    change(
        home,
        "import 'package:flutter_hbb/desktop/widgets/update_progress.dart';\n",
        "import 'package:flutter_hbb/common/widgets/geodom_update_dialog.dart';\n",
    )
    change(
        home,
        """    if (!bind.isCustomClient() &&
        updateUrl.isNotEmpty &&
        !isCardClosed &&
        bind.mainUriPrefixSync().contains('rustdesk')) {
      final isToUpdate = (isWindows || isMacOS) && bind.mainIsInstalled();
      String btnText = isToUpdate ? 'Update' : 'Download';
      GestureTapCallback onPressed = () async {
        final Uri url = Uri.parse('https://rustdesk.com/download');
        await launchUrl(url);
      };
      if (isToUpdate) {
        onPressed = () {
          handleUpdate(updateUrl);
        };
      }
      return buildInstallCard(
          "Status",
          "${translate("new-version-of-{${bind.mainGetAppNameSync()}}-tip")} (${bind.mainGetNewVersion()}).",
          btnText,
          onPressed,
          closeButton: true,
          help: isToUpdate ? 'Changelog' : null,
          link: isToUpdate
              ? 'https://github.com/rustdesk/rustdesk/releases/tag/${bind.mainGetNewVersion()}'
              : null);
    }""",
        """    if (updateUrl.isNotEmpty && !isCardClosed) {
      final isToUpdate = (isWindows || isMacOS) && bind.mainIsInstalled();
      String btnText = isToUpdate ? 'Update' : 'Download';
      GestureTapCallback onPressed = () {
        showGeodomUpdateDialogFromCurrent(updateUrl);
      };
      return buildInstallCard(
          "Status",
          "${translate("new-version-of-{${bind.mainGetAppNameSync()}}-tip")} (${bind.mainGetNewVersion()}).",
          btnText,
          onPressed,
          closeButton: true,
          help: 'Changelog',
          link: updateUrl,
          onHelpTap: () => showGeodomUpdateDialogFromCurrent(updateUrl));
    }""",
    )
    change(
        home,
        """      String? help,
      String? link,
      bool? closeButton,
      String? closeOption}) {""",
        """      String? help,
      String? link,
      VoidCallback? onHelpTap,
      bool? closeButton,
      String? closeOption}) {""",
    )
    change(
        home,
        """                                  child: InkWell(
                                      onTap: () async =>
                                          await launchUrl(Uri.parse(link!)),""",
        """                                  child: InkWell(
                                      onTap: onHelpTap ??
                                          () async =>
                                              await launchUrl(Uri.parse(link!)),""",
    )
    desktop_settings = "flutter/lib/desktop/pages/desktop_setting_page.dart"
    change(
        desktop_settings,
        "import 'package:flutter_hbb/common/widgets/setting_widgets.dart';\n",
        "import 'package:flutter_hbb/common/widgets/setting_widgets.dart';\n"
        "import 'package:flutter_hbb/common/widgets/geodom_update_dialog.dart';\n",
    )
    change(desktop_settings, "if (!isWeb && !bind.isCustomClient())", "if (!isWeb)")
    change(
        desktop_settings,
        """              SelectionArea(
                  child: Text('${translate('Build Date')}: $buildDate')
                      .marginSymmetric(vertical: 4.0)),""",
        """              SelectionArea(
                  child: Text('${translate('Build Date')}: $buildDate')
                      .marginSymmetric(vertical: 4.0)),
              if (!isWeb)
                OutlinedButton.icon(
                  icon: const Icon(Icons.system_update_alt, size: 16),
                  label: Text(translate('Check for updates')),
                  onPressed: () => checkGeodomUpdateManually(),
                ).marginSymmetric(vertical: 6.0),""",
    )
    conn_page = "flutter/lib/mobile/pages/connection_page.dart"
    change(
        conn_page,
        "import '../../common.dart';\n",
        "import '../../common.dart';\nimport '../../common/widgets/geodom_update_dialog.dart';\n",
    )
    change(conn_page, "import 'package:url_launcher/url_launcher.dart';\n", "")
    change(conn_page, "if (!bind.isCustomClient() && !isIOS)", "if (!isIOS)")
    if "final url = 'https://rustdesk.com/download';" in changes[conn_page]:
        change(conn_page, "final url = 'https://rustdesk.com/download';", "final url = updateUrl;")
    change(
        conn_page,
        "              await launchUrl(Uri.parse(url));",
        "              await showGeodomUpdateDialogFromCurrent(url);",
    )
    mobile_settings = "flutter/lib/mobile/pages/settings_page.dart"
    change(
        mobile_settings,
        "import '../../common/widgets/dialog.dart';\n",
        "import '../../common/widgets/dialog.dart';\nimport '../../common/widgets/geodom_update_dialog.dart';\n",
    )
    change(mobile_settings, "if (!bind.isCustomClient()) {", "if (appName == 'GeodomDesk') {")
    change(
        mobile_settings,
        """            SettingsTile(
                title: Text(translate("Build Date")),
                value: Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text(_buildDate),
                ),
                leading: Icon(Icons.query_builder)),""",
        """            SettingsTile(
                title: Text(translate("Build Date")),
                value: Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text(_buildDate),
                ),
                leading: Icon(Icons.query_builder)),
            if (!isIOS && !isWeb)
              SettingsTile(
                onPressed: (context) => checkGeodomUpdateManually(),
                title: Text(translate('Check for updates')),
                leading: Icon(Icons.system_update_alt),
              ),""",
    )
    translations = {
        "New version available": "发现新版本",
        "Current version": "当前版本",
        "New version": "新版本",
        "Later": "稍后再说",
        "Install update": "立即更新",
        "Skip this version": "跳过此版本",
        "Check for updates": "检查更新",
        "Checking for updates...": "正在检查更新...",
        "Already up to date": "当前已是最新版本",
        "Failed to check for updates": "检查更新失败，请稍后重试",
        "Update check timed out, please try again later": "检查更新超时，请稍后重试",
        "Unable to open download page": "无法打开下载页面，请稍后重试",
        "Confirm to verify and install this update now?": "确认后将校验签名并开始下载安装此更新。",
        "Confirm to open the download page for this version?": "确认后将打开新版本下载页面。",
        "This update includes stability improvements, performance optimizations, and bug fixes.": "本次更新包含稳定性改进、性能优化及问题修复。",
    }
    entries = "".join(
        "        ("
        + json.dumps(key, ensure_ascii=False)
        + ", "
        + json.dumps(value, ensure_ascii=False)
        + "),\n"
        for key, value in translations.items()
    )
    change("src/lang/cn.rs", "    [\n", "    [\n" + entries)
