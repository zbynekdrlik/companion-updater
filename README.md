# Bitfocus Companion Setup

Production setup for [Bitfocus Companion](https://bitfocus.io/companion) with USB hot-plug support and remote access.

## Architecture

- **Companion**: Native systemd service via [companion-pi](https://github.com/bitfocus/companion-pi) — direct USB/udev access for reliable Stream Deck hot-plug detection
- **Cloudflare Tunnel + Access**: `cloudflared-companion.service` publishes the Companion UI and the update dashboard behind Cloudflare Access (owner e-mail, one-time PIN)
- **Update Dashboard**: Native Rust binary (axum + Leptos/WASM) for checking and applying Companion updates, with a pre/post upgrade safety gate that auto-rolls back on data loss
- **Off-machine Backups**: Hourly push of the Companion config to a private GitHub repo (`zbynekdrlik/companion-backups`), 30-day history retention
- **Persistent Data**: Configuration stored at `~companion/.config/companion-nodejs/`

## Quick Start

### 1. Install Companion (native)

```bash
curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash
```

This installs Companion as a systemd service with USB support, udev rules, and auto-start on boot.

### 2. Install Update Dashboard (native Rust binary)

Built from `updater/` and deployed via `deploy.sh` as a systemd service:

- Binary: `/usr/local/bin/companion-updater`
- Service: `systemctl status companion-updater`
- Port: 8081

The updater reads `/opt/companion/package.json` for the current version and
fetches the latest stable from the Bitfocus builds API. Its own version is
shown on the dashboard as "Dashboard", so a deploy can be verified from the UI.

When an update is triggered it:

1. Exports the full Companion config and counts connections / buttons / triggers
   (pre-upgrade snapshot, archived under `/var/lib/companion-updater/`).
2. Runs `git pull` in `/usr/local/src/companionpi` — **required**: the version
   picker that `update.sh` invokes lives there, and a stale copy silently
   refuses every newer major release.
3. Stops Companion, runs `update.sh stable`, starts Companion, waits for health.
4. Re-exports and compares the counts; on any decrease it re-imports the
   pre-upgrade snapshot and reports a rollback.
5. Compares the installed version before/after. `update.sh` exits 0 even when it
   installs nothing ("Skipping update" / "No matching stable build was found!"),
   so a version that did not move is reported as an **error**, never as success.

### 3. Access

- **Companion**: `http://<your-ip>:8000`
- **Update Dashboard**: `http://<your-ip>:8081`

## Remote Deploy

Deploy to `companion.lan` (or any target host) over SSH:

```bash
# Deploys to companion.lan as newlevel; the SSH password is required and never committed
COMPANION_PASS=... ./deploy.sh

# Other target host or user
COMPANION_HOST=companion-pp.lan COMPANION_USER=admin COMPANION_PASS=... ./deploy.sh
```

The deploy script (`[1/9]`…`[9/9]` in its output):
1. Checks the SSH connection
2. Installs udev rules from `host/` to `/etc/udev/rules.d/`
3. Enables NTP time sync (`host/ensure-timesync.sh`) and fails if the clock does not synchronise
4. Installs the backup pusher (script + systemd timer)
5. Builds the `companion-updater` Rust binary locally
6. Installs it as a systemd service
7. Installs and starts the web access tunnel (`cloudflared-companion.service`), after the updater so the public dashboard is never served without its cross-site guard. The rig's hostnames come from `host/cloudflare_companion.py hosts`. Before starting it, it sets Companion's `trustedProxies: loopback` (restarting Companion only when needed) when Companion already has its launch config. It fails when the tunnel token is missing (opt out: `COMPANION_TUNNEL=skip`), when Companion refuses the web UI connection from the tunnel, when the unit dies, or when one of the rig's public URLs does not answer with the Cloudflare Access login
8. Triggers the Companion upgrade through that freshly deployed updater
   (binary first, upgrade second — otherwise a fix to the upgrade path could
   never take effect in the deploy that ships it). `COMPANION_UPGRADE=skip`
   deploys the tooling only and leaves Companion at its version
9. Waits for health checks to pass, then checks again, after the upgrade, that Companion accepts the web UI connection from the tunnel (whenever this deploy started the tunnel or one is running)

**Requirements:** `sshpass` must be installed on the machine running the deploy.

## Directory Structure

```
Repository:
├── companion/           # Docker setup (legacy, kept for reference)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── entrypoint.sh
│   └── .env.example
├── updater/             # Rust + WASM updater
│   ├── Cargo.toml       # workspace
│   ├── backend/         # axum HTTP/SSE server
│   ├── frontend/        # Leptos WASM dashboard
│   ├── companion-updater.service
│   └── build.sh
├── host/                # Udev rules for the target host
│   ├── 50-elgato.rules
│   └── 99-nldevicessetup.rules
├── deploy.sh            # Deploy to remote host via SSH
├── setup.sh             # One-click local installer (legacy)
└── README.md
```

## Configuration

### Web access over Cloudflare

| URL | Rig | Service |
|---|---|---|
| https://companion-snv.newlevel.media | companion-snv | Companion UI (:8000) |
| https://companion-update-snv.newlevel.media | companion-snv | Update dashboard (:8081) |
| https://companion-pp.newlevel.media | companion-pp | Companion UI (:8000) |
| https://companion-update-pp.newlevel.media | companion-pp | Update dashboard (:8081) |

Every URL is behind Cloudflare Access with a one-time PIN by e-mail:

- **companion-snv, companion-update-snv, companion-update-pp:** Access apps managed by `host/cloudflare_companion.py`. Only the owner's e-mail is allowed, with a 30-day session like the other newlevel apps.
- **companion-pp and the old `companion.newlevel.media`:** the owner's own Access app "newlevel". Its policy "basic" allows the owner and the two people he chose. The tool only checks this app; it never changes it.

cloudflared on the rig also rejects any request without a valid Access token for that hostname's app.

Companion itself has `trustedProxies: loopback` in `/etc/companion/config.yaml`. Without it, Companion 5 refuses the UI's live WebSocket from cloudflared (a loopback client with the public Host), and the page loads but stays on "connecting". `deploy.sh` sets it via `host/ensure-companion-proxy.sh` and checks the handshake.

**Never run `cloudflared service install <token>` for companion.** It rewrites `cloudflared.service`, which on these rigs belongs to the presenter project's tunnel. That command is how the companion tunnels went down unnoticed before. Companion runs its own unit, `cloudflared-companion.service`.

```bash
# Cloudflare side (Access apps, tunnel ingress, DNS, the tunnel-health e-mail): as code, idempotent
COMPANION_ACCESS_EMAILS=<owner e-mail> host/cloudflare_companion.py plan    # exit 3 = changes pending
COMPANION_ACCESS_EMAILS=<owner e-mail> host/cloudflare_companion.py apply
host/cloudflare_companion.py status                                         # exit 1 = a tunnel is down

# The rig's run token (the tunnel is chosen from the rig's hostname; never printed)
COMPANION_ACCESS_EMAILS=<owner e-mail> COMPANION_PASS=... host/cloudflare_companion.py install-token companion.lan
```

Then `deploy.sh` installs the unit. It fails if the tunnel token is missing (opt out with `COMPANION_TUNNEL=skip`), if the public URL does not answer with the Access login, or if Companion refuses the web UI connection from the tunnel.

## Ports

| Service | Port | Description |
|---------|------|-------------|
| Companion | 8000 | Main web interface |
| Companion | 16622 | Satellite API |
| Update Dashboard | 8081 | Update management UI |

## How Updates Work

1. Run `sudo companion-update` on the host, or
2. Use the Update Dashboard at port 8081 to check for updates and apply them

Configuration is preserved across updates.

## Troubleshooting

### Stream Deck not detected
```bash
# Check that hidraw devices exist
ls -la /dev/hidraw*
# Verify udev rules are installed
ls -la /etc/udev/rules.d/50-companion.rules
# Check Companion service logs
sudo journalctl -u companion --no-pager -n 50 | grep -i stream
# Restart Companion service
sudo systemctl restart companion
```

### Companion won't start
```bash
# Check service status
sudo systemctl status companion
# Check full logs
sudo journalctl -u companion --no-pager -n 100
```

### Cloudflare tunnel not working
```bash
# From dev1: tunnel health as Cloudflare sees it
host/cloudflare_companion.py status
# Anonymous request must get the Access login (302 to newlevelchurch.cloudflareaccess.com)
curl -sI https://companion-snv.newlevel.media | head -3
# On the rig (NOT cloudflared.service: that one is the presenter's tunnel)
sudo systemctl status cloudflared-companion
sudo journalctl -u cloudflared-companion --no-pager -n 50
# Page loads but stays on "connecting": Companion refused the UI's WebSocket
sudo journalctl -u companion --no-pager | grep 'Rejected tRPC'
sudo /usr/local/sbin/ensure-companion-proxy.sh companion-snv.newlevel.media   # sets + checks trustedProxies
```

### Update Dashboard issues
```bash
# Check service status
sudo systemctl status companion-updater
# Check full logs
sudo journalctl -u companion-updater -n 100
```

## Backups & Safety

### Off-machine backups

Each Companion host pushes the latest `.companionconfig` export to a private
GitHub repo (`zbynekdrlik/companion-backups`) hourly via systemd timer.
Last 30 days of hourly snapshots are retained per machine.

Setup on a new host (one-time, after `deploy.sh` has installed the
backup-pusher script and units):

```bash
sudo /usr/local/sbin/setup-backup-key.sh
# Generates a deploy key, prints the public key, waits for ENTER once you've
# added it (with write access) at:
# https://github.com/zbynekdrlik/companion-backups/settings/keys
# Then clones the repo and enables the timer.
```

Manual trigger / status:

```bash
sudo systemctl start companion-backup-push.service
sudo systemctl status companion-backup-push.timer
sudo journalctl -u companion-backup-push.service -n 30
```

### Upgrade safety gate

The Rust `companion-updater` wraps every Companion upgrade with a pre/post
snapshot. If any of `connections`, `pages_with_content`, `buttons`, or
`triggers` decreases between snapshots, the updater automatically imports
the pre-upgrade snapshot back via Companion's tRPC import API and surfaces
the rollback in the dashboard.

This is the gate that would have caught the v4.2 → v4.3 silent button drop
on 2026-04-29.

Pre-upgrade snapshots are kept at `/var/lib/companion-updater/pre-upgrade-archive/`
for 7 days as an audit trail.

**Known limitation:** the gate only catches count *decreases*. A migration
that swaps button content while preserving the same count, or corrupts
existing buttons in place, will not trigger a rollback. For that class of
issue the off-machine GitHub backups are the recovery mechanism — import a
known-good `.companionconfig` from `companion-snv/history/` (or
`companion-pp/history/`) via the dashboard's Import / Export page.

## License

MIT
