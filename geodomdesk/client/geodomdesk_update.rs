//! The GeodomDesk update channel is separate from the public RustDesk channel.
use hbb_common::{bail, ResultType};

pub(crate) const BASE_URL: &str = "https://www.qcjcpt.com/downloads/geodomdesk";
pub(crate) const LATEST_URL: &str = "https://www.qcjcpt.com/downloads/geodomdesk/latest.json";
pub(crate) const BUILD_NUMBER: u64 = 1;

fn release_parts(id: &str) -> ResultType<([u16; 3], u64)> {
    let (version, revision) = match id.split_once("-r") {
        Some((version, build)) => {
            let number: u64 = build.parse()?;
            if number == 0 || number.to_string() != build {
                bail!("Invalid GeodomDesk build number");
            }
            (version, number)
        }
        None => (id, 0),
    };
    let parts = version.split('.').collect::<Vec<_>>();
    if parts.len() != 3 {
        bail!("Invalid official version");
    }
    let mut numbers = [0; 3];
    for (index, part) in parts.into_iter().enumerate() {
        let number: u16 = part.parse()?;
        if number.to_string() != part {
            bail!("Noncanonical official version");
        }
        numbers[index] = number;
    }
    if numbers[0] == 0 {
        bail!("Invalid official version");
    }
    Ok((numbers, revision))
}

pub(crate) fn display_version(id: &str) -> ResultType<String> {
    let (parts, _) = release_parts(id)?;
    Ok(format!("{}.{}.{}", parts[0], parts[1], parts[2]))
}

pub(crate) fn is_newer_release(id: &str, installed: &str, build: u64) -> ResultType<bool> {
    let remote = release_parts(id)?;
    let local = (release_parts(installed)?.0, build);
    Ok(remote > local)
}

pub(crate) fn release_id(raw: &str) -> ResultType<String> {
    let prefix = format!("{BASE_URL}/releases/");
    let Some(tail) = raw.strip_prefix(&prefix) else {
        bail!("Update URL is outside the GeodomDesk channel");
    };
    let id = tail.strip_suffix('/').unwrap_or(tail);
    release_parts(id)?;
    // A canonical numeric path also excludes URL credentials, ports and traversal.
    if id.starts_with('v') || id.contains('/') || id.contains('%') {
        bail!("Noncanonical GeodomDesk release URL");
    }
    Ok(id.to_owned())
}

pub(crate) fn artifact_file(raw: &str) -> Option<&str> {
    let tail = raw.strip_prefix(BASE_URL)?.strip_prefix("/releases/")?;
    let (id, name) = tail.split_once('/')?;
    release_id(&format!("{BASE_URL}/releases/{id}" )).ok()?;
    let expected_prefix = format!("geodomdesk-{id}-");
    if !name.starts_with(&expected_prefix)
        || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return None;
    }
    Some(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_the_dedicated_channel() {
        assert_eq!(release_id(&format!("{BASE_URL}/releases/1.5.2/")).unwrap(), "1.5.2");
        for tail in ["../1.5.2", "v1.5.2", "1.5.2?x", "1.5.2/#x", "%31.5.2", "1.5.2//"] {
            assert!(release_id(&format!("{BASE_URL}/releases/{tail}")).is_err());
        }
        assert!(release_id("https://github.com/rustdesk/rustdesk/releases/tag/1.5.2").is_err());
    }

    #[test]
    fn rejects_paths_and_foreign_payloads() {
        assert!(artifact_file(&format!("{BASE_URL}/releases/1.5.2/geodomdesk-1.5.2-windows-x64.exe")).is_some());
        for name in ["rustdesk.exe", "geodomdesk-1.5.2-../file.exe", "geodomdesk-1.5.2-x.exe?q", "geodomdesk-1.5.2-x%2eexe"] {
            assert!(artifact_file(&format!("{BASE_URL}/releases/1.5.2/{name}")).is_none());
        }
    }

    #[test]
    fn same_official_version_can_receive_a_new_custom_build() {
        assert!(is_newer_release("1.5.1-r102", "1.5.1", 101).unwrap());
        assert!(!is_newer_release("1.5.1-r101", "1.5.1", 101).unwrap());
        assert!(!is_newer_release("1.5.0-r999", "1.5.1", 101).unwrap());
        assert!(is_newer_release("1.5.2-r1", "1.5.1", 999).unwrap());
        assert_eq!(display_version("1.5.1-r102").unwrap(), "1.5.1");
    }
}
