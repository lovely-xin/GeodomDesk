//! Read only the device fields that may cross application boundaries.
use hbb_common::{bail, sodiumoxide::base64, toml, ResultType};
use serde::de::DeserializeOwned;
use serde_derive::Deserialize;
use std::{fs, io::Read, path::Path, time::SystemTime};

#[derive(Default, Deserialize)]
#[serde(default)]
pub(super) struct DeviceInfo {
    pub username: String,
    pub hostname: String,
    pub platform: String,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Notes {
    alias: String,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Peer {
    info: DeviceInfo,
    options: Notes,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Local {
    fav: Vec<String>,
}

pub(super) struct Device {
    pub id: String,
    pub info: DeviceInfo,
    pub alias: String,
    pub file_name: std::ffi::OsString,
    pub modified: Option<SystemTime>,
}

#[derive(Default)]
pub(super) struct UserData {
    pub devices: Vec<Device>,
    pub favorites: Vec<String>,
    pub skipped: usize,
}

fn read_toml<T: DeserializeOwned>(path: &Path) -> ResultType<T> {
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        bail!("RustDesk user data file exceeds the import limit");
    }
    // Do not expose parser errors: a malformed file can contain credentials.
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| hbb_common::anyhow::anyhow!("Invalid user data encoding"))?;
    toml::from_str(text).map_err(|_| hbb_common::anyhow::anyhow!("Invalid RustDesk user data"))
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 256
        && id.trim() == id
        && id != "."
        && id != ".."
        && !id.chars().any(|c| c.is_control() || c == '/' || c == '\\')
}

fn device(path: &Path) -> ResultType<Device> {
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or_default();
    let id = if let Some(encoded) = stem.strip_prefix("base64_") {
        String::from_utf8(
            base64::decode(encoded, base64::Variant::Original)
                .map_err(|_| hbb_common::anyhow::anyhow!("Invalid device ID"))?,
        )?
    } else {
        stem.to_owned()
    };
    if !valid_id(&id) {
        bail!("Invalid device ID");
    }
    let peer: Peer = read_toml(path)?;
    Ok(Device {
        id,
        info: peer.info,
        alias: peer.options.alias,
        file_name: path.file_name().unwrap().to_owned(),
        modified: path.metadata()?.modified().ok(),
    })
}

pub(super) fn read(source: &Path) -> ResultType<UserData> {
    if !source.is_dir() {
        bail!("RustDesk user data was not found");
    }
    let local = source.join("RustDesk_local.toml");
    let mut data = UserData::default();
    if local.is_file() {
        let local: Local = read_toml(&local)?;
        data.favorites = local.fav.into_iter().filter(|id| valid_id(id)).collect();
    }
    let peers = source.join("peers");
    if peers.is_dir() {
        for entry in fs::read_dir(peers)? {
            let entry = entry?;
            if entry.path().extension().and_then(|s| s.to_str()) != Some("toml") {
                continue;
            }
            if !entry.file_type()?.is_file() {
                data.skipped += 1;
                continue;
            }
            match device(&entry.path()) {
                Ok(device) => data.devices.push(device),
                Err(_) => data.skipped += 1,
            }
        }
    }
    data.devices.sort_by_key(|device| device.modified);
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_device_data_without_credentials_or_settings() {
        let root =
            std::env::temp_dir().join(format!("geodomdesk-import-test-{}", std::process::id()));
        fs::create_dir_all(root.join("peers")).unwrap();
        fs::write(
            root.join("RustDesk_local.toml"),
            "fav = ['123456', '../unsafe']\n[options]\naccess_token = 'not-imported'\n",
        )
        .unwrap();
        fs::write(root.join("peers/123456.toml"), "password = [1,2,3]\nview_style = 'adaptive'\n[info]\nusername = 'User'\nhostname = 'Workstation'\nplatform = 'Windows'\n[options]\nalias = 'Desk'\nkey = 'not-imported'\nrdp_password = 'not-imported'\n").unwrap();
        fs::write(root.join("peers/bad.toml"), "invalid [").unwrap();
        let data = read(&root).unwrap();
        assert_eq!(data.devices.len(), 1);
        assert_eq!(data.devices[0].id, "123456");
        assert_eq!(data.devices[0].alias, "Desk");
        assert_eq!(data.devices[0].info.hostname, "Workstation");
        assert_eq!(data.favorites, ["123456"]);
        assert_eq!(data.skipped, 1);
        assert!(fs::read_to_string(root.join("peers/123456.toml"))
            .unwrap()
            .contains("rdp_password"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_path_ids_and_does_not_leak_parser_input() {
        for id in ["", ".", "..", "../device", "device\\file", "123\n"] {
            assert!(!valid_id(id));
        }
        assert!(valid_id("123456@server:21116"));
        let root =
            std::env::temp_dir().join(format!("geodomdesk-import-error-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        fs::write(
            root.join("RustDesk_local.toml"),
            "access_token = 'sensitive_unterminated",
        )
        .unwrap();
        let error = read(&root).err().unwrap().to_string();
        assert!(!error.contains("sensitive_unterminated"));
        fs::remove_dir_all(root).unwrap();
    }
}
