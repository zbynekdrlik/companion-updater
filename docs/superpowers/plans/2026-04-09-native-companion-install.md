# Native Companion Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate Companion from Docker to a native systemd service on companion.lan so USB hot-plug detection works.

**Architecture:** Use the official companion-pi installer to set up Companion as a systemd service. Migrate Cloudflare tunnel to a native systemd service. Update the repo's deploy script for the new architecture. Keep companion-updater in Docker.

**Tech Stack:** companion-pi, systemd, cloudflared, sshpass/SSH, Bash

**Spec:** `docs/superpowers/specs/2026-04-09-native-companion-install-design.md`

**SSH access:** `sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan`

---

### Task 1: Back up existing config on companion.lan

**Why:** The companion-pi installer downloads a fresh Companion tarball to `/opt/companion`. We must protect the existing config (`v4.1` directory) and the Cloudflare tunnel token.

- [ ] **Step 1: Back up Companion config**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo cp -a /opt/companion/v4.1 /opt/companion-config-backup-v4.1"
```

- [ ] **Step 2: Save the Cloudflare tunnel token**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "cat /opt/companion-docker/.env"
```

Save the `CLOUDFLARE_TUNNEL_TOKEN=...` value — it will be needed in Task 5.

- [ ] **Step 3: Verify backup exists**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "ls -la /opt/companion-config-backup-v4.1/"
```

Expected: directory listing with `db.sqlite`, `modules/`, `surfaces/`, `backups/`, etc.

---

### Task 2: Stop Docker Companion

**Why:** Free port 8000 before starting the native service. Keep Docker setup intact for rollback.

- [ ] **Step 1: Stop the Docker Companion container (NOT the updater)**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "cd /opt/companion-docker && docker compose down"
```

- [ ] **Step 2: Verify port 8000 is free**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "ss -tlnp | grep 8000 || echo 'Port 8000 is free'"
```

Expected: `Port 8000 is free`

- [ ] **Step 3: Verify updater is still running**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "docker ps --format '{{.Names}} {{.Status}}' | grep updater"
```

Expected: `companion-updater Up ...`

---

### Task 3: Install companion-pi

**Why:** This is the official way to run Companion as a native systemd service on Linux.

- [ ] **Step 1: Run the companion-pi installer**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "curl -s https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash"
```

This will:
- Install `libusb-1.0-0-dev libudev-dev libfontconfig1`
- Create `companion` system user
- Install fnm to `/opt/fnm`
- Clone companion-pi to `/usr/local/src/companionpi`
- Download Companion tarball to `/opt/companion`
- Enable and start `companion.service`

Wait for the script to complete (may take a few minutes).

- [ ] **Step 2: Verify the service is installed**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "systemctl status companion --no-pager"
```

Expected: `Active: active (running)` or similar.

- [ ] **Step 3: Verify config was preserved**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "ls /opt/companion/v4.1/db.sqlite && echo 'Config preserved'"
```

Expected: `Config preserved`

If config was overwritten, restore from backup:
```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo systemctl stop companion && sudo cp -a /opt/companion-config-backup-v4.1 /opt/companion/v4.1 && sudo systemctl start companion"
```

- [ ] **Step 4: Fix config ownership**

The service runs as the `companion` user. Ensure it owns the config:

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo chown -R companion:companion /opt/companion/v4.1"
```

---

### Task 4: Verify native Companion works

- [ ] **Step 1: Check web UI is accessible**

```bash
curl -s --max-time 10 -o /dev/null -w "%{http_code}" http://companion.lan:8000/
```

Expected: `200`

- [ ] **Step 2: Check udev rules are installed**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "ls /etc/udev/rules.d/50-companion*.rules /etc/udev/rules.d/99-nldevicessetup.rules 2>/dev/null"
```

Expected: at least `50-companion.rules` or `50-companion-headless.rules`, plus `99-nldevicessetup.rules`.

If the companion-pi rules are missing, install our custom ones:

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo cp /etc/udev/rules.d/50-elgato.rules /etc/udev/rules.d/50-elgato.rules.bak 2>/dev/null; sudo udevadm control --reload-rules && sudo udevadm trigger"
```

- [ ] **Step 3: Check Stream Deck is detected**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "lsusb | grep 0fd9"
```

Expected: `Elgato` device listed.

- [ ] **Step 4: Check Companion logs for surface detection**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo journalctl -u companion --no-pager --since '5 minutes ago' | grep -i -E 'surface|stream|deck|hid|usb' | tail -20"
```

Expected: Log lines showing the Stream Deck was detected (e.g., `Elgato Stream Deck MK.2 connected`).

- [ ] **Step 5: Verify mDNS is working**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "systemctl is-active avahi-daemon"
```

Expected: `active`

If not running:
```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo apt-get install -y avahi-daemon libnss-mdns && sudo systemctl enable --now avahi-daemon"
```

- [ ] **Step 6: Test MCP connectivity**

Use the companion-snv MCP tool to verify the API is working:

```
mcp__companion-snv__list_connections
```

Expected: Returns the list of connections (propresenter, OBS instances, etc.) — same as before migration.

---

### Task 5: Install Cloudflare tunnel natively

**Why:** The tunnel previously ran inside the Docker container. Now it needs to run as a host-level systemd service.

- [ ] **Step 1: Install cloudflared on the host**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo mkdir -p --mode=0755 /usr/share/keyrings && \
   sudo curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg && \
   echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list && \
   sudo apt-get update && sudo apt-get install -y cloudflared"
```

- [ ] **Step 2: Install as systemd service with the tunnel token**

Use the tunnel token saved in Task 1, Step 2:

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "sudo cloudflared service install <TUNNEL_TOKEN>"
```

Replace `<TUNNEL_TOKEN>` with the actual token from `/opt/companion-docker/.env`.

- [ ] **Step 3: Verify tunnel is running**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion.lan \
  "systemctl status cloudflared --no-pager | head -10"
```

Expected: `Active: active (running)`

- [ ] **Step 4: Verify external access**

```bash
curl -s --max-time 10 -o /dev/null -w "%{http_code}" https://companion.newlevel.media/
```

Expected: `302` or `200` (redirects to Cloudflare Access login or shows the UI).

---

### Task 6: Update deploy.sh

**Files:**
- Modify: `deploy.sh`

The deploy script must be rewritten for the new native architecture:
- Companion runs as a systemd service, not Docker
- Cloudflare tunnel runs as a systemd service
- companion-updater stays in Docker
- Config lives at `/opt/companion/v4.1` (managed by companion-pi)
- Updates use `companion-update` CLI tool

- [ ] **Step 1: Rewrite deploy.sh**

Replace the entire `deploy.sh` with:

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
#   - Update Dashboard: Docker container (companion-updater)

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
echo "[1/6] Testing connection to ${COMPANION_HOST}..."
if ! remote "echo ok" >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to ${COMPANION_USER}@${COMPANION_HOST}"
  echo "Check that the host is reachable and credentials are correct."
  exit 1
fi
echo "  Connected."

# Step 2: Install udev rules
echo "[2/6] Installing udev rules..."
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
echo "[3/6] Updating Companion..."
if remote "command -v companion-update >/dev/null 2>&1"; then
  remote "sudo companion-update"
  echo "  Companion updated."
else
  echo "  companion-update not found — companion-pi may not be installed."
  echo "  Install with: curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash"
  exit 1
fi

# Step 4: Restart Companion service
echo "[4/6] Restarting Companion service..."
remote "sudo systemctl restart companion"
echo "  Companion service restarted."

# Step 5: Copy and restart updater (Docker)
echo "[5/6] Updating companion-updater..."
remote "sudo mkdir -p /opt/companion-updater"
remote "sudo chown ${COMPANION_USER}:${COMPANION_USER} /opt/companion-updater"
remote_copy "${SCRIPT_DIR}/updater/"* "${COMPANION_USER}@${COMPANION_HOST}:/opt/companion-updater/"
remote "cd /opt/companion-updater && docker compose up -d --build"
echo "  Updater container started."

# Step 6: Health check
echo "[6/6] Waiting for Companion to be ready..."
MAX_WAIT=60
ELAPSED=0
while [ "${ELAPSED}" -lt "${MAX_WAIT}" ]; do
  if curl -sf --max-time 5 "http://${COMPANION_HOST}:8000/" >/dev/null 2>&1; then
    break
  fi
  sleep 5
  ELAPSED=$((ELAPSED + 5))
  echo "  Waiting... (${ELAPSED}s)"
done

if ! curl -sf --max-time 5 "http://${COMPANION_HOST}:8000/" >/dev/null 2>&1; then
  echo ""
  echo "WARNING: Companion did not become ready within ${MAX_WAIT}s"
  echo "  Check logs: ssh ${COMPANION_USER}@${COMPANION_HOST} sudo journalctl -u companion --no-pager -n 50"
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

- [ ] **Step 2: Validate syntax**

```bash
bash -n deploy.sh
```

Expected: no output (clean parse).

- [ ] **Step 3: Commit**

```bash
git add deploy.sh
git commit -m "Update deploy.sh for native Companion systemd service

Replace Docker-based companion deployment with native systemd service
management. Uses companion-update CLI for updates and systemctl for
service control. Updater dashboard stays in Docker."
```

---

### Task 7: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite README for native architecture**

The README currently describes a Docker-only setup. Update it to reflect the new native Companion + Docker updater architecture. Key changes:

- Title: "Bitfocus Companion Setup" (not "Docker Setup")
- Features: native systemd service, USB hot-plug, Cloudflare tunnel
- Quick Start: uses companion-pi installer
- Deploy section: updated for systemd
- Directory structure: updated
- Troubleshooting: systemd commands instead of Docker

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Update README for native Companion architecture

Reflect migration from Docker to native systemd service via companion-pi.
Update quick start, deploy instructions, troubleshooting, and directory
structure."
```

---

### Task 8: Push and create PR

- [ ] **Step 1: Push to dev**

```bash
git push origin dev
```

- [ ] **Step 2: Monitor CI (if applicable)**

```bash
gh run list --branch dev --limit 3
```

- [ ] **Step 3: Create PR**

```bash
gh pr create --base main --head dev --title "Migrate Companion from Docker to native systemd service" --body "$(cat <<'EOF'
## Summary
- Migrate Companion from Docker container to native systemd service via companion-pi
- Root cause fix: Docker cannot forward udev hot-plug events, so Stream Deck was not detected when moved to a different USB port
- Native Companion with libudev receives USB events directly — hot-plug just works
- Cloudflare tunnel migrated to native systemd service
- Deploy script updated for systemd architecture
- companion-updater stays in Docker (no USB dependency)

## Test plan
- [ ] Verify Companion web UI accessible on port 8000
- [ ] Verify Stream Deck detected after hot-plug to different USB port
- [ ] Verify Cloudflare tunnel working (companion.newlevel.media)
- [ ] Verify mDNS resolution (companion.lan)
- [ ] Verify companion-updater still works on port 8081

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify PR is mergeable**

```bash
gh api repos/zbynekdrlik/companion-updater/pulls/NUMBER --jq '{mergeable: .mergeable, mergeable_state: .mergeable_state}'
```

Wait for user approval before merging.
