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
# the old config is put back (atomically) and Companion restarted on it.
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
ADDR="${COMPANION_ADDR:-127.0.0.1}"
PORT="${COMPANION_PORT:-8000}"
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
# The value as written (comment dropped), then unquoted and out of a [flow, list].
raw="$(sed -n "s/${KEY}[[:space:]]*//p" "${CONFIG}" | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//')"
value="$(printf '%s' "${raw}" | tr -d "\"'" | sed 's/^\[[[:space:]]*//; s/[[:space:]]*\]$//')"
# An empty value followed by "- item" lines (after any blank or comment lines) is a block list.
next_line="$(awk -v key="${KEY}" 'found && $0 !~ /^[[:space:]]*(#.*)?$/ { print; exit } $0 ~ key { found = 1 }' "${CONFIG}")"
if [ -z "${raw}" ] && printf '%s\n' "${next_line}" | grep -E '^[[:space:]]*-' >/dev/null; then
  echo "ERROR: ${CONFIG} has trustedProxies as a YAML list; write it on one line (comma-separated) including loopback, and deploy again." >&2
  exit 1
fi

# One HTTP request over bash's /dev/tcp; prints the status code, or nothing.
# Only the status line is read: a WebSocket answering 101 then stays open, and a
# client waiting for it to close (curl does) would turn every success into a timeout.
http_status() {
  local path=$1 host=$2 extra=""
  if [ "${3:-}" = upgrade ]; then
    extra="Origin: https://${host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
  fi
  timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1" && printf "GET %s HTTP/1.1\r\nHost: %s\r\n%b\r\n" "$2" "$3" "$4" >&3 && read -r _ code _ <&3 && printf "%s" "${code}"' \
    "${ADDR}" "${PORT}" "${path}" "${host}" "${extra}" 2>/dev/null || true
}

wait_for_companion() {
  for _attempt in $(seq 1 "${TRIES}"); do
    if [ "$(http_status / "${ADDR}:${PORT}")" = 200 ]; then
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
  trap - EXIT
  if [ -n "${backup}" ]; then
    if cp -p "${backup}" "${CONFIG}.restore" && mv -f "${CONFIG}.restore" "${CONFIG}"; then
      rm -f "${backup}"
      if [ "${restarted}" = yes ]; then
        systemctl restart companion || echo "ERROR: restarting Companion on the restored config failed." >&2 || true
        wait_for_companion || echo "ERROR: Companion did not come back on the restored config (journalctl -u companion -n 50)." >&2 || true
      fi
      echo "  ${CONFIG} restored to its previous content." >&2 || true
    else
      rm -f "${CONFIG}.restore"
      echo "ERROR: could not restore ${CONFIG} (it still says trustedProxies: loopback); backup kept at ${backup}" >&2 || true
    fi
  fi
  exit "${rc}"
}
trap undo_on_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 141' PIPE
trap 'exit 143' TERM

if printf '%s\n' "${value}" | tr ',;' '\n\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -x loopback >/dev/null; then
  echo "  ${CONFIG}: trustedProxies already includes loopback."
else
  case "${value}" in
    "" | null | "~") ;;
    *)
      echo "ERROR: ${CONFIG} already trusts '${raw}' as a proxy; add loopback to that list by hand (comma-separated) and deploy again." >&2
      exit 1 ;;
  esac
  new_backup="$(mktemp)"
  if ! cp -p "${CONFIG}" "${new_backup}"; then
    rm -f "${new_backup}"
    echo "ERROR: could not back up ${CONFIG}; nothing changed." >&2
    exit 1
  fi
  backup="${new_backup}"
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
  echo "  ${CONFIG}: trustedProxies: loopback (was '${raw:-unset}'); restarting Companion..."
  restarted=yes
  systemctl restart companion
fi

# The same request cloudflared makes: from loopback, with the public Host and Origin.
for host in "$@"; do
  code=""
  attempt=0
  while [ "${attempt}" -lt "${TRIES}" ]; do
    attempt=$((attempt + 1))
    code="$(http_status /trpc "${host}" upgrade)"
    if [ "${code}" = 101 ]; then
      break
    fi
    if [ "${code}" = 403 ]; then
      # A Companion restarted by this run answers 403 deliberately; waiting changes nothing.
      if [ "${restarted}" = yes ]; then
        break
      fi
      # The file trusts loopback but the running Companion was started before that
      # (a hand edit, or an earlier run that died): one restart loads it.
      echo "  Companion refused the web UI connection although ${CONFIG} trusts loopback; restarting it once to load that..."
      restarted=yes
      systemctl restart companion
      attempt=0
      continue
    fi
    sleep "${INTERVAL}"
  done
  case "${code}" in
    101) echo "  Companion accepts the web UI connection for https://${host}." ;;
    403)
      echo "ERROR: Companion refused (403) the web UI connection for https://${host}: the page would load but never connect (journalctl -u companion | grep 'Rejected tRPC')." >&2
      exit 1 ;;
    "")
      echo "ERROR: Companion is not answering on ${ADDR}:${PORT} (journalctl -u companion -n 50)." >&2
      exit 1 ;;
    *)
      echo "ERROR: Companion answered ${code} instead of 101 to the web UI connection for https://${host}." >&2
      exit 1 ;;
  esac
done

# Success: keep the new config.
if [ -n "${backup}" ]; then
  rm -f "${backup}"
  backup=""
fi
