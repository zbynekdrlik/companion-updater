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

HOST="${1:?usage: COMPANION_PASS=... module/deploy.sh <host>}"
COMPANION_USER="${COMPANION_USER:-newlevel}"
: "${COMPANION_PASS:?set COMPANION_PASS (SSH password of ${COMPANION_USER})}"

MODULE_ID="resolume-simple"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/${MODULE_ID}"
DEST="/opt/companion-module-dev/${MODULE_ID}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

remote() {
  sshpass -p "${COMPANION_PASS}" ssh ${SSH_OPTS} "${COMPANION_USER}@${HOST}" "$@"
}

if ! git -C "${SCRIPT_DIR}" diff --quiet HEAD -- "${SRC}" || [ -n "$(git -C "${SCRIPT_DIR}" status --porcelain -- "${SRC}")" ]; then
  echo "ERROR: ${SRC} has uncommitted changes — deploy only committed code" >&2
  exit 1
fi

VERSION="$(node -p "require('${SRC}/package.json').version")"
echo "=== Deploying ${MODULE_ID} v${VERSION} ($(git -C "${SCRIPT_DIR}" rev-parse --short HEAD)) to ${HOST} ==="

echo "[1/5] Building the package (production dependencies only)..."
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
mkdir -p "${STAGE}/${MODULE_ID}/lib"
cp "${SRC}/index.js" "${SRC}/package.json" "${SRC}/package-lock.json" "${STAGE}/${MODULE_ID}/"
cp -r "${SRC}/companion" "${STAGE}/${MODULE_ID}/"
find "${SRC}/lib" -maxdepth 1 -name '*.js' ! -name '*.test.js' -exec cp {} "${STAGE}/${MODULE_ID}/lib/" \;
(cd "${STAGE}/${MODULE_ID}" && npm ci --omit=dev --no-audit --no-fund --loglevel=error)
tar -C "${STAGE}" -czf "${STAGE}/${MODULE_ID}.tgz" "${MODULE_ID}"

echo "[2/5] Uploading..."
sshpass -p "${COMPANION_PASS}" scp ${SSH_OPTS} "${STAGE}/${MODULE_ID}.tgz" "${COMPANION_USER}@${HOST}:/tmp/${MODULE_ID}.tgz"

echo "[3/5] Installing into ${DEST} and restarting Companion..."
remote "set -euo pipefail
  sudo rm -rf /tmp/${MODULE_ID}-new && mkdir /tmp/${MODULE_ID}-new
  tar -C /tmp/${MODULE_ID}-new -xzf /tmp/${MODULE_ID}.tgz && rm /tmp/${MODULE_ID}.tgz
  sudo chown -R companion:companion /tmp/${MODULE_ID}-new/${MODULE_ID}
  sudo systemctl stop companion
  sudo rm -rf ${DEST}.old
  if [ -d ${DEST} ]; then sudo mv ${DEST} ${DEST}.old; fi
  sudo mv /tmp/${MODULE_ID}-new/${MODULE_ID} ${DEST}
  sudo rm -rf /tmp/${MODULE_ID}-new ${DEST}.old
  sudo systemctl start companion"

echo "[4/5] Waiting for Companion to answer on :8000..."
for _ in $(seq 1 60); do
  if curl -fsS -m 3 -o /dev/null "http://${HOST}:8000/"; then break; fi
  sleep 1
done
curl -fsS -m 3 -o /dev/null "http://${HOST}:8000/" || { echo "ERROR: Companion did not come back on ${HOST}:8000" >&2; exit 1; }

echo "[5/5] Verifying the installed module..."
INSTALLED="$(remote "python3 -c \"import json; print(json.load(open('${DEST}/companion/manifest.json'))['version'])\"")"
if [ "${INSTALLED}" != "${VERSION}" ]; then
  echo "ERROR: ${DEST} reports v${INSTALLED}, expected v${VERSION}" >&2
  exit 1
fi
echo "  ${MODULE_ID} v${INSTALLED} installed on ${HOST}; Companion is up."
