#!/bin/bash
set -euo pipefail

# Push the most recent Companion .companionconfig backup to the private
# GitHub backup repo. Runs hourly via systemd timer.
#
# Required state:
#   /var/lib/companion-backup/repo            — clone of the backup repo
#   /var/lib/companion-backup/last-pushed.sha256 — hash of last successful push
#   /root/.ssh/companion_backup_id_ed25519    — deploy key (read+write to the
#                                              backup repo only)
# Required env (provided by systemd unit):
#   MACHINE — "companion-snv" or "companion-pp"

REPO_DIR="/var/lib/companion-backup/repo"
HASH_FILE="/var/lib/companion-backup/last-pushed.sha256"
SSH_KEY="/root/.ssh/companion_backup_id_ed25519"
COMPANION_BACKUPS_GLOB="/home/companion/.config/companion-nodejs/v4.*/backups"

if [ -z "${MACHINE:-}" ]; then
  echo "ERROR: MACHINE env var not set" >&2
  exit 1
fi

# 1. Find the most recent .companionconfig across all version subdirs.
# shellcheck disable=SC2086 # Intentional glob expansion of v4.* version dirs.
LATEST="$(find ${COMPANION_BACKUPS_GLOB} -maxdepth 1 -name '*.companionconfig' -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr | head -n1 | awk '{print $2}')"

if [ -z "${LATEST}" ] || [ ! -f "${LATEST}" ]; then
  echo "ERROR: no .companionconfig found under ${COMPANION_BACKUPS_GLOB}" >&2
  exit 1
fi

# 2. Hash compare with last-pushed.
NEW_HASH="$(sha256sum "${LATEST}" | awk '{print $1}')"
LAST_HASH=""
[ -f "${HASH_FILE}" ] && LAST_HASH="$(cat "${HASH_FILE}")"

if [ "${NEW_HASH}" = "${LAST_HASH}" ]; then
  echo "Backup unchanged (${NEW_HASH:0:12}); skipping push."
  exit 0
fi

# 3. Sync the repo before staging.
export GIT_SSH_COMMAND="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes"
cd "${REPO_DIR}"
git pull --ff-only origin main

# 4. Stage the new file as both latest and a timestamped history entry.
mkdir -p "${MACHINE}/history"
cp "${LATEST}" "${MACHINE}/latest.companionconfig"
cp "${LATEST}" "${MACHINE}/history/$(basename "${LATEST}")"

# 5. Prune history older than 30 days.
find "${MACHINE}/history" -name '*.companionconfig' -mtime +30 -delete

# 6. Stage, commit, push.
git add "${MACHINE}"
if git diff --cached --quiet; then
  echo "No staged changes after copy (file already in tree); recording hash and exiting."
  echo "${NEW_HASH}" > "${HASH_FILE}"
  exit 0
fi

ISO="$(date -Iseconds)"
git commit -m "Hourly backup ${ISO} [${MACHINE}]"
git push origin main

echo "${NEW_HASH}" > "${HASH_FILE}"
echo "Pushed backup ${NEW_HASH:0:12} for ${MACHINE}."
