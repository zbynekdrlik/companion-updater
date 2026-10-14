#!/usr/bin/env python3
"""Tests for host/ensure-timesync.sh (run: python3 host/ensure_timesync_test.py).

companion-updater#19: both rigs had systemd-timesyncd disabled, so their clocks
drifted (companion-pp was 60 s behind). deploy.sh runs ensure-timesync.sh on
every box: it must turn NTP on and must FAIL when the clock never reports
synchronised, instead of passing silently.

`timedatectl` is replaced by a stub on PATH that records its calls and answers
`NTPSynchronized` with a scripted sequence.
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
  "set-ntp true") exit 0 ;;
  "show -p NTPSynchronized --value")
    n=$(wc -l < "${STUB_DIR}/calls")
    if [ "${STUB_SYNC_AFTER}" -ge 0 ] && [ "$n" -gt "${STUB_SYNC_AFTER}" ]; then echo yes; else echo no; fi ;;
  status) echo "System clock synchronized: no" ;;
  *) exit 0 ;;
esac
"""


def run(sync_after, tries=3):
    """sync_after = number of stub calls before it starts answering 'yes' (-1 = never)."""
    tmp = tempfile.TemporaryDirectory()
    stub = os.path.join(tmp.name, "timedatectl")
    with open(stub, "w") as f:
        f.write(STUB)
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    env = dict(
        os.environ,
        PATH=tmp.name + os.pathsep + os.environ["PATH"],
        STUB_DIR=tmp.name,
        STUB_SYNC_AFTER=str(sync_after),
        TIMESYNC_TRIES=str(tries),
        TIMESYNC_INTERVAL="0",
    )
    proc = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)
    with open(os.path.join(tmp.name, "calls")) as f:
        calls = f.read().splitlines()
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


if __name__ == "__main__":
    unittest.main()
