# Bitfocus Companion Setup

Production setup for [Bitfocus Companion](https://bitfocus.io/companion) with USB hot-plug support and remote access.

## Architecture

- **Companion**: Native systemd service via [companion-pi](https://github.com/bitfocus/companion-pi) — direct USB/udev access for reliable Stream Deck hot-plug detection
- **Cloudflare Tunnel**: Native systemd service for remote access without port forwarding
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
# Default: deploys to companion.lan as newlevel
./deploy.sh

# Override target host, user, or password
COMPANION_HOST=10.0.0.50 COMPANION_USER=admin COMPANION_PASS=secret ./deploy.sh
```

The deploy script:
1. Installs udev rules from `host/` to `/etc/udev/rules.d/`
2. Installs the backup pusher (script + systemd timer)
3. Builds the `companion-updater` Rust binary locally and installs it as a systemd service
4. Triggers the Companion upgrade through that freshly deployed updater
   (binary first, upgrade second — otherwise a fix to the upgrade path could
   never take effect in the deploy that ships it)
5. Waits for health checks to pass

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

### Cloudflare Tunnel

Install cloudflared natively on the host:

```bash
sudo cloudflared service install <TUNNEL_TOKEN>
```

Get the token from [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → Networks → Tunnels.

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
# Check tunnel service
sudo systemctl status cloudflared
# Check tunnel logs
sudo journalctl -u cloudflared --no-pager -n 50
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
