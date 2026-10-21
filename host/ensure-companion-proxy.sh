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

has_key=no
if grep -q '^trustedProxies:' "${CONFIG}"; then
  has_key=yes
fi
value="$(sed -n 's/^trustedProxies:[[:space:]]*//p' "${CONFIG}" | head -n 1 | tr -d "\"'" | sed 's/[[:space:]]*$//')"

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
  if [ "${has_key}" = yes ]; then
    sed -i 's/^trustedProxies:.*/trustedProxies: loopback/' "${CONFIG}"
  else
    [ -z "$(tail -c 1 "${CONFIG}")" ] || echo >> "${CONFIG}"
    echo "trustedProxies: loopback" >> "${CONFIG}"
  fi
  if ! COMPANION_CONFIG_FILE="${CONFIG}" "${NODE}" "${CONFIG_TOOL}" validate; then
    cp -p "${backup}" "${CONFIG}"
    rm -f "${backup}"
    echo "ERROR: config-tool rejected 'trustedProxies: loopback'; ${CONFIG} restored, Companion not restarted." >&2
    exit 1
  fi
  rm -f "${backup}"
  echo "  ${CONFIG}: trustedProxies: loopback (was '${value:-unset}'); restarting Companion..."
  systemctl restart companion
fi

# The same request cloudflared makes: from loopback, with the public Host and Origin.
for host in "$@"; do
  code=""
  for _attempt in $(seq 1 "${TRIES}"); do
    code="$(curl -s -o /dev/null -m 5 --http1.1 -w '%{http_code}' \
      -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
      -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
      -H "Host: ${host}" -H "Origin: https://${host}" "${URL}/trpc" || true)"
    if [ "${code}" = 101 ]; then
      break
    fi
    sleep "${INTERVAL}"
  done
  if [ "${code}" != 101 ]; then
    echo "ERROR: Companion answered ${code:-nothing} instead of 101 to the web UI connection for https://${host}: the page would load but never connect (journalctl -u companion | grep 'Rejected tRPC')." >&2
    exit 1
  fi
  echo "  Companion accepts the web UI connection for https://${host}."
done
