//! Optional, one-way import. Never copy the source application's settings files.
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
    let data = data::read(project.config_dir())?;
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
        if (known.contains(&id) || PeerConfig::exists(&id)) && existing.insert(id.clone()) {
            favorites.push(id);
            report.favorites_added += 1;
        }
    }
    LocalConfig::set_fav(favorites);
    Ok(report)
}
