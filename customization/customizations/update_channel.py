"""Own signed updates; retain upstream signature and artifact verification."""

import base64
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
    crate::geodomdesk_update::release_id(&url)
        .and_then(|id| crate::geodomdesk_update::display_version(&id))
        .unwrap_or_default()
}
"""
        + changes[name][end:]
    )
    name = "src/common.rs"
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
        "let response_url = resp.url;",
        """let response_url = resp.url;
    if response_url.is_empty() {
        clear_software_update_url();
        return Ok(());
    }""",
    )
    change(name, "let latest_release_version = display_version_from_release_id(&release_id)?;", "")
    change(
        name,
        "if get_version_number(&latest_release_version) > get_version_number(crate::VERSION) {",
        "if crate::geodomdesk_update::is_newer_release(&release_id, crate::VERSION, crate::geodomdesk_update::BUILD_NUMBER)? {",
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
        + """// The dedicated HTTPS channel returns the upstream VersionCheckResponse shape.
#[tokio::main(flavor = "current_thread")]
pub async fn do_check_software_update() -> hbb_common::ResultType<()> {
    let result = async {
        let url = crate::geodomdesk_update::LATEST_URL;
        let proxy_conf = Config::get_socks();
        let tls_url = get_url_for_tls(url, &proxy_conf);
        let cached = get_cached_tls_type(tls_url);
        let tls_type = cached.unwrap_or(TlsType::Rustls);
        let client = create_http_client_async(tls_type, false);
        let request = client.get(crate::geodomdesk_update::LATEST_URL)
            .header("Cache-Control", "no-cache")
            .timeout(std::time::Duration::from_secs(15)).send().await;
        let (used_tls, response) = match request {
            Ok(response) => (tls_type, response),
            Err(error) if cached.is_none() && error.is_request() => {
                let client = create_http_client_async(TlsType::NativeTls, false);
                (TlsType::NativeTls, client.get(url).header("Cache-Control", "no-cache")
                    .timeout(std::time::Duration::from_secs(15)).send().await?)
            }
            Err(error) => return Err(error.into()),
        };
        upsert_tls_cache(tls_url, used_tls, false);
        let mut response = response.error_for_status()?;
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if bytes.len() + chunk.len() > 16384 {
                bail!("GeodomDesk update response exceeds size limit");
            }
            bytes.extend_from_slice(&chunk);
        }
        process_software_update_check_response(Bytes::from(bytes))
    }.await;
    if result.is_err() {
        clear_software_update_url();
    }
    result
}
"""
        + changes[name][end:]
    )

    change(
        "src/updater/artifact.rs",
        "https://github.com/rustdesk/rustdesk/releases/download/{release_id}/",
        "https://www.qcjcpt.com/downloads/geodomdesk/releases/{release_id}/",
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
            "https://www.qcjcpt.com/downloads/geodomdesk/releases/1.4.0/geodomdesk-1.4.0-x86_64.dmg",
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
            "https://www.qcjcpt.com/downloads/geodomdesk/releases/1.4.6",
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
    # This fork has its own verified update channel and must keep those controls.
    change(
        "flutter/lib/common.dart",
        """void checkUpdate() {
  if (!isWeb) {
    if (!bind.isCustomClient()) {""",
        """void checkUpdate() {
  if (!isWeb) {
    if (appName == 'GeodomDesk') {""",
    )
    change(
        "flutter/lib/desktop/pages/desktop_home_page.dart",
        """if (!bind.isCustomClient() &&
        updateUrl.isNotEmpty &&
        !isCardClosed &&
        bind.mainUriPrefixSync().contains('rustdesk'))""",
        """if (updateUrl.isNotEmpty && !isCardClosed)""",
    )
    change(
        "flutter/lib/desktop/pages/desktop_home_page.dart",
        "Uri.parse('https://rustdesk.com/download')",
        "Uri.parse(updateUrl)",
    )
    change(
        "flutter/lib/desktop/pages/desktop_home_page.dart",
        "'https://github.com/rustdesk/rustdesk/releases/tag/${bind.mainGetNewVersion()}'",
        "updateUrl",
    )
    change(
        "flutter/lib/desktop/pages/desktop_setting_page.dart",
        "if (!isWeb && !bind.isCustomClient())",
        "if (!isWeb)",
    )
    change(
        "flutter/lib/mobile/pages/connection_page.dart",
        "if (!bind.isCustomClient() && !isIOS)",
        "if (!isIOS)",
    )
    change(
        "flutter/lib/mobile/pages/settings_page.dart",
        "if (!bind.isCustomClient()) {",
        "if (appName == 'GeodomDesk') {",
    )
