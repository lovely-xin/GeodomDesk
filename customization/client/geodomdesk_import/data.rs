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
    #[serde(rename = "other-server-key")]
    other_server_key: String,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct ServerOptions {
    key: String,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Server {
    options: ServerOptions,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Peer {
    password: Vec<u8>,
    info: DeviceInfo,
    options: Notes,
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct Local {
    fav: Vec<String>,
}

pub(super) struct Device {
    pub password: Vec<u8>,
    pub id: String,
    pub info: DeviceInfo,
    pub alias: String,
    pub file_name: std::ffi::OsString,
    pub modified: Option<SystemTime>,
    pub other_server: bool,
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

fn same_key(candidate: &str, expected: &[u8]) -> bool {
    let candidate = candidate.trim();
    base64::decode(candidate, base64::Variant::Original)
        .or_else(|_| base64::decode(candidate, base64::Variant::OriginalNoPadding))
        .map(|key| key.len() == 32 && key == expected)
        .unwrap_or(false)
}

fn matches_server(id: &str, notes: &Notes, source_key: &str, expected: &[u8]) -> bool {
    if let Some((_, server)) = id.split_once('@') {
        let (server, args) = server.split_once('?').unwrap_or((server, ""));
        if server.is_empty() || server == "public" || server.contains('@') {
            return false;
        }
        let explicit = args
            .split('&')
            .filter_map(|arg| arg.split_once('='))
            .filter(|(key, _)| key.eq_ignore_ascii_case("key"))
            .map(|(_, value)| value)
            .last();
        return same_key(explicit.unwrap_or(&notes.other_server_key), expected);
    }
    // Direct IP/domain connections have no ID-server public-key scope.
    !id.contains(['.', ':']) && same_key(source_key, expected)
}

fn device(path: &Path, source_key: &str, expected: &[u8]) -> ResultType<Device> {
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
    if !matches_server(&id, &peer.options, source_key, expected) {
        bail!("Device ID-server public key does not match");
    }
    let other_server = id.contains('@');
    Ok(Device {
        password: peer.password,
        id,
        info: peer.info,
        alias: peer.options.alias,
        file_name: path.file_name().unwrap().to_owned(),
        modified: path.metadata()?.modified().ok(),
        other_server,
    })
}

pub(super) fn read(source: &Path, expected_key: &str) -> ResultType<UserData> {
    if !source.is_dir() {
        bail!("RustDesk user data was not found");
    }
    let expected = base64::decode(expected_key.trim(), base64::Variant::Original)
        .or_else(|_| base64::decode(expected_key.trim(), base64::Variant::OriginalNoPadding))
        .map_err(|_| hbb_common::anyhow::anyhow!("Invalid configured ID-server public key"))?;
    if expected.len() != 32 {
        bail!("Invalid configured ID-server public key");
    }
    let server = source.join("RustDesk2.toml");
    let source_key = if server.is_file() {
        read_toml::<Server>(&server)?.options.key
    } else {
        String::new()
    };
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
            match device(&entry.path(), &source_key, &expected) {
                Ok(device) => data.devices.push(device),
                Err(_) => data.skipped += 1,
            }
        }
    }
    data.devices.sort_by_key(|device| device.modified);
    let matched: std::collections::HashSet<_> =
        data.devices.iter().map(|d| d.id.as_str()).collect();
    data.favorites.retain(|id| matched.contains(id.as_str()));
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;
    const KEY: &str = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=";

    #[test]
    fn reads_saved_connection_password_without_host_or_account_settings() {
        let root =
            std::env::temp_dir().join(format!("geodomdesk-import-test-{}", std::process::id()));
        fs::create_dir_all(root.join("peers")).unwrap();
        fs::write(
            root.join("RustDesk2.toml"),
            format!("[options]\nkey = '{KEY}'\n"),
        )
        .unwrap();
        fs::write(
            root.join("RustDesk_local.toml"),
            "fav = ['123456', '../unsafe']\n[options]\naccess_token = 'not-imported'\n",
        )
        .unwrap();
        fs::write(root.join("peers/123456.toml"), "password = [1,2,3]\nview_style = 'adaptive'\n[info]\nusername = 'User'\nhostname = 'Workstation'\nplatform = 'Windows'\n[options]\nalias = 'Desk'\nkey = 'not-imported'\nrdp_password = 'not-imported'\n").unwrap();
        fs::write(root.join("peers/bad.toml"), "invalid [").unwrap();
        let data = read(&root, KEY).unwrap();
        assert_eq!(data.devices.len(), 1);
        assert_eq!(data.devices[0].id, "123456");
        assert_eq!(data.devices[0].password, [1, 2, 3]);
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
        let error = read(&root, KEY).err().unwrap().to_string();
        assert!(!error.contains("sensitive_unterminated"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn server_keys_filter_devices_and_favorites_with_official_cross_server_precedence() {
        let root =
            std::env::temp_dir().join(format!("geodomdesk-import-key-test-{}", std::process::id()));
        fs::create_dir_all(root.join("peers")).unwrap();
        fs::write(
            root.join("RustDesk2.toml"),
            format!("[options]\nkey = '{KEY}'\n"),
        )
        .unwrap();
        let peers = [
            ("123456", "".to_owned()),
            ("234567@other", format!("other-server-key = '{KEY}'")),
            (
                "345678@wrong",
                "other-server-key = 'AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI='".to_owned(),
            ),
            ("456789@unknown", "".to_owned()),
            ("567890@public", format!("other-server-key = '{KEY}'")),
            ("127.0.0.1", "".to_owned()),
        ];
        for (id, options) in &peers {
            fs::write(
                root.join("peers").join(format!("{id}.toml")),
                format!("[options]\n{options}\n"),
            )
            .unwrap();
        }
        let favorites = peers
            .iter()
            .map(|(id, _)| format!("'{id}'"))
            .collect::<Vec<_>>()
            .join(",");
        fs::write(
            root.join("RustDesk_local.toml"),
            format!("fav = [{favorites}]\n"),
        )
        .unwrap();
        let data = read(&root, KEY).unwrap();
        assert_eq!(data.devices.len(), 2);
        assert_eq!(data.skipped, 4);
        assert_eq!(data.favorites, ["123456", "234567@other"]);
        let expected = vec![1; 32];
        let notes = Notes {
            alias: String::new(),
            other_server_key: KEY.to_owned(),
        };
        assert!(!matches_server(
            "123456@host?key=invalid",
            &notes,
            KEY,
            &expected
        ));
        assert!(matches_server(
            &format!("123456@host?key={KEY}"),
            &Notes::default(),
            "",
            &expected
        ));
        assert!(same_key(KEY.trim_end_matches('='), &expected));
        fs::remove_file(root.join("RustDesk2.toml")).unwrap();
        assert_eq!(read(&root, KEY).unwrap().devices.len(), 1);
        assert!(read(&root, "").is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
