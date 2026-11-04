//! Same-server device import. Never copy source credentials or application settings.
use hbb_common::{
    config::{Config, LocalConfig, PeerConfig, PeerInfoSerde},
    directories_next::ProjectDirs,
    ResultType,
};
use serde_derive::Serialize;
use std::{collections::HashSet, fs};

mod data;

#[derive(Default, Serialize)]
pub(crate) struct ImportReport {
    added: usize,
    existing: usize,
    skipped: usize,
    favorites_added: usize,
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
            report.existing += 1;
            continue;
        }
        let mut peer = PeerConfig::default();
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
        if let Some(modified) = device.modified {
            if let Ok(file) = fs::OpenOptions::new()
                .write(true)
                .open(Config::path("peers").join(device.file_name))
            {
                let _ = file.set_times(fs::FileTimes::new().set_modified(modified));
            }
        }
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
    Ok(report)
}

#[cfg(any(target_os = "windows", target_os = "linux"))]
pub(crate) fn import_on_first_run() {
    const MARKER: &str = "geodomdesk-initial-import";
    static FIRST_RUN: std::sync::Once = std::sync::Once::new();
    FIRST_RUN.call_once(|| {
        if !LocalConfig::get_option(MARKER).is_empty() {
            return;
        }
        // Upgrades with existing user data retain their explicit import choice.
        let has_peers = fs::read_dir(Config::path("peers"))
            .map(|entries| {
                entries
                    .filter_map(Result::ok)
                    .any(|entry| entry.path().extension().and_then(|s| s.to_str()) == Some("toml"))
            })
            .unwrap_or(false);
        if !has_peers && LocalConfig::get_fav().is_empty() {
            if let Err(error) = import() {
                hbb_common::log::debug!("Initial device import skipped: {error}");
            }
        }
        LocalConfig::set_option(MARKER.to_owned(), "Y".to_owned());
    });
}
