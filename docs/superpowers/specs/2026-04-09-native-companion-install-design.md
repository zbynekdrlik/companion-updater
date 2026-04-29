# Native Companion Install — Migrate from Docker to systemd

## Problem

The Stream Deck fails to be detected when plugged into a different USB port. Root cause: Bitfocus Companion running inside Docker has no mechanism to receive udev hot-plug events. The container's `privileged: true` and `/dev:/dev:rslave` mount give ACCESS to devices but don't trigger Companion's USB rescan when new `hidraw` devices appear.

This is a fundamental limitation of running Companion in Docker for USB-dependent workflows.

## Goal

Replace the Docker-based Companion setup with a native systemd service using the official companion-pi installer. This gives Companion direct access to udev events, making USB hot-plug detection work natively.

## Scope

- **In scope:** companion.lan migration, deploy script update, Cloudflare tunnel migration
- **Out of scope:** companion-pp.lan (will be migrated separately after companion.lan is validated)
- **Out of scope:** companion-updater dashboard (stays in Docker — it has no USB dependency)

## Current State (companion.lan)

- OS: Ubuntu 24.04.2 LTS (Noble Numbat), x86_64, kernel 6.17
- Companion: runs in Docker (`privileged: true`, `network_mode: host`)
- Custom Dockerfile adds: mDNS (avahi), Cloudflare tunnel (cloudflared), timezone, npm module installer
- Config data: `/opt/companion/v4.1` (SQLite DB, modules, surfaces, backups)
- Cloudflare tunnel: cloudflared runs inside the container, token in `/opt/companion-docker/.env`
- companion-updater: separate Docker container on port 8081

## Target State

- Companion: native systemd service via companion-pi (`companion.service`)
- Cloudflare tunnel: native systemd service (`cloudflared.service`)
- mDNS: Ubuntu's native avahi-daemon (already available)
- Config data: same `/opt/companion/v4.1` path (no migration needed)
- companion-updater: stays in Docker on port 8081
- USB hot-plug: works natively via node-hid + libudev

## Design

### Step 1: Install companion-pi

Run the official installer on the host:

```bash
curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | bash
```

This will:
- Install dependencies: `libusb-1.0-0-dev libudev-dev libfontconfig1`
- Create `companion` system user
- Install fnm (Fast Node Manager) to `/opt/fnm`
- Clone companion-pi to `/usr/local/src/companionpi`
- Download Companion tarball to `/opt/companion`
- Install and enable `companion.service`

**Config preservation:** The installer downloads a fresh Companion to `/opt/companion`. Our existing config lives at `/opt/companion/v4.1`. The installer should not overwrite config directories — but we must verify this and back up before running.

### Step 2: Configure Companion service

Ensure the systemd service points to the correct config directory. The default `COMPANION_CONFIG_BASEDIR` should be `/opt/companion` (Companion auto-detects version subdirs like `v4.1`).

The service runs as user `companion`. Ensure the `companion` user owns the config:

```bash
chown -R companion:companion /opt/companion/v4.1
```

### Step 3: Install udev rules

The companion-pi installer should handle this, but verify that `/etc/udev/rules.d/50-companion.rules` exists and covers Elgato devices. Keep our custom `99-nldevicessetup.rules` for USB autosuspend disable.

### Step 4: Stop Docker Companion

```bash
cd /opt/companion-docker && docker compose down
```

Do NOT remove the Docker setup yet — keep it as a rollback option until native is verified working.

### Step 5: Start native Companion

```bash
systemctl start companion
systemctl status companion
```

Verify:
- Web UI accessible on port 8000
- Stream Deck detected (check surfaces in UI)
- Unplug and re-plug Stream Deck to a different port — verify it's re-detected automatically

### Step 6: Migrate Cloudflare tunnel to host

Install cloudflared natively on the host (same method as in the Dockerfile):

```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' > /etc/apt/sources.list.d/cloudflared.list
apt-get update && apt-get install -y cloudflared
```

Configure as a systemd service:

```bash
cloudflared service install <TUNNEL_TOKEN>
systemctl enable cloudflared
systemctl start cloudflared
```

The tunnel token comes from `/opt/companion-docker/.env` (`CLOUDFLARE_TUNNEL_TOKEN`).

### Step 7: Update deploy script

Update `deploy.sh` to:
- Stop `companion.service` instead of `docker compose down`
- Copy files to `/opt/companion` instead of `/opt/companion-docker`
- Run `companion-update` or re-extract tarball for updates
- Start `companion.service` instead of `docker compose up`
- Keep Docker management for companion-updater only

### Step 8: Verify mDNS

Ubuntu 24.04 should have avahi-daemon available. Verify it's running:

```bash
systemctl status avahi-daemon
```

If not installed: `apt-get install -y avahi-daemon libnss-mdns`

## Rollback Plan

If native install fails:
1. `systemctl stop companion`
2. `cd /opt/companion-docker && docker compose up -d`
3. Docker setup is untouched and ready to restart

## Files Changed in Repo

- Modify: `deploy.sh` — update for systemd service management
- Modify: `README.md` — update installation/architecture docs
- Keep: `companion/` directory (Docker setup) — for reference/rollback, but no longer the primary deployment method
- Keep: `host/` udev rules — still deployed to the host
- Keep: `updater/` — companion-updater stays in Docker

## Verification

1. **USB hot-plug:** Unplug Stream Deck, plug into different port, confirm auto-detected (no restart needed)
2. **Web UI:** `http://companion.lan:8000` loads, all pages/buttons/connections intact
3. **Cloudflare tunnel:** `https://companion.newlevel.media` accessible with Access protection
4. **mDNS:** `companion.lan` resolves correctly
5. **Persistence:** Reboot host, confirm companion.service auto-starts and Stream Deck is detected
