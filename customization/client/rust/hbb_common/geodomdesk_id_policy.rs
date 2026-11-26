//! Preserve the automatically assigned ID independently of later custom IDs.
pub(crate) fn is_numeric_id(id: &str) -> bool {
    (6..=10).contains(&id.len()) && id.bytes().all(|byte| byte.is_ascii_digit())
}

pub(crate) fn default_id(
    current: &str,
    saved: &str,
    generate: impl FnOnce() -> Option<String>,
) -> String {
    if is_numeric_id(saved) {
        saved.to_owned()
    } else if is_numeric_id(current) {
        current.to_owned()
    } else {
        generate()
            .filter(|id| is_numeric_id(id))
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retains_the_original_id_after_customization() {
        assert_eq!(default_id("CustomID", "123456789", || None), "123456789");
        assert_eq!(default_id("123456789", "", || None), "123456789");
    }

    #[test]
    fn upgrades_custom_ids_without_using_an_invalid_default() {
        assert_eq!(
            default_id("CustomID", "", || Some("123456789".into())),
            "123456789"
        );
        assert_eq!(
            default_id("CustomID", "invalid", || Some("12345".into())),
            ""
        );
        for value in ["", "12345", "12345678901", "12345x", "１２３４５６"] {
            assert!(!is_numeric_id(value));
        }
    }
}
