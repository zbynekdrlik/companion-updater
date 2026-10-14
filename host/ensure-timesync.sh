#!/bin/bash
# Turn on NTP time sync (systemd-timesyncd) and wait until the kernel reports
# the clock synchronised. Run as root on a Companion host; deploy.sh installs
# and runs it. Fails loudly when the clock does not synchronise.
#
# companion-updater#19: timesyncd was disabled on both rigs and companion-pp
# drifted 60 s behind.
set -euo pipefail

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
