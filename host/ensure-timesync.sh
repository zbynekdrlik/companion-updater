#!/bin/bash
# Turn on NTP time sync (systemd-timesyncd) and wait until the kernel reports
# the clock synchronised. Run as root on a Companion host; deploy.sh installs
# and runs it. Fails loudly when the clock does not synchronise.
#
# companion-updater#19: timesyncd was disabled on both rigs and companion-pp
# drifted 60 s behind.
#
# companion-updater#49: where dsync.service keeps the clock (PTP), dsync turns
# NTP off at its start. Turning timesyncd back on would fight it, so on such a
# box this script changes nothing.
set -euo pipefail

if [ "$(systemctl is-enabled dsync.service 2>/dev/null || true)" = "enabled" ]; then
  echo "  dsync.service is enabled: it keeps the clock, leaving timesyncd (NTP) off."
  exit 0
fi

TRIES="${TIMESYNC_TRIES:-30}"
INTERVAL="${TIMESYNC_INTERVAL:-2}"

echo "  Enabling NTP (timedatectl set-ntp true)..."
timedatectl set-ntp true

for attempt in $(seq 1 "${TRIES}"); do
  synced="$(timedatectl show -p NTPSynchronized --value)"
  if [ "${synced}" = "yes" ]; then
    echo "  Clock synchronised (attempt ${attempt}/${TRIES})."
    exit 0
  fi
  echo "  Waiting for NTP sync: NTPSynchronized=${synced} (attempt ${attempt}/${TRIES})"
  sleep "${INTERVAL}"
done

echo "ERROR: clock not synchronised after ${TRIES} checks, ${INTERVAL} s apart." >&2
timedatectl status >&2 || true
exit 1
