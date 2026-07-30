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

# Step 3: Install backup pusher (script + systemd units; deploy key set up separately)
echo "[3/7] Installing backup pusher..."
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.sh"
remote "sudo install -m 0755 /tmp/companion-backup-push.sh /usr/local/bin/companion-backup-push.sh && rm /tmp/companion-backup-push.sh"
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.service" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.service"
remote "sudo install -m 0644 /tmp/companion-backup-push.service /etc/systemd/system/companion-backup-push.service && rm /tmp/companion-backup-push.service"
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.timer" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.timer"
remote "sudo install -m 0644 /tmp/companion-backup-push.timer /etc/systemd/system/companion-backup-push.timer && rm /tmp/companion-backup-push.timer"
remote_copy "${SCRIPT_DIR}/host/setup-backup-key.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/setup-backup-key.sh"
remote "sudo install -m 0755 /tmp/setup-backup-key.sh /usr/local/sbin/setup-backup-key.sh && rm /tmp/setup-backup-key.sh"
remote "sudo systemctl daemon-reload"
# Enable timer only if /etc/default/companion-backup-push exists (i.e., setup-backup-key.sh has been run).
if remote "test -f /etc/default/companion-backup-push"; then
  remote "sudo systemctl enable --now companion-backup-push.timer"
  echo "  Backup pusher enabled."
else
  echo "  Backup pusher installed but NOT yet enabled."
  echo "  Run on the host:    sudo /usr/local/sbin/setup-backup-key.sh"
  echo "  Then re-run this deploy or:  sudo systemctl enable --now companion-backup-push.timer"
fi

# Step 4: Build companion-updater binary locally
echo "[4/7] Building companion-updater..."
"${SCRIPT_DIR}/updater/build.sh"
BIN="${SCRIPT_DIR}/updater/target/release/companion-updater"
if [ ! -x "${BIN}" ]; then
  echo "ERROR: ${BIN} not found"
  exit 1
fi

# Step 5: Deploy companion-updater binary and systemd unit
#
# The binary is deployed BEFORE the Companion upgrade is triggered, so the
# upgrade always runs through the code in THIS checkout. Doing it the other way
# round meant a deploy could never fix a bug in the upgrade path itself — the
# old binary ran the upgrade, and the fix only took effect a deploy later.
echo "[5/7] Deploying companion-updater..."
remote "sudo systemctl stop companion-updater 2>/dev/null || true"
remote_copy "${BIN}" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-updater"
remote "sudo install -m 0755 /tmp/companion-updater /usr/local/bin/companion-updater && rm /tmp/companion-updater"
remote_copy "${SCRIPT_DIR}/updater/companion-updater.service" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-updater.service"
remote "sudo install -m 0644 /tmp/companion-updater.service /etc/systemd/system/companion-updater.service && rm /tmp/companion-updater.service"
remote "sudo systemctl daemon-reload && sudo systemctl enable --now companion-updater"
echo "  companion-updater deployed."

# Wait for the freshly started updater to serve before driving it.
for _ in $(seq 1 12); do
  if remote "curl -sf --max-time 3 http://127.0.0.1:8081/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

# Step 6: Update Companion through the safety-gated updater endpoint.
echo "[6/7] Updating Companion via companion-updater (safety-gated)..."
# Stream the SSE update endpoint. We grep for the first terminal event in the
# stream: complete = success, safety_rollback = data loss + auto-reverted,
# error = anything else. The first match wins; we then stop reading.
#
# `|| true` swallows the pipeline's exit code: an empty TERMINAL_LINE (curl
# failure, no terminal event seen, etc.) falls through to the *) case below
# which prints a clear error. Set -e would otherwise abort the deploy with
# no diagnostic.
TERMINAL_LINE="$(remote "curl -fsS --no-buffer --max-time 1800 -H 'Accept: text/event-stream' http://127.0.0.1:8081/api/update/stream 2>&1 | grep -m1 -E '\"type\":\"(complete|error|safety_rollback)\"'" || true)"
echo "  ${TERMINAL_LINE}"
case "${TERMINAL_LINE}" in
  *'"type":"complete"'*)
    echo "  Companion updated successfully."
    ;;
  *'"type":"safety_rollback"'*)
    echo "  ERROR: data loss detected during upgrade; updater rolled back."
    exit 1
    ;;
  *'"type":"error"'*)
    echo "  ERROR: upgrade failed (see updater journal: sudo journalctl -u companion-updater -n 100)."
    exit 1
    ;;
  *)
    echo "  ERROR: did not receive a terminal event from updater."
    exit 1
    ;;
esac

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
