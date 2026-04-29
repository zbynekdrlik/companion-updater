//! Read Companion's installed version and systemd service status.

use serde::Deserialize;
use tokio::process::Command;

const PACKAGE_JSON_PATH: &str = "/opt/companion/package.json";

#[derive(Debug, Deserialize)]
struct PackageJson {
    version: String,
}

/// Read the installed Companion version from `/opt/companion/package.json`.
pub async fn read_installed_version() -> Result<String, String> {
    let content = tokio::fs::read_to_string(PACKAGE_JSON_PATH)
        .await
        .map_err(|e| format!("Failed to read {PACKAGE_JSON_PATH}: {e}"))?;
    parse_version_from_json(&content)
}

fn parse_version_from_json(content: &str) -> Result<String, String> {
    let parsed: PackageJson = serde_json::from_str(content)
        .map_err(|e| format!("Failed to parse package.json: {e}"))?;
    Ok(parsed.version)
}

/// Whether `systemctl is-active companion` reports the service as active.
pub async fn service_is_active() -> bool {
    Command::new("systemctl")
        .args(["is-active", "companion"])
        .output()
        .await
        .map(|out| out.status.success())
        .unwrap_or(false)
}

/// Whether `systemctl is-enabled companion` reports the service as enabled.
pub async fn service_is_enabled() -> bool {
    Command::new("systemctl")
        .args(["is-enabled", "companion"])
        .output()
        .await
        .map(|out| out.status.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_package_json() {
        let json = r#"{"name":"companion","version":"4.2.6+8823","license":"MIT"}"#;
        assert_eq!(parse_version_from_json(json).unwrap(), "4.2.6+8823");
    }

    #[test]
    fn parses_v_prefixed_version() {
        let json = r#"{"name":"companion","version":"v4.3.1"}"#;
        assert_eq!(parse_version_from_json(json).unwrap(), "v4.3.1");
    }

    #[test]
    fn fails_on_missing_version_field() {
        let json = r#"{"name":"companion"}"#;
        assert!(parse_version_from_json(json).is_err());
    }

    #[test]
    fn fails_on_invalid_json() {
        let json = "not json";
        assert!(parse_version_from_json(json).is_err());
    }
}
