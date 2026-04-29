//! Bitfocus builds API client.
//!
//! Endpoint: `https://api.bitfocus.io/v1/product/companion/packages?branch=stable`
//! Response: `{"packages": [{"version": "v4.3.1", "target": "linux-tgz", ...}, ...]}`
//!
//! We pick the highest version among packages whose target starts with `linux-`.

use serde::Deserialize;

const API_URL: &str = "https://api.bitfocus.io/v1/product/companion/packages?branch=stable";

#[derive(Debug, Deserialize)]
pub struct Package {
    pub version: String,
    pub target: String,
}

#[derive(Debug, Deserialize)]
struct PackagesResponse {
    packages: Vec<Package>,
}

/// Fetch the latest stable Linux version from Bitfocus.
/// Returns the version string (e.g., `"v4.3.1"`) on success.
pub async fn fetch_latest_stable_linux(client: &reqwest::Client) -> Result<String, String> {
    let resp = client
        .get(API_URL)
        .send()
        .await
        .map_err(|e| format!("HTTP request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("Bitfocus API returned {}", resp.status()));
    }

    let body: PackagesResponse = resp
        .json()
        .await
        .map_err(|e| format!("Failed to parse JSON: {e}"))?;

    pick_latest_linux(&body.packages).ok_or_else(|| "No Linux packages in response".to_string())
}

fn pick_latest_linux(packages: &[Package]) -> Option<String> {
    packages
        .iter()
        .filter(|p| p.target.starts_with("linux-"))
        .max_by(|a, b| crate::version::compare(&a.version, &b.version))
        .map(|p| p.version.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkg(version: &str, target: &str) -> Package {
        Package {
            version: version.to_string(),
            target: target.to_string(),
        }
    }

    #[test]
    fn picks_highest_linux_version() {
        let packages = vec![
            pkg("v4.2.6", "linux-tgz"),
            pkg("v4.3.1", "linux-tgz"),
            pkg("v4.3.1", "win-x64"),
            pkg("v4.2.5", "linux-arm64-tgz"),
        ];
        assert_eq!(pick_latest_linux(&packages), Some("v4.3.1".to_string()));
    }

    #[test]
    fn ignores_non_linux_targets() {
        let packages = vec![
            pkg("v5.0.0", "win-x64"),
            pkg("v5.0.0", "mac-arm"),
            pkg("v4.2.6", "linux-tgz"),
        ];
        assert_eq!(pick_latest_linux(&packages), Some("v4.2.6".to_string()));
    }

    #[test]
    fn returns_none_when_no_linux() {
        let packages = vec![pkg("v5.0.0", "win-x64")];
        assert_eq!(pick_latest_linux(&packages), None);
    }

    #[test]
    fn parses_real_response_shape() {
        let json = r#"{
            "packages": [
                {"version": "v4.3.1", "target": "linux-tgz", "uri": "...", "published": "..."},
                {"version": "v4.2.6", "target": "linux-arm64-tgz", "uri": "...", "published": "..."}
            ]
        }"#;
        let parsed: PackagesResponse = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.packages.len(), 2);
        assert_eq!(parsed.packages[0].version, "v4.3.1");
    }
}
