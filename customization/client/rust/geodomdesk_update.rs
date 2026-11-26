//! The GeodomDesk update channel is separate from the public RustDesk channel.
use hbb_common::{bail, ResultType};

pub(crate) const BASE_URL: &str = "https://www.qcjcpt.com/downloads/geodomdesk";
pub(crate) const LATEST_URL: &str = "https://www.qcjcpt.com/downloads/geodomdesk/latest.json";
pub(crate) const BUILD_NUMBER: u64 = 1;
pub(crate) const MAX_POINTER_BYTES: usize = 65_536;
pub(crate) const MAX_CHANGELOG_BYTES: usize = 8_192;
pub(crate) const MAX_CHANGELOG_LINES: usize = 120;

#[derive(
    Debug, Clone, Default, PartialEq, Eq, serde_derive::Serialize, serde_derive::Deserialize,
)]
pub(crate) struct SoftwareUpdateInfo {
    pub url: String,
    pub release_id: String,
    pub version: String,
    pub changelog: String,
    pub published_at: String,
}

pub(crate) fn format_release_id(version: &str, build: u64) -> String {
    let trimmed = version.trim();
    if build > 0 && !trimmed.is_empty() {
        format!("{trimmed}-r{build}")
    } else {
        trimmed.to_owned()
    }
}

pub(crate) fn current_release_id(version: &str) -> String {
    format_release_id(version, BUILD_NUMBER)
}

pub(crate) fn sanitize_changelog(raw: &str) -> String {
    let normalized = raw.replace("\r\n", "\n").replace('\r', "\n");
    let mut cleaned_lines = Vec::new();
    let mut blank_run = 0usize;
    for line in normalized.split('\n') {
        let filtered: String = line
            .chars()
            .filter(|c| !c.is_control() || *c == '\t')
            .collect();
        let trimmed = filtered.trim_end_matches(|c| c == ' ' || c == '\t');
        if trimmed.trim().is_empty() {
            if !cleaned_lines.is_empty() && blank_run == 0 {
                cleaned_lines.push(String::new());
            }
            blank_run += 1;
            continue;
        }
        blank_run = 0;
        cleaned_lines.push(trimmed.to_owned());
        if cleaned_lines.len() >= MAX_CHANGELOG_LINES {
            break;
        }
    }
    while cleaned_lines.last().map_or(false, |line| line.is_empty()) {
        cleaned_lines.pop();
    }
    if cleaned_lines.is_empty() {
        return String::new();
    }
    let joined = cleaned_lines.join("\n");
    if joined.len() <= MAX_CHANGELOG_BYTES {
        return joined;
    }
    let suffix = "...";
    let limit = MAX_CHANGELOG_BYTES.saturating_sub(suffix.len());
    let mut cut = 0;
    for (idx, ch) in joined.char_indices() {
        if idx + ch.len_utf8() > limit {
            break;
        }
        cut = idx + ch.len_utf8();
    }
    let mut out = joined[..cut].trim_end().to_owned();
    out.push_str(suffix);
    out
}

pub(crate) fn sanitize_published_at(raw: &str) -> String {
    raw.trim()
        .chars()
        .filter(|c| c.is_ascii() && !c.is_ascii_control())
        .take(64)
        .collect()
}

pub(crate) fn parse_pointer_info(
    bytes: &[u8],
    response_url: &str,
    release_id: &str,
) -> SoftwareUpdateInfo {
    let pointer = serde_json::from_slice::<serde_json::Value>(bytes).ok();
    let changelog = pointer
        .as_ref()
        .and_then(|value| value.get("changelog"))
        .and_then(|value| value.as_str())
        .map(sanitize_changelog)
        .unwrap_or_default();
    let published_at = pointer
        .as_ref()
        .and_then(|value| value.get("published_at"))
        .and_then(|value| value.as_str())
        .map(sanitize_published_at)
        .unwrap_or_default();
    let version = display_version(release_id).unwrap_or_else(|_| release_id.to_owned());
    SoftwareUpdateInfo {
        url: response_url.to_owned(),
        release_id: release_id.to_owned(),
        version,
        changelog,
        published_at,
    }
}

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
    release_id(&format!("{BASE_URL}/releases/{id}")).ok()?;
    let expected_prefix = format!("geodomdesk-{id}-");
    if !name.starts_with(&expected_prefix)
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
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
        assert_eq!(
            release_id(&format!("{BASE_URL}/releases/1.5.2/")).unwrap(),
            "1.5.2"
        );
        for tail in [
            "../1.5.2", "v1.5.2", "1.5.2?x", "1.5.2/#x", "%31.5.2", "1.5.2//",
        ] {
            assert!(release_id(&format!("{BASE_URL}/releases/{tail}")).is_err());
        }
        assert!(release_id("https://github.com/rustdesk/rustdesk/releases/tag/1.5.2").is_err());
    }

    #[test]
    fn rejects_paths_and_foreign_payloads() {
        assert!(artifact_file(&format!(
            "{BASE_URL}/releases/1.5.2/geodomdesk-1.5.2-windows-x64.exe"
        ))
        .is_some());
        for name in [
            "rustdesk.exe",
            "geodomdesk-1.5.2-../file.exe",
            "geodomdesk-1.5.2-x.exe?q",
            "geodomdesk-1.5.2-x%2eexe",
        ] {
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
        assert_eq!(format_release_id("1.5.3", 102), "1.5.3-r102");
        assert_eq!(format_release_id("1.5.3", 0), "1.5.3");
        assert_eq!(current_release_id("1.5.3"), format_release_id("1.5.3", BUILD_NUMBER));
    }

    #[test]
    fn sanitizes_and_bounds_changelog() {
        let dirty = "\n\r\n## 更新内容\r\n- 修复问题\x00\x1b[31m   \n\n\n\n- 性能优化\t\n\n";
        assert_eq!(sanitize_changelog(dirty), "## 更新内容\n- 修复问题[31m\n\n- 性能优化");
        assert_eq!(sanitize_changelog("   \r\n\t\x07 "), "");

        let long_cn = "更新项测试".repeat(1000);
        let bounded = sanitize_changelog(&long_cn);
        assert!(bounded.len() <= MAX_CHANGELOG_BYTES);
        assert!(bounded.ends_with("..."));

        let many_lines = (1..=200)
            .map(|idx| format!("- 第{idx}条"))
            .collect::<Vec<_>>()
            .join("\n");
        let capped = sanitize_changelog(&many_lines);
        assert_eq!(capped.lines().count(), MAX_CHANGELOG_LINES);
    }

    #[test]
    fn parses_pointer_info_with_and_without_changelog() {
        let with_changelog = serde_json::json!({
            "url": format!("{BASE_URL}/releases/1.5.3-r12/"),
            "changelog": "- 修复连接问题\r\n- 提升传输速度",
            "published_at": "2026-11-26T08:00:00Z",
        });
        let info = parse_pointer_info(
            with_changelog.to_string().as_bytes(),
            &format!("{BASE_URL}/releases/1.5.3-r12/"),
            "1.5.3-r12",
        );
        assert_eq!(info.version, "1.5.3");
        assert_eq!(info.release_id, "1.5.3-r12");
        assert_eq!(info.changelog, "- 修复连接问题\n- 提升传输速度");
        assert_eq!(info.published_at, "2026-11-26T08:00:00Z");

        let legacy = br#"{"url":"https://www.qcjcpt.com/downloads/geodomdesk/releases/1.5.2/"}"#;
        let legacy_info = parse_pointer_info(
            legacy,
            "https://www.qcjcpt.com/downloads/geodomdesk/releases/1.5.2/",
            "1.5.2",
        );
        assert_eq!(legacy_info.version, "1.5.2");
        assert_eq!(legacy_info.changelog, "");
        assert_eq!(legacy_info.published_at, "");
    }
}
