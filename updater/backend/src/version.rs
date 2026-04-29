//! Version string parsing and comparison.
//!
//! Companion versions look like `"4.2.6+8823"` or `"v4.3.1"`. This module
//! strips the `v` prefix and the `+build` suffix, then compares the
//! left-to-right numeric components.

use std::cmp::Ordering;

/// Parse a version string into a vector of numeric components.
/// `"v4.2.6+8823"` → `[4, 2, 6]`.
pub fn parse(s: &str) -> Vec<u32> {
    let trimmed = s.trim().trim_start_matches('v');
    let semver = trimmed.split('+').next().unwrap_or("");
    semver
        .split('.')
        .filter_map(|p| p.parse::<u32>().ok())
        .collect()
}

/// Compare two version strings numerically.
pub fn compare(a: &str, b: &str) -> Ordering {
    let pa = parse(a);
    let pb = parse(b);
    let max = pa.len().max(pb.len());
    for i in 0..max {
        let ai = pa.get(i).copied().unwrap_or(0);
        let bi = pb.get(i).copied().unwrap_or(0);
        match ai.cmp(&bi) {
            Ordering::Equal => continue,
            other => return other,
        }
    }
    Ordering::Equal
}

/// Returns true if `latest` is strictly greater than `current`.
pub fn is_update_available(current: &str, latest: &str) -> bool {
    compare(current, latest) == Ordering::Less
}

/// Format a version string for display: ensure a single `v` prefix,
/// drop any `+build` suffix.
pub fn format(s: &str) -> String {
    let trimmed = s.trim().trim_start_matches('v');
    let semver = trimmed.split('+').next().unwrap_or(trimmed);
    format!("v{}", semver)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_strips_v_prefix() {
        assert_eq!(parse("v4.2.6"), vec![4, 2, 6]);
    }

    #[test]
    fn parse_strips_build_suffix() {
        assert_eq!(parse("4.2.6+8823"), vec![4, 2, 6]);
    }

    #[test]
    fn parse_handles_both() {
        assert_eq!(parse("v4.3.1+9209-stable"), vec![4, 3, 1]);
    }

    #[test]
    fn compare_equal() {
        assert_eq!(compare("4.2.6", "v4.2.6+8823"), Ordering::Equal);
    }

    #[test]
    fn compare_patch_difference() {
        assert_eq!(compare("4.2.6", "4.2.8"), Ordering::Less);
        assert_eq!(compare("4.2.8", "4.2.6"), Ordering::Greater);
    }

    #[test]
    fn compare_minor_difference() {
        assert_eq!(compare("4.2.10", "4.3.0"), Ordering::Less);
    }

    #[test]
    fn compare_different_lengths() {
        assert_eq!(compare("4.2", "4.2.0"), Ordering::Equal);
        assert_eq!(compare("4.2", "4.2.1"), Ordering::Less);
    }

    #[test]
    fn update_available_true_when_remote_newer() {
        assert!(is_update_available("4.2.6", "4.3.1"));
    }

    #[test]
    fn update_available_false_when_equal() {
        assert!(!is_update_available("4.2.6", "v4.2.6+8823"));
    }

    #[test]
    fn update_available_false_when_local_newer() {
        assert!(!is_update_available("4.3.1", "4.2.6"));
    }

    #[test]
    fn format_adds_v_prefix() {
        assert_eq!(format("4.2.6"), "v4.2.6");
    }

    #[test]
    fn format_drops_build() {
        assert_eq!(format("4.2.6+8823"), "v4.2.6");
    }

    #[test]
    fn format_idempotent() {
        assert_eq!(format("v4.2.6"), "v4.2.6");
    }
}
