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
