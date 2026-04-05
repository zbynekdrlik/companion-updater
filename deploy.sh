#!/bin/bash
set -euo pipefail

# Deploy companion stack to remote host via SSH
# Usage: ./deploy.sh
# Configuration via environment variables (see defaults below)

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
echo "[1/8] Testing connection to ${COMPANION_HOST}..."
if ! remote "echo ok" >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to ${COMPANION_USER}@${COMPANION_HOST}"
  echo "Check that the host is reachable and credentials are correct."
  exit 1
fi
echo "  Connected."

# Step 2: Copy companion files
echo "[2/8] Copying companion files to /opt/companion-docker/..."
remote "sudo mkdir -p /opt/companion-docker /opt/companion /opt/companion-updater"
remote "sudo chown ${COMPANION_USER}:${COMPANION_USER} /opt/companion-docker /opt/companion /opt/companion-updater"
remote_copy "${SCRIPT_DIR}/companion/"* "${COMPANION_USER}@${COMPANION_HOST}:/opt/companion-docker/"
# Glob * misses dotfiles — copy .env.example explicitly
remote_copy "${SCRIPT_DIR}/companion/.env.example" "${COMPANION_USER}@${COMPANION_HOST}:/opt/companion-docker/"
echo "  Done."

# Step 3: Copy updater files
echo "[3/8] Copying updater files to /opt/companion-updater/..."
remote_copy "${SCRIPT_DIR}/updater/"* "${COMPANION_USER}@${COMPANION_HOST}:/opt/companion-updater/"
echo "  Done."

# Step 4: Create .env if it doesn't exist
echo "[4/8] Ensuring .env file exists..."
if remote "test -f /opt/companion-docker/.env"; then
  echo "  .env already exists, preserving."
else
  remote "cp /opt/companion-docker/.env.example /opt/companion-docker/.env"
  echo "  Created .env from .env.example."
  echo "  WARNING: Set CLOUDFLARE_TUNNEL_TOKEN in /opt/companion-docker/.env if you need the Cloudflare tunnel."
fi

# Step 5: Install udev rules
echo "[5/8] Installing udev rules..."
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

# Step 6: Build and start companion container
echo "[6/8] Building and starting companion container..."
remote "cd /opt/companion-docker && docker compose up -d --build"
echo "  Companion container started."

# Step 7: Build and start updater container
echo "[7/8] Building and starting updater container..."
remote "cd /opt/companion-updater && docker compose up -d --build"
echo "  Updater container started."

# Step 8: Wait for health checks
echo "[8/8] Waiting for health checks..."
MAX_WAIT=120
ELAPSED=0
HEALTH="unknown"
while [ "${ELAPSED}" -lt "${MAX_WAIT}" ]; do
  HEALTH=$(remote "docker inspect --format='{{.State.Health.Status}}' companion 2>/dev/null" || echo "unknown")
  if [ "${HEALTH}" = "healthy" ]; then
    break
  fi
  sleep 5
  ELAPSED=$((ELAPSED + 5))
  echo "  Waiting... (${ELAPSED}s, status: ${HEALTH})"
done

if [ "${HEALTH}" != "healthy" ]; then
  echo ""
  echo "WARNING: Companion container did not become healthy within ${MAX_WAIT}s"
  echo "  Current status: ${HEALTH}"
  echo "  Check logs: ssh ${COMPANION_USER}@${COMPANION_HOST} docker logs companion"
  exit 1
fi

HOST_IP=$(remote "hostname -I | awk '{print \$1}'" 2>/dev/null || echo "${COMPANION_HOST}")

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "  Companion:        http://${HOST_IP}:8000"
echo "  Update Dashboard: http://${HOST_IP}:8081"
echo "  Companion status: ${HEALTH}"
echo ""
