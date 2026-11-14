//! Account-scoped recent device metadata and portable connection preferences.
use hbb_common::config::{Ab, AbPeer, LocalConfig, PeerConfig};
use hbb_common::sodiumoxide::base64::{decode, Variant};
use serde_derive::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

pub(crate) const FIELD: &str = "geodom_recent";
const ACCOUNT_OPTION: &str = "geodom-recent-sync-account";
const REMOVALS_OPTION: &str = "geodom-recent-sync-removals";
const MAX_PROFILE_BYTES: usize = 2048;

#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
struct Profile {
    timestamp: u64,
    removed: bool,
    view_style: String,
    scroll_style: String,
    image_quality: String,
    keyboard_mode: String,
    force_relay: bool,
}

fn millis(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(u64::MAX as u128) as u64)
        .unwrap_or_default()
}

fn device_id(id: &str) -> bool {
    (6..=64).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn preference(value: &str) -> &str {
    if value.len() <= 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        value
    } else {
        ""
    }
}

fn parse(value: &str) -> Option<Profile> {
    if value.len() > MAX_PROFILE_BYTES {
        return None;
    }
    let profile: Profile = serde_json::from_str(value).ok()?;
    (profile.timestamp > 0
        && profile.timestamp <= millis(SystemTime::now()).saturating_add(300_000))
    .then_some(profile)
}

pub(crate) fn record(id: &str, modified: SystemTime, peer: &PeerConfig) -> Option<String> {
    if !device_id(id) || peer.info.platform.is_empty() {
        return None;
    }
    let profile = Profile {
        timestamp: millis(modified),
        view_style: preference(&peer.view_style).to_owned(),
        scroll_style: preference(&peer.scroll_style).to_owned(),
        image_quality: preference(&peer.image_quality).to_owned(),
        keyboard_mode: preference(&peer.keyboard_mode).to_owned(),
        force_relay: peer
            .options
            .get("force-always-relay")
            .is_some_and(|value| value == "Y"),
        ..Profile::default()
    };
    let encoded = serde_json::to_string(&profile).ok()?;
    parse(&encoded).map(|_| encoded)
}

pub(crate) fn account_peers() -> Vec<AbPeer> {
    let token = LocalConfig::get_option("access_token");
    let owner = LocalConfig::get_option(ACCOUNT_OPTION);
    let user: serde_json::Value =
        serde_json::from_str(&LocalConfig::get_option("user_info")).unwrap_or_default();
    if token.is_empty()
        || owner.is_empty()
        || user.get("name").and_then(serde_json::Value::as_str) != Some(owner.as_str())
    {
        return Vec::new();
    }
    let account = Ab::load();
    if account.access_token != token {
        return Vec::new();
    }
    account
        .ab_entries
        .into_iter()
        .find(|entry| entry.personal())
        .map(|entry| {
            entry
                .peers
                .into_iter()
                .filter(|peer| device_id(&peer.id) && !peer.geodom_recent.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

pub(crate) fn merge_list(
    local: Vec<HashMap<&'static str, String>>,
    paths: &[(String, SystemTime, PathBuf)],
    cloud: Vec<AbPeer>,
) -> Vec<HashMap<&'static str, String>> {
    if cloud.is_empty() {
        return local;
    }
    let pending: HashMap<String, u64> =
        serde_json::from_str(&LocalConfig::get_option(REMOVALS_OPTION)).unwrap_or_default();
    let times: HashMap<_, _> = paths
        .iter()
        .map(|(id, time, _)| (id.as_str(), millis(*time)))
        .collect();
    let mut peers: HashMap<_, _> = local
        .into_iter()
        .filter_map(|peer| {
            let id = peer.get("id")?.clone();
            Some((
                id.clone(),
                (times.get(id.as_str()).copied().unwrap_or_default(), peer),
            ))
        })
        .collect();
    for peer in cloud {
        let Some(profile) = parse(&peer.geodom_recent) else {
            continue;
        };
        let local_timestamp = times.get(peer.id.as_str()).copied().unwrap_or_default();
        if profile.timestamp <= local_timestamp {
            continue;
        }
        if profile.removed
            || pending
                .get(&peer.id)
                .is_some_and(|timestamp| *timestamp >= profile.timestamp)
        {
            peers.remove(&peer.id);
        } else {
            peers.insert(
                peer.id.clone(),
                (
                    profile.timestamp,
                    HashMap::from([
                        ("id", peer.id),
                        ("username", peer.username),
                        ("hostname", peer.hostname),
                        ("platform", peer.platform),
                        ("alias", peer.alias),
                    ]),
                ),
            );
        }
    }
    let mut peers: Vec<_> = peers.into_iter().collect();
    peers.sort_by(|(a, (at, _)), (b, (bt, _))| bt.cmp(at).then_with(|| a.cmp(b)));
    peers.into_iter().map(|(_, (_, peer))| peer).collect()
}

pub(crate) fn apply_settings(id: &str, local: &mut PeerConfig) {
    if !device_id(id) {
        return;
    }
    let Some(peer) = account_peers().into_iter().find(|peer| peer.id == id) else {
        return;
    };
    apply_peer(peer, millis(PeerConfig::last_modified(id)), local);
}

fn apply_peer(peer: AbPeer, local_timestamp: u64, local: &mut PeerConfig) {
    let Some(profile) = parse(&peer.geodom_recent) else {
        return;
    };
    if profile.timestamp <= local_timestamp {
        return;
    }
    if profile.removed {
        if peer.geodom_recent_hash_present && peer.hash.is_empty() {
            local.password.clear();
        }
        return;
    }
    if peer.geodom_recent_hash_present && peer.hash.is_empty() {
        local.password.clear();
    } else if let Ok(password) = decode(&peer.hash, Variant::Original) {
        if password.len() == 32 {
            local.password = password;
        }
    }
    for (setting, value) in [
        (&mut local.view_style, profile.view_style),
        (&mut local.scroll_style, profile.scroll_style),
        (&mut local.image_quality, profile.image_quality),
        (&mut local.keyboard_mode, profile.keyboard_mode),
    ] {
        let value = preference(&value);
        if !value.is_empty() {
            *setting = value.to_owned();
        }
    }
    local.options.insert("alias".to_owned(), peer.alias);
    local.options.insert(
        "force-always-relay".to_owned(),
        if profile.force_relay { "Y" } else { "N" }.to_owned(),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn cloud_records_fill_missing_devices_and_newer_removals_win() {
        let local = HashMap::from([
            ("id", "834502176".to_owned()),
            ("alias", "Local".to_owned()),
        ]);
        let paths = vec![(
            "834502176".to_owned(),
            UNIX_EPOCH + Duration::from_millis(50),
            PathBuf::new(),
        )];
        let remote = AbPeer {
            id: "834502177".to_owned(),
            alias: "Remote".to_owned(),
            geodom_recent: r#"{"timestamp":60}"#.to_owned(),
            ..AbPeer::default()
        };
        let removal = AbPeer {
            id: "834502176".to_owned(),
            geodom_recent: r#"{"timestamp":70,"removed":true}"#.to_owned(),
            ..AbPeer::default()
        };
        let merged = merge_list(vec![local], &paths, vec![remote, removal]);
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0]["alias"], "Remote");
        assert!(!merged[0].contains_key("hash"));
    }

    #[test]
    fn exported_preferences_are_scoped_to_valid_device_ids() {
        let mut peer = PeerConfig::default();
        peer.info.platform = "Windows".to_owned();
        peer.options
            .insert("force-always-relay".to_owned(), "Y".to_owned());
        let time = UNIX_EPOCH + Duration::from_millis(123);
        let profile = record("834502176", time, &peer)
            .and_then(|value| parse(&value))
            .unwrap();
        assert!(profile.force_relay);
        assert_eq!(profile.timestamp, 123);
        assert!(record("834502176@another.example", time, &peer).is_none());
    }

    #[test]
    fn newer_account_profile_replaces_or_clears_an_older_local_credential() {
        let mut local = PeerConfig {
            password: vec![1; 32],
            ..PeerConfig::default()
        };
        let peer = AbPeer {
            geodom_recent: r#"{"timestamp":200}"#.to_owned(),
            hash: hbb_common::sodiumoxide::base64::encode([2; 32], Variant::Original),
            ..AbPeer::default()
        };
        apply_peer(peer.clone(), 200, &mut local);
        assert_eq!(local.password, vec![1; 32]);
        apply_peer(peer.clone(), 199, &mut local);
        assert_eq!(local.password, vec![2; 32]);
        let cleared = AbPeer {
            hash: String::new(),
            ..peer.clone()
        };
        apply_peer(cleared.clone(), 199, &mut local);
        assert_eq!(local.password, vec![2; 32]);
        apply_peer(
            AbPeer {
                geodom_recent_hash_present: true,
                ..cleared
            },
            199,
            &mut local,
        );
        assert!(local.password.is_empty());
        local.password = vec![1; 32];
        apply_peer(
            AbPeer {
                geodom_recent: r#"{"timestamp":201,"removed":true}"#.to_owned(),
                geodom_recent_hash_present: true,
                hash: String::new(),
                ..peer
            },
            199,
            &mut local,
        );
        assert!(local.password.is_empty());
    }
}
