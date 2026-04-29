# Companion Updater Rust+WASM Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the companion-updater dashboard as a native Rust binary (axum backend + Leptos/WASM frontend) that manages the companion-pi installation.

**Architecture:** Cargo workspace with two crates: `backend` (axum HTTP/SSE, runs as root systemd service) and `frontend` (Leptos CSR, compiled to WASM by trunk). Backend embeds the WASM bundle via `include_dir!` and serves it. Backend reads `/opt/companion/package.json` for the current version, fetches the latest from Bitfocus's builds API, and triggers updates via `update.sh stable`.

**Tech Stack:** Rust 1.90+, axum 0.7, tokio, reqwest, serde, leptos (CSR), trunk, wasm-bindgen, web-sys (EventSource), include_dir

**Spec:** `docs/superpowers/specs/2026-04-21-companion-updater-rust-wasm-design.md`

**Target host:** companion.lan (Ubuntu 24.04 x86_64). Build locally on the same arch — no cross-compilation needed.

**SSH access:** `sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan "<command>"`

---

### Task 1: Remove old Python/Docker updater

**Why:** Clean slate before adding the Rust workspace. Stop the running Docker container so port 8081 is free.

**Files:**
- Delete: `updater/Dockerfile`
- Delete: `updater/docker-compose.yml`
- Delete: `updater/requirements.txt`
- Delete: `updater/app/` (directory)

- [ ] **Step 1: Stop the Docker updater on companion.lan**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "cd /opt/companion-updater && docker compose down 2>&1; docker rm -f companion-updater 2>&1 || true"
```

Expected: Container stopped and removed. No error if it was already gone.

- [ ] **Step 2: Remove old Python files from the repo**

```bash
cd /home/newlevel/devel/companion
rm -rf updater/Dockerfile updater/docker-compose.yml updater/requirements.txt updater/app
ls updater/
```

Expected: `updater/` directory is empty.

- [ ] **Step 3: Commit removal**

```bash
git add -A updater/
git commit -m "Remove Python/Docker updater (replaced by Rust+WASM)"
```

---

### Task 2: Create Cargo workspace + skeleton crates

**Why:** Establish the directory layout with empty crates that compile, before adding logic.

**Files:**
- Create: `updater/Cargo.toml` (workspace root)
- Create: `updater/backend/Cargo.toml`
- Create: `updater/backend/src/main.rs`
- Create: `updater/frontend/Cargo.toml`
- Create: `updater/frontend/src/main.rs`
- Create: `updater/frontend/index.html`
- Create: `updater/.gitignore`

- [ ] **Step 1: Create workspace Cargo.toml**

`updater/Cargo.toml`:

```toml
[workspace]
members = ["backend", "frontend"]
resolver = "2"

[workspace.package]
version = "0.1.0"
edition = "2021"
license = "MIT"
```

- [ ] **Step 2: Create backend Cargo.toml**

`updater/backend/Cargo.toml`:

```toml
[package]
name = "companion-updater"
version.workspace = true
edition.workspace = true
license.workspace = true

[dependencies]
axum = "0.7"
tokio = { version = "1", features = ["full"] }
tower-http = { version = "0.5", features = ["trace"] }
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }
include_dir = "0.7"
mime_guess = "2"
chrono = "0.4"
futures = "0.3"

[dev-dependencies]
tokio = { version = "1", features = ["full", "test-util"] }
```

- [ ] **Step 3: Create backend skeleton**

`updater/backend/src/main.rs`:

```rust
use axum::{routing::get, Router};
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let app = Router::new().route("/healthz", get(|| async { "ok" }));

    let addr: SocketAddr = "0.0.0.0:8081".parse().unwrap();
    tracing::info!("listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

- [ ] **Step 4: Create frontend Cargo.toml**

`updater/frontend/Cargo.toml`:

```toml
[package]
name = "companion-updater-frontend"
version.workspace = true
edition.workspace = true
license.workspace = true

[dependencies]
leptos = { version = "0.7", features = ["csr"] }
wasm-bindgen = "0.2"
wasm-bindgen-futures = "0.4"
js-sys = "0.3"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
gloo-net = { version = "0.6", features = ["http"] }
console_error_panic_hook = "0.1"

[dependencies.web-sys]
version = "0.3"
features = [
    "EventSource",
    "MessageEvent",
    "Event",
    "Window",
]
```

- [ ] **Step 5: Create frontend skeleton**

`updater/frontend/src/main.rs`:

```rust
fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(|| leptos::view! { <h1>"Companion Update Dashboard"</h1> });
}
```

`updater/frontend/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Companion Update Dashboard</title>
    <link data-trunk rel="rust" data-bin="companion-updater-frontend" data-wasm-opt="z" />
</head>
<body></body>
</html>
```

- [ ] **Step 6: Create .gitignore**

`updater/.gitignore`:

```
target/
frontend/dist/
**/*.rs.bk
Cargo.lock
```

- [ ] **Step 7: Verify workspace builds**

```bash
cd /home/newlevel/devel/companion/updater
cargo build --workspace --exclude companion-updater-frontend
```

Expected: Backend compiles successfully (frontend is excluded because it needs the wasm32 target).

- [ ] **Step 8: Verify frontend builds with trunk**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build
ls dist/
```

Expected: `dist/` contains `index.html`, a `.wasm` file, and a `.js` file.

- [ ] **Step 9: Commit skeleton**

```bash
cd /home/newlevel/devel/companion
git add updater/
git commit -m "Add Cargo workspace skeleton for Rust updater"
```

---

### Task 3: Implement version parsing and comparison

**Why:** Pure logic — easiest to TDD. Foundation for the status endpoint.

**Files:**
- Create: `updater/backend/src/version.rs`
- Modify: `updater/backend/src/main.rs` (add `mod version;`)

- [ ] **Step 1: Write failing tests**

`updater/backend/src/version.rs`:

```rust
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
```

- [ ] **Step 2: Add module declaration**

In `updater/backend/src/main.rs`, add at the top:

```rust
mod version;
```

- [ ] **Step 3: Run tests, expect them to pass**

```bash
cd /home/newlevel/devel/companion/updater
cargo test -p companion-updater version
```

Expected: All 12 tests pass.

- [ ] **Step 4: Commit**

```bash
git add updater/backend/src/version.rs updater/backend/src/main.rs
git commit -m "Add version parsing and comparison module"
```

---

### Task 4: Implement Bitfocus builds API client

**Why:** Backend needs to fetch the latest stable version. Pure parsing logic is testable; HTTP call is verified at runtime.

**Files:**
- Create: `updater/backend/src/bitfocus.rs`
- Modify: `updater/backend/src/main.rs` (add `mod bitfocus;`)

- [ ] **Step 1: Write the module with tests**

`updater/backend/src/bitfocus.rs`:

```rust
//! Bitfocus builds API client.
//!
//! Endpoint: `https://api.bitfocus.io/v1/product/companion/packages?branch=stable`
//! Response: `{"packages": [{"version": "v4.3.1", "target": "linux-tgz", ...}, ...]}`
//!
//! We pick the highest version among packages whose target starts with `linux-`.

use serde::Deserialize;

const API_URL: &str =
    "https://api.bitfocus.io/v1/product/companion/packages?branch=stable";

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
pub async fn fetch_latest_stable_linux(
    client: &reqwest::Client,
) -> Result<String, String> {
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

    pick_latest_linux(&body.packages)
        .ok_or_else(|| "No Linux packages in response".to_string())
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
```

- [ ] **Step 2: Add module declaration**

In `updater/backend/src/main.rs`, add at the top:

```rust
mod bitfocus;
```

- [ ] **Step 3: Run tests**

```bash
cd /home/newlevel/devel/companion/updater
cargo test -p companion-updater bitfocus
```

Expected: All 4 tests pass.

- [ ] **Step 4: Commit**

```bash
git add updater/backend/src/bitfocus.rs updater/backend/src/main.rs
git commit -m "Add Bitfocus builds API client"
```

---

### Task 5: Implement companion module (read package.json + systemctl)

**Why:** Read the currently installed Companion version and check the systemd service status.

**Files:**
- Create: `updater/backend/src/companion.rs`
- Modify: `updater/backend/src/main.rs` (add `mod companion;`)

- [ ] **Step 1: Write the module with tests**

`updater/backend/src/companion.rs`:

```rust
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
```

- [ ] **Step 2: Add module declaration**

In `updater/backend/src/main.rs`, add at the top:

```rust
mod companion;
```

- [ ] **Step 3: Run tests**

```bash
cd /home/newlevel/devel/companion/updater
cargo test -p companion-updater companion
```

Expected: All 4 tests pass.

- [ ] **Step 4: Commit**

```bash
git add updater/backend/src/companion.rs updater/backend/src/main.rs
git commit -m "Add companion module: read package.json and systemctl status"
```

---

### Task 6: Implement update module (run update.sh, stream output)

**Why:** The core action — invoke the companion-pi update script and stream its stdout/stderr to subscribers.

**Files:**
- Create: `updater/backend/src/update.rs`
- Modify: `updater/backend/src/main.rs` (add `mod update;`)

- [ ] **Step 1: Write the module**

`updater/backend/src/update.rs`:

```rust
//! Run the companion-pi update script and stream its output.
//!
//! Spawns `sudo bash /usr/local/src/companionpi/update.sh stable` and yields
//! each line of combined stdout/stderr. After the child exits with success,
//! restarts the companion service.

use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;

const UPDATE_SCRIPT: &str = "/usr/local/src/companionpi/update.sh";

#[derive(Clone, Debug, serde::Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum UpdateEvent {
    Progress { message: String },
    Complete { message: String },
    Error { message: String },
}

/// Spawn the update process and stream lines through `tx`.
/// Closes `tx` when finished.
pub async fn run_update(tx: mpsc::Sender<UpdateEvent>) {
    let _ = tx
        .send(UpdateEvent::Progress {
            message: "Starting update (stable channel)...".into(),
        })
        .await;

    let mut child = match Command::new("sudo")
        .args(["bash", UPDATE_SCRIPT, "stable"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("Failed to spawn update.sh: {e}"),
                })
                .await;
            return;
        }
    };

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let tx_out = tx.clone();
    let stdout_handle = tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = tx_out
                .send(UpdateEvent::Progress { message: line })
                .await;
        }
    });

    let tx_err = tx.clone();
    let stderr_handle = tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = tx_err
                .send(UpdateEvent::Progress { message: line })
                .await;
        }
    });

    let status = match child.wait().await {
        Ok(s) => s,
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("update.sh wait failed: {e}"),
                })
                .await;
            return;
        }
    };

    let _ = stdout_handle.await;
    let _ = stderr_handle.await;

    if !status.success() {
        let _ = tx
            .send(UpdateEvent::Error {
                message: format!("update.sh exited with {status}"),
            })
            .await;
        return;
    }

    let _ = tx
        .send(UpdateEvent::Progress {
            message: "Restarting companion service...".into(),
        })
        .await;

    let restart = Command::new("sudo")
        .args(["systemctl", "restart", "companion"])
        .status()
        .await;

    match restart {
        Ok(s) if s.success() => {}
        Ok(s) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("systemctl restart exited with {s}"),
                })
                .await;
            return;
        }
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("systemctl restart failed: {e}"),
                })
                .await;
            return;
        }
    }

    // Allow Companion a moment to come up, then read the new version.
    tokio::time::sleep(Duration::from_secs(2)).await;

    let new_version = crate::companion::read_installed_version()
        .await
        .unwrap_or_else(|_| "unknown".to_string());

    let _ = tx
        .send(UpdateEvent::Complete {
            message: format!(
                "Update complete. Now running {}",
                crate::version::format(&new_version)
            ),
        })
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_progress_serializes() {
        let e = UpdateEvent::Progress {
            message: "hello".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"progress","message":"hello"}"#);
    }

    #[test]
    fn event_complete_serializes() {
        let e = UpdateEvent::Complete {
            message: "done".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"complete","message":"done"}"#);
    }

    #[test]
    fn event_error_serializes() {
        let e = UpdateEvent::Error {
            message: "boom".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"error","message":"boom"}"#);
    }
}
```

- [ ] **Step 2: Add module declaration**

In `updater/backend/src/main.rs`, add at the top:

```rust
mod update;
```

- [ ] **Step 3: Run tests**

```bash
cd /home/newlevel/devel/companion/updater
cargo test -p companion-updater update
```

Expected: 3 tests pass.

- [ ] **Step 4: Commit**

```bash
git add updater/backend/src/update.rs updater/backend/src/main.rs
git commit -m "Add update module: run update.sh and stream output"
```

---

### Task 7: Implement static_files module (embed and serve frontend)

**Why:** The backend needs to serve the WASM bundle compiled by trunk into `frontend/dist/`.

**Files:**
- Create: `updater/backend/src/static_files.rs`
- Modify: `updater/backend/src/main.rs` (add `mod static_files;`)

- [ ] **Step 1: Build the frontend so dist/ exists**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build --release
ls dist/
```

Expected: `dist/index.html`, `dist/*.wasm`, `dist/*.js`.

- [ ] **Step 2: Write static_files module**

`updater/backend/src/static_files.rs`:

```rust
//! Serve the frontend bundle that trunk produces into `frontend/dist/`.
//! The whole directory is embedded at compile time via `include_dir!`.

use axum::{
    body::Body,
    extract::Path,
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use include_dir::{include_dir, Dir};

static DIST: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist");

pub async fn index() -> Response {
    serve("index.html").await
}

pub async fn asset(Path(path): Path<String>) -> Response {
    serve(&path).await
}

async fn serve(path: &str) -> Response {
    let file = match DIST.get_file(path) {
        Some(f) => f,
        None => {
            return (StatusCode::NOT_FOUND, "not found").into_response();
        }
    };
    let mime = mime_guess::from_path(path)
        .first_or_octet_stream()
        .essence_str()
        .to_string();
    let mut resp = Response::new(Body::from(file.contents()));
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(&mime).unwrap_or(HeaderValue::from_static("application/octet-stream")),
    );
    resp
}
```

- [ ] **Step 3: Add module declaration**

In `updater/backend/src/main.rs`, add at the top:

```rust
mod static_files;
```

- [ ] **Step 4: Verify it compiles**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater
```

Expected: Compiles cleanly. The `include_dir!` macro requires `frontend/dist/` to exist (Step 1 ensured this).

- [ ] **Step 5: Commit**

```bash
git add updater/backend/src/static_files.rs updater/backend/src/main.rs
git commit -m "Add static_files module: embed frontend dist via include_dir"
```

---

### Task 8: Wire up axum router with all endpoints

**Why:** Connect the modules into a working HTTP server.

**Files:**
- Modify: `updater/backend/src/main.rs`

- [ ] **Step 1: Replace main.rs with the full router**

`updater/backend/src/main.rs`:

```rust
mod bitfocus;
mod companion;
mod static_files;
mod update;
mod version;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::{
    extract::State,
    http::StatusCode,
    response::sse::{Event, KeepAlive, Sse},
    response::Json,
    routing::get,
    Router,
};
use chrono::Local;
use futures::stream::Stream;
use serde::Serialize;
use tokio::sync::{mpsc, Mutex};
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::StreamExt;

const COOLDOWN_SECS: u64 = 300;

#[derive(Clone)]
struct AppState {
    http: reqwest::Client,
    last_update: Arc<Mutex<Option<Instant>>>,
    update_running: Arc<Mutex<bool>>,
}

#[derive(Serialize)]
struct StatusResponse {
    current_version: String,
    latest_version: String,
    update_available: bool,
    service_active: bool,
    service_enabled: bool,
    can_update: bool,
    cooldown_remaining: u64,
    last_checked: String,
    error: Option<String>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let state = AppState {
        http: reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .build()
            .expect("reqwest client"),
        last_update: Arc::new(Mutex::new(None)),
        update_running: Arc::new(Mutex::new(false)),
    };

    let app = Router::new()
        .route("/", get(static_files::index))
        .route("/assets/*path", get(static_files::asset))
        .route("/api/status", get(status_handler))
        .route("/api/update/stream", get(update_stream_handler))
        .route("/healthz", get(|| async { "ok" }))
        .with_state(state);

    let addr: SocketAddr = "0.0.0.0:8081".parse().unwrap();
    tracing::info!("companion-updater listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn status_handler(State(state): State<AppState>) -> Json<StatusResponse> {
    let current_raw = companion::read_installed_version().await;
    let latest_raw = bitfocus::fetch_latest_stable_linux(&state.http).await;
    let service_active = companion::service_is_active().await;
    let service_enabled = companion::service_is_enabled().await;

    let cooldown_remaining = {
        let last = state.last_update.lock().await;
        match *last {
            Some(t) => {
                let elapsed = t.elapsed().as_secs();
                if elapsed >= COOLDOWN_SECS {
                    0
                } else {
                    COOLDOWN_SECS - elapsed
                }
            }
            None => 0,
        }
    };

    let update_running = *state.update_running.lock().await;
    let can_update = cooldown_remaining == 0 && !update_running;

    let (current_version, latest_version, update_available, error) =
        match (current_raw, latest_raw) {
            (Ok(c), Ok(l)) => {
                let avail = version::is_update_available(&c, &l);
                (version::format(&c), version::format(&l), avail, None)
            }
            (Err(e), _) => (
                "unknown".into(),
                "unknown".into(),
                false,
                Some(format!("Cannot read installed version: {e}")),
            ),
            (Ok(c), Err(e)) => (
                version::format(&c),
                "unknown".into(),
                false,
                Some(format!("Cannot fetch latest version: {e}")),
            ),
        };

    Json(StatusResponse {
        current_version,
        latest_version,
        update_available,
        service_active,
        service_enabled,
        can_update,
        cooldown_remaining,
        last_checked: Local::now().format("%H:%M:%S").to_string(),
        error,
    })
}

async fn update_stream_handler(
    State(state): State<AppState>,
) -> Result<
    Sse<impl Stream<Item = Result<Event, std::convert::Infallible>>>,
    (StatusCode, String),
> {
    {
        let mut running = state.update_running.lock().await;
        if *running {
            return Err((
                StatusCode::CONFLICT,
                "Update already in progress".into(),
            ));
        }
        let last = state.last_update.lock().await;
        if let Some(t) = *last {
            let elapsed = t.elapsed().as_secs();
            if elapsed < COOLDOWN_SECS {
                return Err((
                    StatusCode::TOO_MANY_REQUESTS,
                    format!("Cooldown active. Wait {} seconds.", COOLDOWN_SECS - elapsed),
                ));
            }
        }
        *running = true;
    }

    let (tx, rx) = mpsc::channel::<update::UpdateEvent>(64);
    let state_clone = state.clone();

    tokio::spawn(async move {
        update::run_update(tx).await;
        let mut running = state_clone.update_running.lock().await;
        *running = false;
        let mut last = state_clone.last_update.lock().await;
        *last = Some(Instant::now());
    });

    let stream = ReceiverStream::new(rx).map(|event| {
        let json = serde_json::to_string(&event).unwrap_or_else(|_| "{}".into());
        Ok(Event::default().data(json))
    });

    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}
```

- [ ] **Step 2: Add tokio-stream dependency**

In `updater/backend/Cargo.toml`, add to `[dependencies]`:

```toml
tokio-stream = "0.1"
```

- [ ] **Step 3: Build**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater
```

Expected: Compiles cleanly.

- [ ] **Step 4: Run unit tests for the whole backend**

```bash
cargo test -p companion-updater
```

Expected: All tests from version, bitfocus, companion, update pass.

- [ ] **Step 5: Smoke test the binary locally**

```bash
cd /home/newlevel/devel/companion/updater
RUST_LOG=info cargo run -p companion-updater &
sleep 3
curl -s http://127.0.0.1:8081/healthz
curl -s http://127.0.0.1:8081/api/status | python3 -m json.tool
kill %1 2>/dev/null || pkill -f companion-updater
wait 2>/dev/null
```

Expected:
- `/healthz` returns `ok`
- `/api/status` returns JSON. Since this dev machine doesn't have `/opt/companion/package.json`, expect an `error` field — that's fine, we're testing wiring.

- [ ] **Step 6: Commit**

```bash
git add updater/backend/src/main.rs updater/backend/Cargo.toml
git commit -m "Wire up axum router with status and update endpoints"
```

---

### Task 9: Build frontend Leptos app skeleton

**Why:** Replace the placeholder frontend with the real component tree and a status fetcher.

**Files:**
- Modify: `updater/frontend/src/main.rs`
- Create: `updater/frontend/src/app.rs`

- [ ] **Step 1: Replace main.rs**

`updater/frontend/src/main.rs`:

```rust
mod app;

use app::App;

fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(App);
}
```

- [ ] **Step 2: Create app.rs with status state and polling**

`updater/frontend/src/app.rs`:

```rust
use leptos::prelude::*;
use serde::Deserialize;

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Status {
    pub current_version: String,
    pub latest_version: String,
    pub update_available: bool,
    pub service_active: bool,
    pub service_enabled: bool,
    pub can_update: bool,
    pub cooldown_remaining: u64,
    pub last_checked: String,
    #[serde(default)]
    pub error: Option<String>,
}

#[component]
pub fn App() -> impl IntoView {
    let (status, set_status) = signal::<Option<Status>>(None);
    let (progress_lines, set_progress_lines) = signal::<Vec<(String, String)>>(vec![]);
    let (updating, set_updating) = signal(false);

    // Initial fetch + 30s polling
    Effect::new(move |_| {
        let set_status = set_status;
        wasm_bindgen_futures::spawn_local(async move {
            fetch_status(set_status).await;
        });
    });

    let refresh = move |_| {
        wasm_bindgen_futures::spawn_local(async move {
            fetch_status(set_status).await;
        });
    };

    view! {
        <div class="container">
            <header>
                <h1>"Companion Update Dashboard"</h1>
                <p class="subtitle">"Bitfocus Companion (companion-pi)"</p>
            </header>

            <div class="card">
                <crate::components::status_card::StatusCard status=status />
            </div>

            <div class="card">
                <crate::components::update_button::UpdateButton
                    status=status
                    updating=updating
                    set_updating=set_updating
                    set_progress_lines=set_progress_lines
                    set_status=set_status
                />
                <crate::components::progress_log::ProgressLog
                    lines=progress_lines
                />
            </div>

            <div class="actions">
                <button class="refresh-btn" on:click=refresh>"Refresh"</button>
            </div>
        </div>
    }
}

async fn fetch_status(set_status: WriteSignal<Option<Status>>) {
    let result = gloo_net::http::Request::get("/api/status")
        .send()
        .await;
    match result {
        Ok(resp) => {
            if let Ok(s) = resp.json::<Status>().await {
                set_status.set(Some(s));
            }
        }
        Err(e) => {
            web_sys::console::error_1(&format!("status fetch failed: {e}").into());
        }
    }
}
```

- [ ] **Step 3: Build frontend (will fail — components don't exist yet)**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build 2>&1 | tail -10
```

Expected: Compile error referencing `crate::components`. This is fine — Task 10 fills it in.

- [ ] **Step 4: Commit (intermediate)**

```bash
cd /home/newlevel/devel/companion
git add updater/frontend/src/main.rs updater/frontend/src/app.rs
git commit -m "Add Leptos App component (compiles after components added)"
```

---

### Task 10: Build frontend components

**Why:** Implement the three components the App references: StatusCard, UpdateButton, ProgressLog.

**Files:**
- Create: `updater/frontend/src/components/mod.rs`
- Create: `updater/frontend/src/components/status_card.rs`
- Create: `updater/frontend/src/components/update_button.rs`
- Create: `updater/frontend/src/components/progress_log.rs`
- Modify: `updater/frontend/src/main.rs` (add `mod components;`)

- [ ] **Step 1: Add components module declaration**

In `updater/frontend/src/main.rs`, add `mod components;` so the file becomes:

```rust
mod app;
mod components;

use app::App;

fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(App);
}
```

- [ ] **Step 2: Create components/mod.rs**

`updater/frontend/src/components/mod.rs`:

```rust
pub mod progress_log;
pub mod status_card;
pub mod update_button;
```

- [ ] **Step 3: Create StatusCard component**

`updater/frontend/src/components/status_card.rs`:

```rust
use crate::app::Status;
use leptos::prelude::*;

#[component]
pub fn StatusCard(status: ReadSignal<Option<Status>>) -> impl IntoView {
    view! {
        <Show
            when=move || status.get().is_some()
            fallback=|| view! { <p class="loading">"Loading status..."</p> }
        >
            {move || {
                let s = status.get().unwrap_or_default();
                let latest_class = if s.update_available { "version-number latest" } else { "version-number up-to-date" };
                let svc_class = if s.service_active { "status-badge badge-running" } else { "status-badge badge-stopped" };
                let svc_text = if s.service_active { "Running" } else { "Stopped" };
                let upd_class = if s.update_available { "status-badge badge-update" } else { "status-badge badge-current" };
                let upd_text = if s.update_available { "Update Available" } else { "Up to Date" };
                view! {
                    <div class="version-grid">
                        <div class="version-box">
                            <div class="version-label">"Current"</div>
                            <div class="version-number current">{s.current_version.clone()}</div>
                        </div>
                        <div class="version-box">
                            <div class="version-label">"Latest"</div>
                            <div class={latest_class}>{s.latest_version.clone()}</div>
                        </div>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Service"</span>
                        <span class={svc_class}>{svc_text}</span>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Status"</span>
                        <span class={upd_class}>{upd_text}</span>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Last checked"</span>
                        <span class="status-value">{s.last_checked.clone()}</span>
                    </div>
                    {s.error.clone().map(|e| view! {
                        <div class="error-banner">{e}</div>
                    })}
                }
            }}
        </Show>
    }
}
```

- [ ] **Step 4: Create UpdateButton component**

`updater/frontend/src/components/update_button.rs`:

```rust
use crate::app::Status;
use leptos::prelude::*;
use serde::Deserialize;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{EventSource, MessageEvent};

#[derive(Deserialize)]
struct UpdateEvent {
    #[serde(rename = "type")]
    kind: String,
    message: String,
}

#[component]
pub fn UpdateButton(
    status: ReadSignal<Option<Status>>,
    updating: ReadSignal<bool>,
    set_updating: WriteSignal<bool>,
    set_progress_lines: WriteSignal<Vec<(String, String)>>,
    set_status: WriteSignal<Option<Status>>,
) -> impl IntoView {
    let label = move || {
        if updating.get() {
            return "Updating...".to_string();
        }
        let s = match status.get() {
            Some(s) => s,
            None => return "Checking...".to_string(),
        };
        if !s.update_available {
            "Up to Date".to_string()
        } else if !s.can_update && s.cooldown_remaining > 0 {
            format!("Cooldown: {}s", s.cooldown_remaining)
        } else {
            "Update Now".to_string()
        }
    };

    let class = move || {
        if updating.get() {
            "update-btn in-progress"
        } else {
            match status.get() {
                Some(s) if s.update_available && s.can_update => "update-btn available",
                _ => "update-btn disabled",
            }
        }
    };

    let disabled = move || {
        if updating.get() {
            return true;
        }
        match status.get() {
            Some(s) => !(s.update_available && s.can_update),
            None => true,
        }
    };

    let on_click = move |_| {
        if updating.get_untracked() {
            return;
        }
        set_updating.set(true);
        set_progress_lines.set(vec![]);

        let es = match EventSource::new("/api/update/stream") {
            Ok(es) => es,
            Err(e) => {
                web_sys::console::error_1(&format!("EventSource error: {e:?}").into());
                set_updating.set(false);
                return;
            }
        };

        let es_for_msg = es.clone();
        let on_message = Closure::<dyn FnMut(MessageEvent)>::new(
            move |event: MessageEvent| {
                let data = event.data().as_string().unwrap_or_default();
                let parsed: UpdateEvent = match serde_json::from_str(&data) {
                    Ok(p) => p,
                    Err(_) => return,
                };
                set_progress_lines.update(|v| {
                    v.push((parsed.kind.clone(), parsed.message.clone()));
                });
                if parsed.kind == "complete" || parsed.kind == "error" {
                    es_for_msg.close();
                    set_updating.set(false);
                    // Refresh status after a short delay
                    let set_status = set_status;
                    wasm_bindgen_futures::spawn_local(async move {
                        gloo_timers::future::TimeoutFuture::new(1500).await;
                        if let Ok(resp) = gloo_net::http::Request::get("/api/status").send().await {
                            if let Ok(s) = resp.json::<Status>().await {
                                set_status.set(Some(s));
                            }
                        }
                    });
                }
            },
        );
        es.set_onmessage(Some(on_message.as_ref().unchecked_ref()));
        on_message.forget();

        let es_for_err = es.clone();
        let on_error = Closure::<dyn FnMut(_)>::new(move |_event: web_sys::Event| {
            es_for_err.close();
            set_updating.set(false);
            set_progress_lines.update(|v| {
                v.push((
                    "error".into(),
                    "Connection lost. Please refresh.".into(),
                ));
            });
        });
        es.set_onerror(Some(on_error.as_ref().unchecked_ref()));
        on_error.forget();
    };

    view! {
        <button class={class} disabled={disabled} on:click=on_click>
            {label}
        </button>
    }
}
```

- [ ] **Step 5: Create ProgressLog component**

`updater/frontend/src/components/progress_log.rs`:

```rust
use leptos::prelude::*;

#[component]
pub fn ProgressLog(lines: ReadSignal<Vec<(String, String)>>) -> impl IntoView {
    let visible = move || !lines.get().is_empty();
    view! {
        <Show when=visible fallback=|| view! { <span></span> }>
            <div class="progress-container active">
                <div class="progress-log">
                    <For
                        each=move || lines.get().into_iter().enumerate().collect::<Vec<_>>()
                        key=|(i, _)| *i
                        children=move |(_i, (kind, msg))| {
                            let cls = match kind.as_str() {
                                "error" => "line error",
                                "complete" => "line success",
                                _ => "line",
                            };
                            view! { <div class={cls}>{msg}</div> }
                        }
                    />
                </div>
            </div>
        </Show>
    }
}
```

- [ ] **Step 6: Add gloo-timers dependency**

In `updater/frontend/Cargo.toml`, add:

```toml
gloo-timers = { version = "0.3", features = ["futures"] }
```

- [ ] **Step 7: Build the frontend**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build --release
```

Expected: Compiles cleanly. `dist/` updated with new WASM.

- [ ] **Step 8: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/frontend/
git commit -m "Add frontend components: StatusCard, UpdateButton, ProgressLog"
```

---

### Task 11: Add CSS styling

**Why:** Replicate the dark-themed dashboard look from the original Python app.

**Files:**
- Create: `updater/frontend/style.css`
- Modify: `updater/frontend/index.html`

- [ ] **Step 1: Create style.css**

`updater/frontend/style.css`:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    min-height: 100vh;
    color: #e0e0e0;
    padding: 2rem;
}

.container { max-width: 600px; margin: 0 auto; }
header { text-align: center; margin-bottom: 2rem; }
h1 { font-size: 2rem; color: #fff; margin-bottom: 0.5rem; }
.subtitle { color: #888; font-size: 0.9rem; }

.card {
    background: rgba(255, 255, 255, 0.05);
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1rem;
    border: 1px solid rgba(255, 255, 255, 0.1);
}

.version-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-bottom: 1rem;
}

.version-box {
    background: rgba(0, 0, 0, 0.2);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
}

.version-label {
    font-size: 0.8rem;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 0.5rem;
}

.version-number { font-size: 1.8rem; font-weight: bold; color: #fff; }
.version-number.current { color: #4ecdc4; }
.version-number.latest { color: #ff6b6b; }
.version-number.up-to-date { color: #4ecdc4; }

.status-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}
.status-row:last-child { border-bottom: none; }
.status-label { color: #888; }
.status-value { font-weight: 500; }

.status-badge {
    display: inline-block;
    padding: 0.25rem 0.75rem;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 500;
}

.badge-running { background: rgba(78, 205, 196, 0.2); color: #4ecdc4; }
.badge-stopped { background: rgba(255, 107, 107, 0.2); color: #ff6b6b; }
.badge-update { background: rgba(255, 193, 7, 0.2); color: #ffc107; }
.badge-current { background: rgba(78, 205, 196, 0.2); color: #4ecdc4; }

.update-btn {
    width: 100%;
    padding: 1rem;
    font-size: 1.1rem;
    font-weight: 600;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.3s ease;
    text-transform: uppercase;
    letter-spacing: 1px;
}
.update-btn.available {
    background: linear-gradient(135deg, #ff6b6b 0%, #ee5a5a 100%);
    color: white;
}
.update-btn.available:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 15px rgba(255, 107, 107, 0.4);
}
.update-btn.disabled { background: #333; color: #666; cursor: not-allowed; }
.update-btn.in-progress { background: #444; color: #888; cursor: wait; }

.progress-container { margin-top: 1rem; }
.progress-log {
    background: #000;
    border-radius: 8px;
    padding: 1rem;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    font-size: 0.85rem;
    max-height: 300px;
    overflow-y: auto;
    line-height: 1.6;
}
.progress-log .line { color: #4ecdc4; }
.progress-log .error { color: #ff6b6b; }
.progress-log .success { color: #4ecdc4; font-weight: bold; }

.actions { text-align: center; margin-top: 1rem; }
.refresh-btn {
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 0.2);
    color: #888;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.85rem;
}
.refresh-btn:hover { border-color: rgba(255, 255, 255, 0.4); color: #fff; }

.error-banner {
    background: rgba(255, 107, 107, 0.15);
    border: 1px solid rgba(255, 107, 107, 0.3);
    color: #ff6b6b;
    padding: 0.75rem;
    border-radius: 8px;
    margin-top: 1rem;
    font-size: 0.85rem;
}

.loading { text-align: center; color: #888; padding: 1rem; }
```

- [ ] **Step 2: Update index.html to load CSS via trunk**

`updater/frontend/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Companion Update Dashboard</title>
    <link data-trunk rel="css" href="style.css" />
    <link data-trunk rel="rust" data-bin="companion-updater-frontend" data-wasm-opt="z" />
</head>
<body></body>
</html>
```

- [ ] **Step 3: Rebuild frontend**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build --release
ls dist/
```

Expected: `dist/` now contains a CSS file alongside HTML/WASM/JS.

- [ ] **Step 4: Rebuild backend (re-embeds dist)**

```bash
cd /home/newlevel/devel/companion/updater
cargo build --release -p companion-updater
```

Expected: Compiles cleanly.

- [ ] **Step 5: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/frontend/style.css updater/frontend/index.html
git commit -m "Add CSS styling for updater dashboard"
```

---

### Task 12: Build orchestration script

**Why:** One command to build frontend then backend in the right order.

**Files:**
- Create: `updater/build.sh`

- [ ] **Step 1: Write build.sh**

`updater/build.sh`:

```bash
#!/bin/bash
set -euo pipefail

# Build the companion-updater binary.
# 1. Build the WASM frontend with trunk → frontend/dist/
# 2. Build the Rust backend, which embeds frontend/dist/ via include_dir!

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[1/2] Building frontend (trunk)..."
cd "${SCRIPT_DIR}/frontend"
trunk build --release

echo "[2/2] Building backend (cargo)..."
cd "${SCRIPT_DIR}"
cargo build --release -p companion-updater

BIN="${SCRIPT_DIR}/target/release/companion-updater"
if [ ! -x "${BIN}" ]; then
  echo "ERROR: binary not produced at ${BIN}"
  exit 1
fi

SIZE=$(du -h "${BIN}" | cut -f1)
echo ""
echo "Binary: ${BIN} (${SIZE})"
```

- [ ] **Step 2: Make executable and run**

```bash
cd /home/newlevel/devel/companion/updater
chmod +x build.sh
./build.sh
```

Expected: Both builds succeed, prints final binary path and size (~5-10 MB).

- [ ] **Step 3: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/build.sh
git commit -m "Add build.sh for frontend+backend in one command"
```

---

### Task 13: Create systemd unit file

**Why:** The updater needs to start at boot and survive crashes.

**Files:**
- Create: `updater/companion-updater.service`

- [ ] **Step 1: Create service unit**

`updater/companion-updater.service`:

```ini
[Unit]
Description=Bitfocus Companion Update Dashboard
After=network-online.target companion.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/companion-updater
Restart=on-failure
RestartSec=10
Environment=RUST_LOG=info

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Validate syntax**

```bash
systemd-analyze verify /home/newlevel/devel/companion/updater/companion-updater.service 2>&1 || true
```

Expected: No errors. (May warn about non-existent paths, which is fine — the binary doesn't exist on the dev machine.)

- [ ] **Step 3: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/companion-updater.service
git commit -m "Add systemd unit file for companion-updater"
```

---

### Task 14: Update deploy.sh for native updater

**Why:** The current deploy.sh still copies the old `updater/` Docker files. Replace that section with binary deployment.

**Files:**
- Modify: `deploy.sh`

- [ ] **Step 1: Read current deploy.sh**

```bash
cat /home/newlevel/devel/companion/deploy.sh
```

Note the existing structure — we replace Step 5 (Docker updater) with native binary deployment.

- [ ] **Step 2: Rewrite deploy.sh**

`deploy.sh`:

```bash
#!/bin/bash
set -euo pipefail

# Deploy companion stack to remote host via SSH
# Usage: ./deploy.sh
# Configuration via environment variables (see defaults below)
#
# Architecture:
#   - Companion: native systemd service (companion-pi)
#   - Cloudflare tunnel: native systemd service (cloudflared)
#   - Update Dashboard: native systemd service (companion-updater Rust binary)

COMPANION_HOST="${COMPANION_HOST:-companion.lan}"
COMPANION_USER="${COMPANION_USER:-newlevel}"
COMPANION_PASS="${COMPANION_PASS:-newlevel}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

remote() {
  sshpass -p "${COMPANION_PASS}" ssh ${SSH_OPTS} "${COMPANION_USER}@${COMPANION_HOST}" "$@"
}

remote_copy() {
  sshpass -p "${COMPANION_PASS}" scp ${SSH_OPTS} -r "$@"
}

echo "=== Deploying to ${COMPANION_HOST} ==="
echo ""

# Step 1: Validate connection
echo "[1/7] Testing connection to ${COMPANION_HOST}..."
if ! remote "echo ok" >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to ${COMPANION_USER}@${COMPANION_HOST}"
  exit 1
fi
echo "  Connected."

# Step 2: Install udev rules
echo "[2/7] Installing udev rules..."
for rule_file in "${SCRIPT_DIR}"/host/*.rules; do
  if [ -f "${rule_file}" ]; then
    rule_name="$(basename "${rule_file}")"
    remote_copy "${rule_file}" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/${rule_name}"
    remote "sudo cp /tmp/${rule_name} /etc/udev/rules.d/${rule_name} && rm /tmp/${rule_name}"
    echo "  Installed ${rule_name}"
  fi
done
remote "sudo udevadm control --reload-rules && sudo udevadm trigger"
echo "  Udev rules reloaded."

# Step 3: Update Companion via companion-update
echo "[3/7] Updating Companion..."
if remote "command -v companion-update >/dev/null 2>&1"; then
  remote "sudo bash /usr/local/src/companionpi/update.sh stable"
  echo "  Companion updated."
else
  echo "  companion-update not found — companion-pi may not be installed."
  echo "  Install with: curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash"
  exit 1
fi

# Step 4: Restart Companion service
echo "[4/7] Restarting Companion service..."
remote "sudo systemctl restart companion"
echo "  Companion service restarted."

# Step 5: Build companion-updater binary locally
echo "[5/7] Building companion-updater..."
"${SCRIPT_DIR}/updater/build.sh"
BIN="${SCRIPT_DIR}/updater/target/release/companion-updater"
if [ ! -x "${BIN}" ]; then
  echo "ERROR: ${BIN} not found"
  exit 1
fi

# Step 6: Deploy companion-updater binary and systemd unit
echo "[6/7] Deploying companion-updater..."
remote "sudo systemctl stop companion-updater 2>/dev/null || true"
remote_copy "${BIN}" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-updater"
remote "sudo install -m 0755 /tmp/companion-updater /usr/local/bin/companion-updater && rm /tmp/companion-updater"
remote_copy "${SCRIPT_DIR}/updater/companion-updater.service" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-updater.service"
remote "sudo install -m 0644 /tmp/companion-updater.service /etc/systemd/system/companion-updater.service && rm /tmp/companion-updater.service"
remote "sudo systemctl daemon-reload && sudo systemctl enable --now companion-updater"
echo "  companion-updater deployed."

# Step 7: Health check
echo "[7/7] Waiting for services to be ready..."
MAX_WAIT=60
ELAPSED=0
while [ "${ELAPSED}" -lt "${MAX_WAIT}" ]; do
  if curl -sf --max-time 5 "http://${COMPANION_HOST}:8000/" >/dev/null 2>&1 \
     && curl -sf --max-time 5 "http://${COMPANION_HOST}:8081/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 5
  ELAPSED=$((ELAPSED + 5))
  echo "  Waiting... (${ELAPSED}s)"
done

if ! curl -sf --max-time 5 "http://${COMPANION_HOST}:8000/" >/dev/null 2>&1; then
  echo "WARNING: Companion did not become ready within ${MAX_WAIT}s"
  echo "  Check: ssh ${COMPANION_USER}@${COMPANION_HOST} sudo journalctl -u companion -n 50"
  exit 1
fi

if ! curl -sf --max-time 5 "http://${COMPANION_HOST}:8081/healthz" >/dev/null 2>&1; then
  echo "WARNING: companion-updater did not become ready within ${MAX_WAIT}s"
  echo "  Check: ssh ${COMPANION_USER}@${COMPANION_HOST} sudo journalctl -u companion-updater -n 50"
  exit 1
fi

HOST_IP=$(remote "hostname -I | awk '{print \$1}'" 2>/dev/null || echo "${COMPANION_HOST}")

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "  Companion:        http://${HOST_IP}:8000"
echo "  Update Dashboard: http://${HOST_IP}:8081"
echo ""
```

- [ ] **Step 3: Validate syntax**

```bash
bash -n /home/newlevel/devel/companion/deploy.sh
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
cd /home/newlevel/devel/companion
git add deploy.sh
git commit -m "Update deploy.sh for native Rust companion-updater"
```

---

### Task 15: Deploy and verify

**Why:** Confirm the entire stack works end-to-end on companion.lan.

- [ ] **Step 1: Run deploy**

```bash
cd /home/newlevel/devel/companion
./deploy.sh
```

Expected: All 7 steps complete, final URLs printed.

- [ ] **Step 2: Verify systemd service is active**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "systemctl status companion-updater --no-pager | head -10"
```

Expected: `Active: active (running)`.

- [ ] **Step 3: Verify health endpoint**

```bash
curl -sf http://companion.lan:8081/healthz
```

Expected: `ok`.

- [ ] **Step 4: Verify status endpoint returns real data**

```bash
curl -s http://companion.lan:8081/api/status | python3 -m json.tool
```

Expected:
- `current_version` matches the version in `/opt/companion/package.json` on the host (formatted as `vX.Y.Z`)
- `latest_version` matches the latest `linux-tgz` package from Bitfocus stable channel
- `service_active` is `true`
- `service_enabled` is `true`
- No `error` field present
- `update_available` is true or false depending on whether they match

- [ ] **Step 5: Verify the dashboard loads in a browser via Playwright**

Use the playwright MCP to:
1. Navigate to `http://companion.lan:8081/`
2. Take a snapshot
3. Verify the page shows "Companion Update Dashboard" heading
4. Verify the version values render (not stuck on "Loading...")
5. Check the browser console — must have zero errors and zero warnings (per `browser-console-zero-errors.md`)

- [ ] **Step 6: Update README.md**

In `/home/newlevel/devel/companion/README.md`, replace the section that says "Update Dashboard (Docker)" with:

```markdown
### 2. Update Dashboard (native Rust binary)

Built with `updater/build.sh` and deployed via `deploy.sh` as a systemd service:

- Binary: `/usr/local/bin/companion-updater`
- Service: `systemctl status companion-updater`
- Port: 8081

The updater reads `/opt/companion/package.json` for the current version,
fetches the latest stable from the Bitfocus builds API, and runs
`update.sh stable` when triggered.
```

Also update the Directory Structure to:

```
Repository:
├── companion/           # Docker setup (legacy, kept for reference)
├── updater/             # Rust + WASM updater
│   ├── Cargo.toml
│   ├── backend/         # axum HTTP server
│   ├── frontend/        # Leptos WASM dashboard
│   ├── companion-updater.service
│   └── build.sh
├── host/                # Udev rules
├── deploy.sh
└── README.md
```

And update Troubleshooting to add:

```markdown
### Update Dashboard issues
\`\`\`bash
# Check service status
sudo systemctl status companion-updater
# Check full logs
sudo journalctl -u companion-updater -n 100
\`\`\`
```

- [ ] **Step 7: Commit README update**

```bash
cd /home/newlevel/devel/companion
git add README.md
git commit -m "Update README for Rust+WASM updater"
```

- [ ] **Step 8: Push branch**

```bash
git push origin dev
```

---

### Task 16: Create PR

- [ ] **Step 1: Open PR**

```bash
gh pr create --base main --head dev \
  --title "Rewrite companion-updater as native Rust+WASM binary" \
  --body "$(cat <<'EOF'
## Summary
- Replace Python/Docker updater with native Rust binary (axum backend + Leptos/WASM frontend)
- Reads current version from /opt/companion/package.json
- Fetches latest stable Linux build from Bitfocus builds API
- Triggers updates via `sudo bash /usr/local/src/companionpi/update.sh stable`
- Streams update output via Server-Sent Events
- Runs as a root-owned systemd service on port 8081
- Old Docker updater removed entirely

## Test plan
- [x] Backend unit tests pass (version, bitfocus, companion, update modules)
- [x] Frontend builds with trunk (release WASM)
- [x] /healthz returns "ok"
- [x] /api/status returns real version data, no error field
- [x] Dashboard loads in browser, no console errors/warnings
- [x] systemctl status companion-updater is active

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Verify mergeable + clean**

```bash
sleep 5
gh pr view --json number,mergeable,mergeStateStatus
```

Expected: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`.

Wait for explicit user merge instruction.
