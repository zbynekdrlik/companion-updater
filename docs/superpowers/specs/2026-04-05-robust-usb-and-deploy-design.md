# Robust USB Hot-Detect & Deploy Script

**Date:** 2026-04-05
**Status:** Approved

## Problem

Bitfocus Companion running in Docker on `companion.lan` loses Stream Deck connectivity when the device is plugged into a different USB port. Root causes:

1. **Wrong cgroup device major** — docker-compose specifies `c 241:* rwm` for hidraw, but the kernel uses major **240**. Container cannot access hidraw devices through cgroup rules.
2. **No hot-detect** — When Stream Deck moves to a new USB port, it creates a new `/dev/hidraw*` node. The container has no mechanism to pick it up without restart.
3. **Ghost USB group** — Compose adds group 983 but it doesn't exist on the host. Works only because udev sets `MODE=0666`.
4. **No deployment automation** — Changes to the repo require manual SSH and file copying.

## Solution

### 1. Privileged Container Mode

Replace `group_add` + `device_cgroup_rules` with `privileged: true` in `companion/docker-compose.yml`.

**Why privileged:** The container already mounts `/dev:/dev:rslave` (full device tree with slave propagation). The only barrier is cgroup device filtering, which is currently misconfigured. Privileged mode removes this barrier entirely, allowing immediate access to any new USB device on any port without container restart.

**What to remove:**
- `group_add` section
- `device_cgroup_rules` section

**What to add:**
- `privileged: true`

**What stays unchanged:**
- `/dev:/dev:rslave` volume (device node propagation)
- `/run/udev:/run/udev:ro` volume (udev metadata access)
- All other volumes, environment, healthcheck, resource limits

### 2. Entrypoint Cleanup

Remove from `companion/entrypoint.sh`:
- `groupadd -g 983 companionusb` — no longer needed
- `usermod -a -G 983 companion` — no longer needed

Keep unchanged:
- dbus-daemon setup
- avahi-daemon setup
- Cloudflare tunnel startup
- npm module dependency install
- Final exec to `/docker-entrypoint.sh`

### 3. Host Udev Rules (tracked in repo)

Create `host/` directory in repo root with:

**`host/50-elgato.rules`:**
```
# Elgato Stream Deck devices — allow access for all users
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"
```

**`host/99-nldevicessetup.rules`:**
```
# NL Devices Setup - Disable USB autosuspend
ACTION=="add", SUBSYSTEM=="usb", ATTR{power/autosuspend}="-1"
ACTION=="add", SUBSYSTEM=="usb", ATTR{power/control}="on"
```

These are already present on the production host but not tracked in version control. Storing them in the repo ensures consistency and reproducibility.

### 4. Deploy Script (`deploy.sh`)

A bash script in the repo root that deploys everything to `companion.lan` via SSH.

**Configuration** (environment variables with defaults):
- `COMPANION_HOST` — target hostname (default: `companion.lan`)
- `COMPANION_USER` — SSH user (default: `newlevel`)
- `COMPANION_PASS` — SSH password (default: `newlevel`)

**Steps:**
1. Validate connection to target host
2. Copy `companion/` files to `/opt/companion-docker/` on target
3. Copy `updater/` files to `/opt/companion-updater/` on target
4. Install udev rules from `host/*.rules` to `/etc/udev/rules.d/` (via sudo)
5. Reload udev rules: `sudo udevadm control --reload-rules && sudo udevadm trigger`
6. Build and start companion container: `cd /opt/companion-docker && docker compose up -d --build`
7. Build and start updater container: `cd /opt/companion-updater && docker compose up -d --build`
8. Wait for health checks to pass
9. Print status summary

**Error handling:** `set -euo pipefail`, exit on any failure with clear message.

**Uses `sshpass`** for password-based SSH (matches current access method).

### 5. `.env.example` Update

Remove `COMPANION_USB_GID=983` (no longer used with privileged mode).
Keep `CLOUDFLARE_TUNNEL_TOKEN=`.

### 6. File Structure After Changes

```
companion/
├── companion/
│   ├── Dockerfile              (unchanged)
│   ├── docker-compose.yml      (privileged: true, remove group_add/cgroup)
│   ├── entrypoint.sh           (remove USB group setup)
│   └── .env.example            (remove COMPANION_USB_GID)
├── updater/                    (unchanged)
├── host/
│   ├── 50-elgato.rules         (new — tracked udev rule)
│   └── 99-nldevicessetup.rules (new — tracked udev rule)
├── deploy.sh                   (new — SSH deploy script)
├── setup.sh                    (unchanged — local setup)
├── README.md                   (update with deploy instructions)
└── .gitignore                  (unchanged)
```

## Non-Goals

- CI/CD pipeline (user chose manual SSH deploy)
- Auto-restart on USB change (privileged + rslave gives hot-detect)
- Stream Deck configuration or button mapping (out of scope)
