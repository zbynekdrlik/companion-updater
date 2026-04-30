#!/bin/bash
set -euo pipefail

# One-time setup of the Companion backup pusher on a single host.
# Must be run AS ROOT on the target host.
#
# Steps:
#   1. Generate a deploy keypair at /root/.ssh/companion_backup_id_ed25519.
#   2. Print the public key for manual addition to the GitHub repo's
#      Deploy Keys (read+write).
#   3. Wait for confirmation, then clone the repo.
#   4. Write /etc/default/companion-backup-push with the MACHINE identity.
#   5. Enable + start the timer.

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

REPO_URL="git@github.com:zbynekdrlik/companion-backups.git"
KEY_PATH="/root/.ssh/companion_backup_id_ed25519"
REPO_DIR="/var/lib/companion-backup/repo"
ENV_FILE="/etc/default/companion-backup-push"
HOSTNAME_LOWER="$(hostname | tr '[:upper:]' '[:lower:]')"

# Determine MACHINE identity from hostname.
MACHINE="${MACHINE:-}"
if [ -z "${MACHINE}" ]; then
  case "${HOSTNAME_LOWER}" in
    companion-snv*|*-snv|snv*) MACHINE="companion-snv" ;;
    companion-pp*|linux-pp*|*-pp|pp*) MACHINE="companion-pp" ;;
    *)
      echo "ERROR: cannot determine MACHINE from hostname '${HOSTNAME_LOWER}'." >&2
      echo "       Override by exporting MACHINE before running this script." >&2
      exit 1
      ;;
  esac
fi
echo "MACHINE = ${MACHINE}"

# Generate keypair if not present.
if [ ! -f "${KEY_PATH}" ]; then
  install -d -m 0700 /root/.ssh
  ssh-keygen -t ed25519 -N '' -f "${KEY_PATH}" -C "companion-backup-push@$(hostname)"
  echo
  echo "=== Public key (add as Deploy Key with WRITE access on the backup repo) ==="
  cat "${KEY_PATH}.pub"
  echo "==========================================================================="
  echo "Visit: https://github.com/zbynekdrlik/companion-backups/settings/keys"
  echo
  read -rp "Press ENTER once the key is added on GitHub..." _
fi

# Clone the repo.
install -d -m 0700 /var/lib/companion-backup
if [ ! -d "${REPO_DIR}/.git" ]; then
  GIT_SSH_COMMAND="ssh -i ${KEY_PATH} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes" \
    git clone "${REPO_URL}" "${REPO_DIR}"
  git -C "${REPO_DIR}" config core.sshCommand \
    "ssh -i ${KEY_PATH} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes"
  git -C "${REPO_DIR}" config user.name "companion-backup-push"
  git -C "${REPO_DIR}" config user.email "companion-backup-push@$(hostname).local"
fi

# Write the environment file.
cat > "${ENV_FILE}" <<EOF
MACHINE=${MACHINE}
EOF
chmod 0644 "${ENV_FILE}"

# Reload systemd if the units are already in place; enable + start timer.
systemctl daemon-reload
if systemctl list-unit-files companion-backup-push.timer --no-legend | grep -q '.'; then
  systemctl enable --now companion-backup-push.timer
  systemctl status companion-backup-push.timer --no-pager | head -10
else
  echo "WARN: companion-backup-push.timer not yet installed; deploy via deploy.sh first."
fi

echo "Setup complete for ${MACHINE}."
