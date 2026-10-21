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
# What Companion gets is read through config-tool `generate`, the very snippet
# launch.sh sources, instead of parsing the YAML here. When config-tool cannot
# read the file, launch.sh starts Companion with NONE of its settings, so this
# script then changes nothing and restarts nothing.
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

config_tool() {
  COMPANION_CONFIG_FILE="${CONFIG}" "${NODE}" "${CONFIG_TOOL}" "$@"
}

# The --trusted-proxies value launch.sh would pass Companion ("" = none); fails
# when config-tool cannot read the file.
trusted_proxies() {
  local snippet
  snippet="$(config_tool generate)" || return 1
  # shellcheck disable=SC2016  # expanded by the inner bash, on the snippet's own `set --`
  bash -c 'eval "$0"; v=""; while [ "$#" -gt 0 ]; do case "$1" in --trusted-proxies) v="${2:-}"; shift ;; --trusted-proxies=*) v="${1#*=}" ;; esac; shift || break; done; printf "%s" "${v}"' "${snippet}"
}

# Companion splits the value on , and ; and trims each entry.
includes_loopback() {
  printf '%s\n' "$1" | tr ',;' '\n\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -x loopback >/dev/null
}

# One HTTP request over bash's /dev/tcp; prints the status code, or nothing.
# Only the status line is read: a WebSocket answering 101 then stays open, and a
# client waiting for it to close (curl does) would turn every success into a timeout.
http_status() {
  local path=$1 host=$2 extra=""
  if [ "${3:-}" = upgrade ]; then
    extra="Origin: https://${host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
  fi
  { timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1" && printf "GET %s HTTP/1.1\r\nHost: %s\r\n%b\r\n" "$2" "$3" "$4" >&3 && read -r _ code _ <&3 && printf "%s" "${code}"' \
    "${ADDR}" "${PORT}" "${path}" "${host}" "${extra}" 2>/dev/null || true; } | tr -d '\r'
}

wait_for_companion() {
  for _attempt in $(seq 1 "${TRIES}"); do
    case "$(http_status / "${ADDR}:${PORT}")" in
      2?? | 3??) return 0 ;;
    esac
    sleep "${INTERVAL}"
  done
  return 1
}

keys="$(grep -c '^trustedProxies[[:space:]]*:' "${CONFIG}" || true)"
if [ "${keys}" -gt 1 ]; then
  echo "ERROR: ${CONFIG} sets trustedProxies ${keys} times; keep one line and deploy again." >&2
  exit 1
fi
current=""
if ! current="$(trusted_proxies)"; then
  echo "ERROR: config-tool cannot read ${CONFIG} (see above), so launch.sh would start Companion with none of its settings. Fix the file by hand; nothing changed." >&2
  exit 1
fi

backup=""
restarted=no
undo_on_failure() {
  local rc=$?
  trap - EXIT
  trap '' HUP INT TERM PIPE
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

if includes_loopback "${current}"; then
  echo "  ${CONFIG}: Companion gets --trusted-proxies '${current}', loopback included."
elif [ -n "${current}" ]; then
  echo "ERROR: ${CONFIG} already trusts '${current}' as a proxy; add loopback to that value by hand (comma-separated) and deploy again." >&2
  exit 1
else
  new_backup="$(mktemp)"
  if ! cp -p "${CONFIG}" "${new_backup}"; then
    rm -f "${new_backup}"
    echo "ERROR: could not back up ${CONFIG}; nothing changed." >&2
    exit 1
  fi
  backup="${new_backup}"
  if [ "${keys}" -eq 1 ]; then
    sed -i 's/^trustedProxies[[:space:]]*:.*/trustedProxies: loopback/' "${CONFIG}"
  else
    [ -z "$(tail -c 1 "${CONFIG}")" ] || echo >> "${CONFIG}"
    echo "trustedProxies: loopback" >> "${CONFIG}"
  fi
  if ! config_tool validate; then
    echo "ERROR: config-tool rejected 'trustedProxies: loopback'; Companion not restarted." >&2
    exit 1
  fi
  after=""
  if ! after="$(trusted_proxies)" || [ "${after}" != loopback ]; then
    echo "ERROR: after the edit config-tool reads trustedProxies as '${after:-unset}', not 'loopback'; Companion not restarted." >&2
    exit 1
  fi
  echo "  ${CONFIG}: trustedProxies: loopback (was unset); restarting Companion..."
  restarted=yes
  systemctl restart companion
fi

# The same request cloudflared makes: from loopback, with the public Host and Origin.
accepted=()
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
      # The file gives loopback but the running Companion was started before that
      # (a hand edit, or an earlier run that died): one restart loads it.
      if ! config_tool validate; then
        echo "ERROR: Companion refused (403) the web UI connection and ${CONFIG} does not validate; Companion not restarted. Fix the file by hand." >&2
        exit 1
      fi
      echo "  Companion refused the web UI connection although ${CONFIG} trusts loopback; restarting it once to load that..."
      restarted=yes
      systemctl restart companion
      attempt=0
      continue
    fi
    sleep "${INTERVAL}"
  done
  case "${code}" in
    101) accepted+=("  Companion accepts the web UI connection for https://${host}.") ;;
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

# Success: keep the new config first, then report (a dead ssh session on the
# report must not roll back a proven config).
if [ -n "${backup}" ]; then
  rm -f "${backup}"
  backup=""
fi
printf '%s\n' "${accepted[@]}"
