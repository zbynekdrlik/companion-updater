#!/bin/bash
set -euo pipefail

# Deploy companion stack to remote host via SSH
# Usage: COMPANION_PASS=... ./deploy.sh
# Configuration via environment variables (see defaults below)
#
# Architecture:
#   - Companion: native systemd service (companion-pi)
#   - Web access: cloudflared-companion.service (own tunnel, behind Cloudflare Access;
#     cloudflared.service on the rigs is the presenter project's tunnel)
#   - Update Dashboard: native systemd service (companion-updater Rust binary)

COMPANION_HOST="${COMPANION_HOST:-companion.lan}"
COMPANION_USER="${COMPANION_USER:-newlevel}"
COMPANION_PASS="${COMPANION_PASS:?set COMPANION_PASS to the SSH password of ${COMPANION_USER}@${COMPANION_HOST}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

# sshpass -e reads SSHPASS, so the password never appears on a command line (ps).
export SSHPASS="${COMPANION_PASS}"

remote() {
  sshpass -e ssh ${SSH_OPTS} "${COMPANION_USER}@${COMPANION_HOST}" "$@"
}

remote_copy() {
  sshpass -e scp ${SSH_OPTS} -r "$@"
}

echo "=== Deploying to ${COMPANION_HOST} ==="
echo ""

# Step 1: Validate connection
echo "[1/9] Testing connection to ${COMPANION_HOST}..."
if ! remote "echo ok" >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to ${COMPANION_USER}@${COMPANION_HOST}"
  exit 1
fi
echo "  Connected."

# Step 2: Install udev rules
echo "[2/9] Installing udev rules..."
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

# Step 3: Enable NTP time sync (companion-updater#19: timesyncd was disabled and
# the clock drifted). Fails the deploy when the clock does not synchronise.
echo "[3/9] Enabling time sync..."
remote_copy "${SCRIPT_DIR}/host/ensure-timesync.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/ensure-timesync.sh"
remote "sudo install -m 0755 /tmp/ensure-timesync.sh /usr/local/sbin/ensure-timesync.sh && rm /tmp/ensure-timesync.sh"
remote "sudo /usr/local/sbin/ensure-timesync.sh"

# Step 4: Install backup pusher (script + systemd units; deploy key set up separately)
echo "[4/9] Installing backup pusher..."
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

# Step 5: Build companion-updater binary locally
echo "[5/9] Building companion-updater..."
"${SCRIPT_DIR}/updater/build.sh"
BIN="${SCRIPT_DIR}/updater/target/release/companion-updater"
if [ ! -x "${BIN}" ]; then
  echo "ERROR: ${BIN} not found"
  exit 1
fi

# Step 6: Deploy companion-updater binary and systemd unit
#
# The binary is deployed BEFORE the Companion upgrade is triggered, so the
# upgrade always runs through the code in THIS checkout. Doing it the other way
# round meant a deploy could never fix a bug in the upgrade path itself — the
# old binary ran the upgrade, and the fix only took effect a deploy later.
echo "[6/9] Deploying companion-updater..."
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

# Step 7: Companion web UI tunnel (companion-updater#26). Its own unit next to
# the presenter's cloudflared.service. It comes AFTER the updater install, so
# the public update dashboard is never served by an updater without the
# cross-site guard. A rig without the tunnel token fails the
# deploy (that silent state is how the tunnels sat dead for weeks); a deliberate
# deploy without web access sets COMPANION_TUNNEL=skip.
echo "[7/9] Installing the Companion web UI tunnel..."
remote_copy "${SCRIPT_DIR}/host/cloudflared-companion.service" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/cloudflared-companion.service"
remote "sudo install -m 0644 /tmp/cloudflared-companion.service /etc/systemd/system/cloudflared-companion.service && rm /tmp/cloudflared-companion.service"
remote_copy "${SCRIPT_DIR}/host/cloudflared-companion.yml" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/cloudflared-companion.yml"
remote "sudo install -m 0644 /tmp/cloudflared-companion.yml /etc/cloudflared-companion.yml && rm /tmp/cloudflared-companion.yml"
remote "sudo systemctl daemon-reload"
RIG="$(remote hostname)"
# Every hostname the rig's tunnel serves (keep in sync with TUNNELS in
# host/cloudflare_companion.py); each must answer with the Access login.
# UI_HOSTS are the ones serving the Companion UI itself.
case "${RIG}" in
  companion-snv)
    PUBLIC_HOSTS="companion-snv.newlevel.media companion-update-snv.newlevel.media companion.newlevel.media"
    UI_HOSTS="companion-snv.newlevel.media companion.newlevel.media" ;;
  linux-pp)
    PUBLIC_HOSTS="companion-pp.newlevel.media companion-update-pp.newlevel.media"
    UI_HOSTS="companion-pp.newlevel.media" ;;
  *) PUBLIC_HOSTS=""; UI_HOSTS="" ;;
esac
remote_copy "${SCRIPT_DIR}/host/ensure-companion-proxy.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/ensure-companion-proxy.sh"
remote "sudo install -m 0755 /tmp/ensure-companion-proxy.sh /usr/local/sbin/ensure-companion-proxy.sh && rm /tmp/ensure-companion-proxy.sh"
if [ "${COMPANION_TUNNEL:-}" = "skip" ]; then
  echo "  COMPANION_TUNNEL=skip: unit installed, not started."
elif [ -z "${PUBLIC_HOSTS}" ]; then
  echo "ERROR: ${COMPANION_HOST} is '${RIG}', which has no web hostname (add it to deploy.sh and host/cloudflare_companion.py, or set COMPANION_TUNNEL=skip)"
  exit 1
elif ! remote "sudo test -f /etc/cloudflared-companion.env"; then
  echo "ERROR: no tunnel token on ${RIG}. Run on dev1:"
  echo "  COMPANION_ACCESS_EMAILS=<owner e-mail> COMPANION_PASS=... host/cloudflare_companion.py install-token ${COMPANION_HOST}"
  echo "  (or deploy without web access: COMPANION_TUNNEL=skip)"
  exit 1
else
  # Companion must accept the UI's WebSocket from the tunnel, or the page loads
  # and never connects (restarts Companion only when it changes the setting).
  if ! remote "sudo /usr/local/sbin/ensure-companion-proxy.sh ${UI_HOSTS}"; then
    echo "ERROR: Companion on ${RIG} would not accept the web UI connection from the tunnel (see above)."
    echo "  A rig without /etc/companion/config.yaml needs the Companion upgrade first: COMPANION_TUNNEL=skip ./deploy.sh, then deploy again."
    exit 1
  fi
  remote "sudo systemctl enable cloudflared-companion" >/dev/null
  if ! remote "sudo systemctl restart cloudflared-companion"; then
    echo "ERROR: cloudflared-companion did not start on ${RIG} (sudo journalctl -u cloudflared-companion -n 50)"
    exit 1
  fi
  sleep 5   # a crash right after READY shows up here, not in the restart itself
  if ! remote "systemctl is-active --quiet cloudflared-companion"; then
    echo "ERROR: cloudflared-companion died after starting on ${RIG} (sudo journalctl -u cloudflared-companion -n 50)"
    exit 1
  fi
  # Every public URL must answer with the Access login. A 200 means the rig is
  # reachable without login: stop AND disable the tunnel (a reboot must not
  # publish it again).
  for host in ${PUBLIC_HOSTS}; do
    answer="$(curl -s -o /dev/null -m 20 -w '%{http_code} %{redirect_url}' "https://${host}/" || true)"
    case "${answer}" in
      302\ https://newlevelchurch.cloudflareaccess.com/*) echo "  https://${host} asks for the Access login." ;;
      200\ *)
        remote "sudo systemctl disable --now cloudflared-companion"
        echo "ERROR: https://${host} answered 200 WITHOUT login; tunnel stopped and disabled. Run host/cloudflare_companion.py plan."
        exit 1 ;;
      *)
        echo "ERROR: https://${host} answered '${answer}', not the Access login (host/cloudflare_companion.py status / plan)"
        exit 1 ;;
    esac
  done
  echo "  cloudflared-companion running."
fi

# Step 8: Update Companion through the safety-gated updater endpoint.
# COMPANION_UPGRADE=skip deploys this repo's tooling without upgrading Companion
# itself (the owner upgrades Companion from the dashboard when he wants to).
echo "[8/9] Updating Companion via companion-updater (safety-gated)..."
if [ "${COMPANION_UPGRADE:-}" = "skip" ]; then
  echo "  COMPANION_UPGRADE=skip: Companion stays at its current version."
else
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
fi

# Step 9: Health check
echo "[9/9] Waiting for services to be ready..."
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

# The step-8 upgrade must not have broken the web UI behind a running tunnel.
if [ "${COMPANION_UPGRADE:-}" != "skip" ] && [ -n "${UI_HOSTS}" ] \
   && remote "systemctl is-active --quiet cloudflared-companion"; then
  remote "sudo /usr/local/sbin/ensure-companion-proxy.sh ${UI_HOSTS}"
fi

HOST_IP=$(remote "hostname -I | awk '{print \$1}'" 2>/dev/null || echo "${COMPANION_HOST}")

echo ""
echo "=== Deploy Complete ==="
echo ""
echo "  Companion:        http://${HOST_IP}:8000"
echo "  Update Dashboard: http://${HOST_IP}:8081"
echo ""
