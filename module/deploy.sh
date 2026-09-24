#!/bin/bash
set -euo pipefail

# Deploy the resolume-simple Companion module to one rig.
# Usage: COMPANION_PASS=... module/deploy.sh <host>
#   hosts: companion.lan (snv), 100.101.72.101 (pp)
#
# Both rigs run Companion with --extra-module-path /opt/companion-module-dev,
# so the module is loaded from there with the stable version id "dev" and a
# connection never has to be re-pinned when the version changes. Companion
# only picks up a new or changed module on restart.
#
# Safety: Companion is restarted on every path (a remote EXIT trap), the
# previous module is kept until the new one is verified, and a failed
# verification rolls back to it.

HOST="${1:?usage: COMPANION_PASS=... module/deploy.sh <host>}"
COMPANION_USER="${COMPANION_USER:-newlevel}"
: "${COMPANION_PASS:?set COMPANION_PASS (SSH password of ${COMPANION_USER})}"
export SSHPASS="${COMPANION_PASS}"   # sshpass -e: keeps the password off the command line

MODULE_ID="resolume-simple"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/${MODULE_ID}"
DEST="/opt/companion-module-dev/${MODULE_ID}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

remote() {
  sshpass -e ssh "${SSH_OPTS[@]}" "${COMPANION_USER}@${HOST}" "$@"
}

# Deploy only committed, pushed code (the module and this script).
if [ -n "$(git -C "${SCRIPT_DIR}" status --porcelain -- "${SCRIPT_DIR}")" ]; then
  echo "ERROR: ${SCRIPT_DIR} has uncommitted changes — deploy only committed code" >&2
  exit 1
fi
if [ -z "$(git -C "${SCRIPT_DIR}" branch -r --contains HEAD)" ]; then
  echo "ERROR: HEAD $(git -C "${SCRIPT_DIR}" rev-parse --short HEAD) is not pushed to any remote branch" >&2
  exit 1
fi

VERSION="$(node -p "require('${SRC}/package.json').version")"
echo "=== Deploying ${MODULE_ID} v${VERSION} ($(git -C "${SCRIPT_DIR}" rev-parse --short HEAD)) to ${HOST} ==="

echo "[1/5] Building the package (production dependencies only, no tests)..."
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
PKG="${STAGE}/${MODULE_ID}"
mkdir -p "${PKG}"
cp "${SRC}/index.js" "${SRC}/package.json" "${SRC}/package-lock.json" "${PKG}/"
cp -r "${SRC}/companion" "${SRC}/lib" "${PKG}/"
find "${PKG}/lib" -name '*.test.js' -delete
(cd "${PKG}" && npm ci --omit=dev --no-audit --no-fund --loglevel=error)
(cd "${PKG}" && node -e "require('./lib/resolume'); require('./lib/osc')")
tar -C "${STAGE}" -czf "${STAGE}/${MODULE_ID}.tgz" "${MODULE_ID}"

echo "[2/5] Uploading..."
sshpass -e scp "${SSH_OPTS[@]}" "${STAGE}/${MODULE_ID}.tgz" "${COMPANION_USER}@${HOST}:/tmp/${MODULE_ID}.tgz"

rollback() {
  echo "ERROR: $1 — rolling back" >&2
  remote bash -s <<REMOTE || echo "ERROR: the rollback itself failed — check ${HOST} by hand" >&2
set -eu
trap 'sudo systemctl start companion' EXIT
if [ -d "${DEST}.old" ]; then
  sudo systemctl stop companion
  sudo rm -rf "${DEST}" && sudo mv "${DEST}.old" "${DEST}"
  echo "  restored the previous ${MODULE_ID}"
else
  echo "  no previous ${MODULE_ID} to restore; the new one stays installed"
fi
REMOTE
  exit 1
}

echo "[3/5] Installing into ${DEST} and restarting Companion..."
# ${DEST}.old is the last VERIFIED module: if an earlier deploy failed half-way
# it is still there, so keep it and replace whatever sits in ${DEST}.
remote bash -s <<REMOTE || rollback "the install step failed"
set -euo pipefail
trap 'sudo systemctl start companion' EXIT   # Companion must never stay down
rm -rf /tmp/${MODULE_ID}-new && mkdir /tmp/${MODULE_ID}-new
tar -C /tmp/${MODULE_ID}-new -xzf /tmp/${MODULE_ID}.tgz && rm /tmp/${MODULE_ID}.tgz
sudo chown -R companion:companion /tmp/${MODULE_ID}-new/${MODULE_ID}
sudo mkdir -p /opt/companion-module-dev
date -u '+%Y-%m-%d %H:%M:%S UTC' > /tmp/${MODULE_ID}-restarted-at
sudo systemctl stop companion
if [ -d "${DEST}.old" ]; then sudo rm -rf "${DEST}"; elif [ -d "${DEST}" ]; then sudo mv "${DEST}" "${DEST}.old"; fi
sudo mv /tmp/${MODULE_ID}-new/${MODULE_ID} "${DEST}"
rm -rf /tmp/${MODULE_ID}-new
REMOTE

echo "[4/5] Waiting for Companion to answer on :8000..."
up=""
for _ in $(seq 1 90); do
  if curl -fsS -m 3 -o /dev/null "http://${HOST}:8000/"; then up=1; break; fi
  sleep 1
done
[ -n "${up}" ] || rollback "Companion did not come back on ${HOST}:8000"

echo "[5/5] Verifying every ${MODULE_ID} connection ran its first health check..."
SINCE="$(remote "cat /tmp/${MODULE_ID}-restarted-at")" || rollback "could not read the restart time"
verified=""
for _ in $(seq 1 30); do
  set +e
  report="$(remote "sudo python3 - '${MODULE_ID}' '${SINCE}'" < "${SCRIPT_DIR}/verify-deploy.py")"
  rc=$?
  set -e
  case "${rc}" in
    0) verified=1; break ;;
    3) sleep 2 ;;
    *) echo "${report}" >&2; rollback "verification failed (exit ${rc})" ;;
  esac
done
echo "${report}" | sed 's/^/  /'
[ -n "${verified}" ] || rollback "a connection did not report its health check within 60 s"

remote "sudo rm -rf '${DEST}.old' /tmp/${MODULE_ID}-restarted-at" || echo "WARN: could not remove ${DEST}.old on ${HOST}" >&2
INSTALLED="$(remote "python3 -c \"import json; print(json.load(open('${DEST}/companion/manifest.json'))['version'])\"")" || rollback "could not read the installed manifest"
echo "  ${MODULE_ID} v${INSTALLED} installed on ${HOST}; Companion is up."
