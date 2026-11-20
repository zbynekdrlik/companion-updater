#!/usr/bin/env python3
"""Tests for host/ensure-timesync.sh (run: python3 host/ensure_timesync_test.py).

companion-updater#19: both rigs had systemd-timesyncd disabled, so their clocks
drifted (companion-pp was 60 s behind). deploy.sh runs ensure-timesync.sh on
every box: it must turn NTP on and must FAIL when the clock never reports
synchronised, instead of passing silently.

`timedatectl` is replaced by a stub on PATH that records its calls and answers
`NTPSynchronized` with a scripted sequence. `systemctl` is stubbed too: it
answers `is-enabled dsync.service` with STUB_DSYNC.

companion-updater#49: on a box where dsync.service keeps the clock (PTP), dsync
turns NTP off at its start; set-ntp true at every deploy fights it. There the
script must leave timesyncd alone.
"""
import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ensure-timesync.sh")

STUB = """#!/bin/bash
set -euo pipefail
echo "$*" >> "${STUB_DIR}/calls"
case "$*" in
  "set-ntp true") exit "${STUB_SETNTP_EXIT}" ;;
  "show -p NTPSynchronized --value")
    n=$(wc -l < "${STUB_DIR}/calls")
    if [ "${STUB_SYNC_AFTER}" -ge 0 ] && [ "$n" -gt "${STUB_SYNC_AFTER}" ]; then echo yes; else echo no; fi ;;
  status) echo "System clock synchronized: no" ;;
  *) exit 0 ;;
esac
"""

SYSTEMCTL_STUB = """#!/bin/bash
set -euo pipefail
echo "systemctl $*" >> "${STUB_DIR}/calls"
if [ "$*" = "is-enabled dsync.service" ]; then
  echo "${STUB_DSYNC}"
  [ "${STUB_DSYNC}" = "enabled" ] && exit 0
  exit 1
fi
exit 0
"""


def run(sync_after, tries=3, setntp_exit=0, dsync="not-found"):
    """sync_after = number of stub calls before it starts answering 'yes' (-1 = never)."""
    tmp = tempfile.TemporaryDirectory()
    stub = os.path.join(tmp.name, "timedatectl")
    with open(stub, "w") as f:
        f.write(STUB)
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    sysctl = os.path.join(tmp.name, "systemctl")
    with open(sysctl, "w") as f:
        f.write(SYSTEMCTL_STUB)
    os.chmod(sysctl, os.stat(sysctl).st_mode | stat.S_IEXEC)
    open(os.path.join(tmp.name, "calls"), "w").close()
    env = dict(
        os.environ,
        PATH=tmp.name + os.pathsep + os.environ["PATH"],
        STUB_DIR=tmp.name,
        STUB_SYNC_AFTER=str(sync_after),
        STUB_SETNTP_EXIT=str(setntp_exit),
        STUB_DSYNC=dsync,
        TIMESYNC_TRIES=str(tries),
        TIMESYNC_INTERVAL="0",
    )
    proc = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)
    with open(os.path.join(tmp.name, "calls")) as f:
        calls = [c for c in f.read().splitlines() if not c.startswith("systemctl ")]
    tmp.cleanup()
    return proc, calls


class EnsureTimesync(unittest.TestCase):
    def test_turns_ntp_on_and_succeeds_once_the_clock_is_synchronised(self):
        proc, calls = run(sync_after=2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls[0], "set-ntp true")
        self.assertIn("synchronised", proc.stdout)

    def test_fails_when_the_clock_never_synchronises(self):
        proc, calls = run(sync_after=-1, tries=3)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls[0], "set-ntp true")
        self.assertEqual(calls.count("show -p NTPSynchronized --value"), 3)
        self.assertIn("not synchronised", proc.stderr)

    def test_fails_when_turning_ntp_on_fails_even_if_the_clock_reports_synced(self):
        proc, calls = run(sync_after=0, setntp_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls, ["set-ntp true"])

    def test_leaves_timesyncd_alone_when_dsync_keeps_the_clock(self):
        proc, calls = run(sync_after=-1, dsync="enabled")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, [], "no timedatectl call may run when dsync.service is enabled")
        self.assertIn("dsync.service is enabled", proc.stdout)

    def test_disabled_dsync_still_turns_ntp_on(self):
        proc, calls = run(sync_after=2, dsync="disabled")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls[0], "set-ntp true")


if __name__ == "__main__":
    unittest.main()
