//! Detect an Ubuntu image copied to different permanent network hardware.
use sodiumoxide::crypto::hash::sha256;
use std::{fs, path::Path};

pub(crate) fn current_binding() -> Vec<String> {
    read_binding(Path::new("/sys/class/net"))
}

fn read_binding(root: &Path) -> Vec<String> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut addresses = Vec::new();
    for entry in entries {
        let Ok(entry) = entry else { return Vec::new() };
        let path = entry.path();
        // Bridges, VPNs and containers are not hardware identity anchors.
        if !path.join("device").exists() {
            continue;
        }
        let Ok(kind) = fs::read_to_string(path.join("addr_assign_type")) else {
            return Vec::new();
        };
        if kind.trim() != "0" {
            continue;
        }
        let Ok(address) = fs::read_to_string(path.join("address")) else {
            return Vec::new();
        };
        let address = address.trim().to_ascii_lowercase();
        let bytes: Option<Vec<u8>> = address
            .split(':')
            .map(|part| {
                if part.len() == 2 {
                    u8::from_str_radix(part, 16).ok()
                } else {
                    None
                }
            })
            .collect();
        let Some(bytes) = bytes else {
            return Vec::new();
        };
        if bytes.len() != 6 || bytes[0] & 1 != 0 || bytes.iter().all(|b| *b == 0) {
            return Vec::new();
        }
        addresses.push(address);
    }
    addresses.sort_unstable();
    addresses.dedup();
    addresses
}

#[derive(Debug, PartialEq)]
pub(crate) enum BindingUpdate {
    Unchanged,
    Bound,
    Cloned,
}

pub(crate) fn refresh(
    id: &mut String,
    saved: &mut Vec<String>,
    current: Vec<String>,
) -> BindingUpdate {
    if current.is_empty() || *saved == current {
        return BindingUpdate::Unchanged;
    }
    let cloned = has_changed(saved, &current);
    if cloned {
        *id = replacement_id(id, &current);
        *saved = current;
        return BindingUpdate::Cloned;
    }
    let before = saved.len();
    for anchor in current {
        if !saved.contains(&anchor) {
            saved.push(anchor);
        }
    }
    if saved.len() == before {
        return BindingUpdate::Unchanged;
    }
    saved.sort_unstable();
    BindingUpdate::Bound
}

fn has_changed(previous: &[String], current: &[String]) -> bool {
    !previous.is_empty() && !current.is_empty() && !previous.iter().any(|id| current.contains(id))
}

fn replacement_id(previous: &str, binding: &[String]) -> String {
    // Equal copied configurations on the same hardware must agree across processes.
    let mut input = b"GeodomDesk clone ID\0".to_vec();
    input.extend_from_slice(previous.as_bytes());
    for anchor in binding {
        input.push(0);
        input.extend_from_slice(anchor.as_bytes());
    }
    let digest = sha256::hash(&input);
    let mut prefix = [0; 8];
    prefix.copy_from_slice(&digest.0[..8]);
    // The same numeric range as the official duplicate-ID recovery.
    let range = 1_000_000_000;
    let number = u64::from_be_bytes(prefix) % range;
    let id = (range + number).to_string();
    if id == previous {
        (range + (number + 1) % range).to_string()
    } else {
        id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn preserves_first_run_unknown_hardware_and_existing_devices() {
        assert!(!has_changed(&[], &ids(&["a"])));
        assert!(!has_changed(&ids(&["a"]), &[]));
        assert!(!has_changed(&ids(&["a"]), &ids(&["a"])));
        assert!(!has_changed(&ids(&["a"]), &ids(&["a", "b"])));
        assert!(!has_changed(&ids(&["a", "b"]), &ids(&["b"])));
        assert!(has_changed(&ids(&["a"]), &ids(&["b"])));
    }

    #[test]
    fn replacement_is_compatible_and_different_from_the_copied_id() {
        for previous in ["custom-device", "123456789", "1500000000"] {
            let id = replacement_id(previous, &ids(&["a"]));
            assert_ne!(id, previous);
            assert!((1_000_000_000..2_000_000_000).contains(&id.parse::<u32>().unwrap()));
            assert_eq!(id, replacement_id(previous, &ids(&["a"])));
            assert_ne!(id, replacement_id(previous, &ids(&["b"])));
        }
    }

    #[test]
    fn manual_id_survives_normal_restarts_and_clone_is_reassigned_only_once() {
        let mut id = "custom-device".to_owned();
        let mut binding = Vec::new();
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["a"])),
            BindingUpdate::Bound
        );
        assert_eq!(id, "custom-device");
        id = "manually-changed".to_owned();
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["a"])),
            BindingUpdate::Unchanged
        );
        assert_eq!(id, "manually-changed");
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["a", "b"])),
            BindingUpdate::Bound
        );
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["b"])),
            BindingUpdate::Unchanged
        );
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["a"])),
            BindingUpdate::Unchanged
        );
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["c"])),
            BindingUpdate::Cloned
        );
        let replacement = id.clone();
        assert_ne!(replacement, "manually-changed");
        assert_eq!(
            refresh(&mut id, &mut binding, ids(&["c"])),
            BindingUpdate::Unchanged
        );
        assert_eq!(id, replacement);
    }

    #[test]
    fn ignores_virtual_randomized_and_unreadable_interfaces() {
        let root =
            std::env::temp_dir().join(format!("geodomdesk-clone-test-{}", std::process::id()));
        for (name, address, kind, physical) in [
            ("eth0", "02:00:00:00:00:01", "0", true),
            ("eth1", "02:00:00:00:00:01", "0", true),
            ("veth0", "02:00:00:00:00:02", "0", false),
            ("wlan0", "02:00:00:00:00:03", "1", true),
        ] {
            let path = root.join(name);
            fs::create_dir_all(&path).unwrap();
            if physical {
                fs::create_dir(path.join("device")).unwrap();
            }
            fs::write(path.join("address"), address).unwrap();
            fs::write(path.join("addr_assign_type"), kind).unwrap();
        }
        assert_eq!(read_binding(&root), ids(&["02:00:00:00:00:01"]));
        fs::remove_file(root.join("eth1/address")).unwrap();
        assert!(read_binding(&root).is_empty());
        fs::remove_dir_all(root).unwrap();
    }
}
