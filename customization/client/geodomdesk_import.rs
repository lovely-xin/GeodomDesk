//! Same-server device import. Host credentials and application settings stay isolated.
use hbb_common::{
    config::{Config, LocalConfig, PeerConfig, PeerInfoSerde},
    directories_next::ProjectDirs,
    ResultType,
};
use serde_derive::Serialize;
use std::{collections::HashSet, fs};

mod data;
const INITIAL_IMPORT: &str = "geodomdesk-initial-import";

#[derive(Default, Serialize)]
pub(crate) struct ImportReport {
    added: usize,
    existing: usize,
    skipped: usize,
    favorites_added: usize,
    passwords_added: usize,
    passwords_skipped: usize,
}

pub(crate) fn import() -> ResultType<ImportReport> {
    let project = ProjectDirs::from("", "", "RustDesk")
        .ok_or_else(|| hbb_common::anyhow::anyhow!("RustDesk user directory is unavailable"))?;
    let key = Config::get_option("key");
    let data = data::read(project.config_dir(), &key)?;
    let mut report = ImportReport {
        skipped: data.skipped,
        ..Default::default()
    };
    let mut known = HashSet::new();
    for device in data.devices {
        known.insert(device.id.clone());
        if PeerConfig::exists(&device.id) {
            let mut peer = PeerConfig::load(&device.id);
            if peer.password.is_empty() && !device.password.is_empty() {
                if let Some(password) = saved_password(&device.password) {
                    let modified = Config::path("peers").join(&device.file_name)
                        .metadata().and_then(|m| m.modified()).ok();
                    peer.password = password;
                    peer.store(&device.id);
                    if PeerConfig::load(&device.id).password != peer.password {
                        hbb_common::bail!("Could not store imported device password");
                    }
                    preserve_modified(&device.file_name, modified);
                    report.passwords_added += 1;
                } else {
                    report.passwords_skipped += 1;
                }
            }
            report.existing += 1;
            continue;
        }
        let mut peer = PeerConfig::default();
        if !device.password.is_empty() {
            if let Some(password) = saved_password(&device.password) {
                peer.password = password;
                report.passwords_added += 1;
            } else {
                report.passwords_skipped += 1;
            }
        }
        peer.info = PeerInfoSerde {
            username: device.info.username,
            hostname: device.info.hostname,
            platform: device.info.platform,
        };
        if !device.alias.is_empty() {
            peer.options.insert("alias".to_owned(), device.alias);
        }
        if device.other_server {
            // Cross-server IDs need the same verified key when reconnecting.
            peer.options
                .insert("other-server-key".to_owned(), key.clone());
        }
        peer.store(&device.id);
        if !PeerConfig::exists(&device.id) {
            hbb_common::bail!("Could not store imported device records");
        }
        if !peer.password.is_empty() && PeerConfig::load(&device.id).password != peer.password {
            hbb_common::bail!("Could not store imported device password");
        }
        preserve_modified(&device.file_name, device.modified);
        report.added += 1;
    }
    let mut favorites = LocalConfig::get_fav();
    let mut existing: HashSet<_> = favorites.iter().cloned().collect();
    for id in data.favorites {
        if known.contains(&id) && existing.insert(id.clone()) {
            favorites.push(id);
            report.favorites_added += 1;
        }
    }
    LocalConfig::set_fav(favorites);
    if LocalConfig::get_option(INITIAL_IMPORT) == "pending" {
        LocalConfig::set_option(INITIAL_IMPORT.to_owned(), "Y".to_owned());
    }
    Ok(report)
}

fn saved_password(stored: &[u8]) -> Option<Vec<u8>> {
    use hbb_common::{password_security::decrypt_vec_or_original, sodiumoxide::base64};
    let encrypted = stored.strip_prefix(b"00")
        .and_then(|data| base64::decode(data, base64::Variant::Original).ok())
        .is_some_and(|data| data.len() >= 16);
    let (password, decrypted, _) = decrypt_vec_or_original(stored, "00");
    (!encrypted || decrypted)
        .then_some(password)
        .filter(|password| !password.is_empty() && password.len() <= hbb_common::config::ENCRYPT_MAX_LEN)
}

fn preserve_modified(name: &std::ffi::OsStr, modified: Option<std::time::SystemTime>) {
    if let Some(modified) = modified {
        if let Ok(file) = fs::OpenOptions::new().write(true).open(Config::path("peers").join(name)) {
            let _ = file.set_times(fs::FileTimes::new().set_modified(modified));
        }
    }
}

#[cfg(any(target_os = "windows", target_os = "linux"))]
pub(crate) fn import_on_first_run() {
    static FIRST_RUN: std::sync::Once = std::sync::Once::new();
    FIRST_RUN.call_once(|| {
        let state = LocalConfig::get_option(INITIAL_IMPORT);
        if !state.is_empty() && state != "pending" {
            return;
        }
        // Upgrades with existing user data retain their explicit import choice.
        if state.is_empty() {
            let has_peers = fs::read_dir(Config::path("peers"))
                .map(|entries| {
                    entries.filter_map(Result::ok).any(|entry| {
                        entry.path().extension().and_then(|s| s.to_str()) == Some("toml")
                    })
                })
                .unwrap_or(false);
            if has_peers || !LocalConfig::get_fav().is_empty() {
                LocalConfig::set_option(INITIAL_IMPORT.to_owned(), "Y".to_owned());
                return;
            }
        }
        let source_exists = ProjectDirs::from("", "", "RustDesk")
            .map(|project| project.config_dir().is_dir())
            .unwrap_or(false);
        if source_exists {
            // Persist before the first device write so a stopped migration can resume.
            LocalConfig::set_option(INITIAL_IMPORT.to_owned(), "pending".to_owned());
            if let Err(error) = import() {
                hbb_common::log::debug!("Initial device import deferred: {error}");
                return;
            }
        } else if state == "pending" {
            return;
        }
        LocalConfig::set_option(INITIAL_IMPORT.to_owned(), "Y".to_owned());
    });
}
