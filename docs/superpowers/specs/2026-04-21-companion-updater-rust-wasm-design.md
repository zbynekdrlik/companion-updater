# Companion Updater Rewrite — Rust + WASM for Native Companion

## Problem

The existing companion-updater web dashboard (Python/FastAPI in Docker) stopped working after Companion was migrated from Docker to a native systemd service via companion-pi. The current updater:

- Reads current version from Docker container labels (`docker inspect`) — the container no longer exists
- Fetches latest version from GitHub releases (incorrect source for companion-pi; Bitfocus uses its own builds API)
- Updates by running `docker pull` + `docker compose up -d --build` — no Docker image to rebuild

## Goal

Rewrite the updater as a native systemd service that manages the companion-pi installation. Keep the same port (8081), same web UI behavior, and same functional scope. Migrate the entire stack to Rust with a WebAssembly frontend.

## Scope

- **In scope:** Full rewrite of the `updater/` directory in Rust. Replace Docker-based deploy with native systemd service. Update `deploy.sh`.
- **Out of scope:** Companion config management. Authentication (single-purpose appliance on LAN). Beta/custom version selection — stable only.

## Architecture

Single Rust binary that serves both the web UI (WASM bundle embedded at compile time) and the backend API. Runs as a root-owned systemd service on the host, identical to how companion-pi itself runs.

```
Browser → HTTP → axum backend (port 8081)
   │                 │
   │                 ├─ GET /              → serve embedded HTML/JS/WASM
   │                 ├─ GET /api/status    → read package.json, call systemctl, fetch Bitfocus API
   │                 └─ GET /api/update/stream → run update.sh, stream stdout via SSE
   │
   └── WASM (Leptos CSR) — renders status card, update button, SSE progress log
```

### Backend (Rust + axum)

- **Framework:** `axum` 0.7+ for HTTP routing and Server-Sent Events
- **HTTP client:** `reqwest` for Bitfocus builds API
- **Subprocess:** `tokio::process::Command` for `systemctl` and `update.sh`
- **Serialization:** `serde` + `serde_json`
- **Static files:** `include_dir!` macro embeds the WASM build output in the binary
- **Runs as:** root (systemd `User=root`) — needed to invoke `update.sh` and `systemctl restart companion`

### Frontend (Rust + Leptos + WASM)

- **Framework:** `leptos` with CSR (client-side rendering) mode
- **Build tool:** `trunk` — compiles the Rust frontend crate to WASM, bundles with HTML/CSS
- **Components:**
  - `App` — root, holds status signal, wires SSE
  - `StatusCard` — version comparison, service status, last-checked timestamp
  - `UpdateButton` — triggers update, disabled during cooldown or in-progress
  - `ProgressLog` — receives SSE events, appends lines with color coding
- **SSE consumption:** `web-sys::EventSource` via `wasm-bindgen`

### File layout

```
updater/
├── Cargo.toml                       # Cargo workspace root
├── backend/
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs                  # axum app, router, bind to 0.0.0.0:8081
│       ├── companion.rs             # read /opt/companion/package.json; systemctl status
│       ├── bitfocus.rs              # Bitfocus builds API client
│       ├── update.rs                # run update.sh, stream output via broadcast channel
│       ├── version.rs               # parse and compare version strings
│       └── static_files.rs          # include_dir! macro + serve_static handler
├── frontend/
│   ├── Cargo.toml
│   ├── index.html                   # trunk entry point, <link data-trunk rel="rust">
│   ├── style.css
│   └── src/
│       ├── main.rs                  # mount Leptos app
│       ├── app.rs                   # root component
│       └── components/
│           ├── mod.rs
│           ├── status_card.rs
│           ├── update_button.rs
│           └── progress_log.rs
├── companion-updater.service        # systemd unit file
├── build.sh                         # trunk build + cargo build --release
└── README.md
```

## Data flow

### Status endpoint (`GET /api/status`)

1. Read `/opt/companion/package.json`, extract `version` field (e.g., `"4.2.6+8823"`)
2. Split on `+` to get semver part (`4.2.6`)
3. `GET https://api.bitfocus.io/v1/product/companion/packages?branch=stable`
4. Parse response, find the highest version in the `stable` channel
5. Compare versions (left-to-right numeric comparison on semver)
6. Call `systemctl is-active companion` and `systemctl is-enabled companion`
7. Return JSON:

```json
{
  "current_version": "v4.2.6",
  "latest_version": "v4.2.8",
  "update_available": true,
  "service_active": true,
  "service_enabled": true,
  "can_update": true,
  "cooldown_remaining": 0,
  "last_checked": "14:23:05"
}
```

### Update endpoint (`GET /api/update/stream`, SSE)

1. Check cooldown (in-memory; 5 min since last update)
2. Spawn `sudo bash /usr/local/src/companionpi/update.sh stable` with piped stdout/stderr
3. Read lines from the child's output, emit as SSE events: `data: {"type":"progress","message":"<line>"}\n\n`
4. When child exits successfully, run `systemctl restart companion`
5. Wait briefly, read new version from `package.json`
6. Emit `data: {"type":"complete","message":"Now running v4.2.8"}\n\n`
7. On any failure, emit `data: {"type":"error","message":"<detail>"}\n\n`
8. Close the SSE stream

Cooldown timer updates on successful completion only.

## Permissions & security

- Service runs as `root`. Justification: needs to invoke `update.sh` (writes to `/opt/companion`) and `systemctl restart companion`. Alternative sudoers rules with a dedicated user add complexity for no real benefit on this single-purpose LAN appliance.
- No authentication on the web UI. The appliance is LAN-only; the Cloudflare tunnel only exposes port 8000 (Companion), not 8081 (updater).
- No user input is passed to shell commands. The update command is a fixed string. No SQL, no file paths from the network.

## Build process

1. `cd updater/frontend && trunk build --release` → produces `dist/` with `.wasm`, `.js`, `index.html`, `style.css`
2. `cd updater/backend && cargo build --release` → embeds `../frontend/dist/` via `include_dir!`, produces `target/release/companion-updater`
3. `updater/build.sh` orchestrates both steps

Binary is ~5-10 MB (axum + reqwest + embedded WASM).

## Systemd unit file

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

[Install]
WantedBy=multi-user.target
```

## Deployment

`deploy.sh` gains a new section (or separate `deploy-updater.sh`):

1. Build binary locally (assumes x86_64 Linux target; companion.lan is Ubuntu 24.04 x86_64)
2. SCP `target/release/companion-updater` → `/usr/local/bin/companion-updater` on host
3. SCP `companion-updater.service` → `/etc/systemd/system/companion-updater.service`
4. `sudo systemctl daemon-reload && sudo systemctl enable --now companion-updater`
5. Verify: `curl -f http://companion.lan:8081/`

## Versioning & compatibility

- The updater depends on two paths existing on the host:
  - `/opt/companion/package.json` (written by companion-pi)
  - `/usr/local/src/companionpi/update.sh` (written by companion-pi installer)
- If either is missing, status endpoint returns a clear error message in the UI instead of crashing.

## Rollback

If the Rust updater fails after deploy:
1. `sudo systemctl stop companion-updater`
2. No impact on Companion itself — the updater is independent.
3. Fix issue, redeploy.

The old Docker updater is removed entirely in this rewrite (no rollback path to Docker). The existing `updater/` directory is replaced.

## Verification

1. Binary runs on host as systemd service (`systemctl status companion-updater` → active).
2. Web UI accessible at `http://companion.lan:8081/`.
3. Status endpoint returns current version matching `cat /opt/companion/package.json | jq .version`.
4. Status endpoint shows latest version from Bitfocus stable channel.
5. Clicking "Update Now" streams log output via SSE and completes with new version displayed.
6. After update, `systemctl status companion` shows the service was restarted and is running.
7. 5-minute cooldown prevents rapid re-triggering.
8. Browser console shows zero errors or warnings after page load and after an update.
