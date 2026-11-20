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
# Optional env (tests): COMPANION_CONFIG_DIR, BACKUP_STATE_DIR.

STATE_DIR="${BACKUP_STATE_DIR:-/var/lib/companion-backup}"
REPO_DIR="${STATE_DIR}/repo"
HASH_FILE="${STATE_DIR}/last-pushed.sha256"
SSH_KEY="/root/.ssh/companion_backup_id_ed25519"
CONFIG_DIR="${COMPANION_CONFIG_DIR:-/home/companion/.config/companion-nodejs}"

if [ -z "${MACHINE:-}" ]; then
  echo "ERROR: MACHINE env var not set" >&2
  exit 1
fi

# 1. Find the most recent .companionconfig across all Companion versions
# (v4.*, v5.*, ...; #8: a v4-only glob missed every Companion 5 backup).
# awk keeps the newest line itself and reads all input, so nothing upstream
# gets SIGPIPE (`sort | head` under pipefail died with `sort: write error`).
shopt -s nullglob
BACKUP_DIRS=("${CONFIG_DIR}"/v*/backups)
shopt -u nullglob
LATEST=""
if [ "${#BACKUP_DIRS[@]}" -gt 0 ]; then
  LATEST="$(find "${BACKUP_DIRS[@]}" -maxdepth 1 -type f -name '*.companionconfig' -printf '%T@ %p\n' \
    | awk 'NR == 1 || $1 > best { best = $1; line = $0 } END { if (line != "") { sub(/^[^ ]+ /, "", line); print line } }')"
fi

if [ -z "${LATEST}" ] || [ ! -f "${LATEST}" ]; then
  echo "ERROR: no .companionconfig found under ${CONFIG_DIR}/v*/backups" >&2
  exit 1
fi
echo "Newest backup: ${LATEST}"

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
