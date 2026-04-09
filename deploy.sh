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
