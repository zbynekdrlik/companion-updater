#!/bin/bash
# Let Companion's web UI work behind the rig's own cloudflared tunnel, then prove
# it. Run as root on a Companion host with the rig's public Companion hostnames
# as arguments; deploy.sh installs and runs it.
#
# companion-updater#26: Companion 5 refuses the UI's live connection (the tRPC
# WebSocket) from a loopback client whose Host is not localhost, unless that
# client is a trusted proxy. cloudflared runs on the rig, connects to
# localhost:8000 and keeps the public Host, so the UI loaded but never
# connected. `trustedProxies: loopback` in the launch config fixes that;
# companion-pi's launch.sh passes it to Companion, and update.sh never
# overwrites a value that is already set.
#
# The rig is a live A/V machine: if anything fails after the config was edited,
# the old config is put back and Companion restarted on it.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <public Companion hostname>..." >&2
  exit 2
fi

CONFIG="${COMPANION_CONFIG_FILE:-/etc/companion/config.yaml}"
NODE="${COMPANION_NODE:-}"
if [ -z "${NODE}" ]; then
  NODE=/opt/companion/node-runtime/bin/node
  [ -x "${NODE}" ] || NODE=/opt/companion/node-runtimes/main/bin/node
fi
CONFIG_TOOL="${COMPANION_CONFIG_TOOL:-/opt/companion/config-tool.js}"
URL="${COMPANION_URL:-http://127.0.0.1:8000}"
TRIES="${PROXY_TRIES:-30}"
INTERVAL="${PROXY_INTERVAL:-2}"

if [ ! -f "${CONFIG}" ]; then
  echo "ERROR: no Companion launch config at ${CONFIG} (Companion older than 5.0, or not installed by companion-pi)" >&2
  exit 1
fi

KEY='^trustedProxies[[:space:]]*:'
keys="$(grep -c "${KEY}" "${CONFIG}" || true)"
if [ "${keys}" -gt 1 ]; then
  echo "ERROR: ${CONFIG} sets trustedProxies ${keys} times; keep one line and deploy again." >&2
  exit 1
fi
value="$(sed -n "s/${KEY}[[:space:]]*//p" "${CONFIG}" | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//' | tr -d "\"'")"
next_line="$(awk -v key="${KEY}" '$0 ~ key { if ((getline line) > 0) print line; exit }' "${CONFIG}")"
if [ -z "${value}" ] && printf '%s\n' "${next_line}" | grep -E '^[[:space:]]+-' >/dev/null; then
  echo "ERROR: ${CONFIG} has trustedProxies as a YAML list; write it on one line (comma-separated) including loopback, and deploy again." >&2
  exit 1
fi

wait_for_companion() {
  for _attempt in $(seq 1 "${TRIES}"); do
    if curl -sf -o /dev/null -m 2 "${URL}/"; then
      return 0
    fi
    sleep "${INTERVAL}"
  done
  return 1
}

backup=""
restarted=no
undo_on_failure() {
  local rc=$?
  if [ -n "${backup}" ]; then
    cp -p "${backup}" "${CONFIG}" || true
    rm -f "${backup}"
    if [ "${restarted}" = yes ]; then
      systemctl restart companion || true
      wait_for_companion || echo "ERROR: Companion did not come back on the restored config (journalctl -u companion -n 50)." >&2
    fi
    echo "  ${CONFIG} restored to its previous content." >&2
  fi
  exit "${rc}"
}
trap undo_on_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if printf '%s\n' "${value}" | tr ',;' '\n\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -x loopback >/dev/null; then
  echo "  ${CONFIG}: trustedProxies already includes loopback."
else
  case "${value}" in
    "" | null | "~") ;;
    *)
      echo "ERROR: ${CONFIG} already trusts '${value}' as a proxy; add loopback to that list by hand (comma-separated) and deploy again." >&2
      exit 1 ;;
  esac
  backup="$(mktemp)"
  cp -p "${CONFIG}" "${backup}"
  if [ "${keys}" -eq 1 ]; then
    sed -i "s/${KEY}.*/trustedProxies: loopback/" "${CONFIG}"
  else
    [ -z "$(tail -c 1 "${CONFIG}")" ] || echo >> "${CONFIG}"
    echo "trustedProxies: loopback" >> "${CONFIG}"
  fi
  if ! COMPANION_CONFIG_FILE="${CONFIG}" "${NODE}" "${CONFIG_TOOL}" validate; then
    echo "ERROR: config-tool rejected 'trustedProxies: loopback'; Companion not restarted." >&2
    exit 1
  fi
  echo "  ${CONFIG}: trustedProxies: loopback (was '${value:-unset}'); restarting Companion..."
  restarted=yes
  systemctl restart companion
fi

# The same request cloudflared makes: from loopback, with the public Host and Origin.
handshake() {
  curl -s -o /dev/null -m 2 --http1.1 -w '%{http_code}' \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H "Host: $1" -H "Origin: https://$1" "${URL}/trpc" || true
}

for host in "$@"; do
  code=""
  for _attempt in $(seq 1 "${TRIES}"); do
    code="$(handshake "${host}")"
    if [ "${code}" = 101 ]; then
      break
    fi
    # A refusal from a Companion this run did not restart will not change by waiting.
    if [ "${code}" = 403 ] && [ "${restarted}" = no ]; then
      break
    fi
    sleep "${INTERVAL}"
  done
  case "${code}" in
    101) echo "  Companion accepts the web UI connection for https://${host}." ;;
    403)
      echo "ERROR: Companion refused (403) the web UI connection for https://${host}: the page would load but never connect (journalctl -u companion | grep 'Rejected tRPC')." >&2
      exit 1 ;;
    000 | "")
      echo "ERROR: Companion is not answering on ${URL} (journalctl -u companion -n 50)." >&2
      exit 1 ;;
    *)
      echo "ERROR: Companion answered ${code} instead of 101 to the web UI connection for https://${host}." >&2
      exit 1 ;;
  esac
done

# Success: keep the new config.
rm -f "${backup}"
backup=""
