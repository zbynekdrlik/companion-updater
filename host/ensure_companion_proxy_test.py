#!/usr/bin/env python3
"""Tests for host/ensure-companion-proxy.sh (run: python3 host/ensure_companion_proxy_test.py).

companion-updater#26: the Companion web UI loaded through the tunnel but never
connected. Companion 5 refuses a tRPC WebSocket from a loopback client unless the
Host is localhost or that client is a trusted proxy, and cloudflared on the rig
connects from loopback with the public Host. The script sets
`trustedProxies: loopback` in Companion's launch config, restarts Companion only
when it changed it, and FAILS unless a handshake with the public Host and Origin
answers 101. When anything fails after the edit, the old config is put back.

`systemctl`, `curl` and Companion's node (running config-tool) are stubs on
PATH that record their calls.
"""
import os
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ensure-companion-proxy.sh")
HOSTS = ["companion-snv.newlevel.media", "companion.newlevel.media"]

CONFIG = """# Companion headless launch configuration.

# Set the port the admin ui should bind to
adminPort: 8000
# Allow loading modules that are otherwise held back for safety
enableRestrictedModules: true
# Trust a reverse proxy in front of the admin UI, so the real client ip is detected instead of the proxy address
trustedProxies: {value}
# Don't show version-related notifications in the header.
notifications: true
"""

STUBS = {
    "systemctl": """#!/bin/bash
set -euo pipefail
echo "systemctl $*" >> "${STUB_DIR}/calls"
""",
    # The WebSocket handshake prints a status; the readiness probe (curl -sf URL/) only exits.
    "curl": """#!/bin/bash
set -euo pipefail
echo "curl $*" >> "${STUB_DIR}/calls"
case "$*" in
  *"Upgrade: websocket"*) printf '%s' "${STUB_WS_CODE}" ;;
  *) exit "${STUB_HTTP_EXIT}" ;;
esac
""",
    # Records what the config says at the moment it is validated.
    "node": """#!/bin/bash
set -euo pipefail
echo "node [$(grep '^trustedProxies' "${COMPANION_CONFIG_FILE}" || true)] $*" >> "${STUB_DIR}/calls"
exit "${STUB_VALIDATE_EXIT}"
""",
}


def run(config, hosts=HOSTS, ws_code="101", validate_exit=0, tries=2):
    """config = the launch config's text (None = no file). Returns (proc, calls, config after)."""
    tmp = tempfile.TemporaryDirectory()
    for name, body in STUBS.items():
        path = os.path.join(tmp.name, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    open(os.path.join(tmp.name, "calls"), "w").close()
    config_file = os.path.join(tmp.name, "config.yaml")
    if config is not None:
        with open(config_file, "w") as f:
            f.write(config)
    env = dict(
        os.environ,
        PATH=tmp.name + os.pathsep + os.environ["PATH"],
        STUB_DIR=tmp.name,
        STUB_WS_CODE=ws_code,
        STUB_HTTP_EXIT="0",
        STUB_VALIDATE_EXIT=str(validate_exit),
        COMPANION_CONFIG_FILE=config_file,
        COMPANION_NODE=os.path.join(tmp.name, "node"),
        COMPANION_CONFIG_TOOL="/opt/companion/config-tool.js",
        PROXY_TRIES=str(tries),
        PROXY_INTERVAL="0",
    )
    proc = subprocess.run(["bash", SCRIPT, *hosts], capture_output=True, text=True, env=env)
    with open(os.path.join(tmp.name, "calls")) as f:
        calls = f.read().splitlines()
    after = None
    if os.path.exists(config_file):
        with open(config_file) as f:
            after = f.read()
    tmp.cleanup()
    return proc, calls, after


def handshakes(calls):
    return [c for c in calls if c.startswith("curl ") and "Upgrade: websocket" in c]


def systemctl(calls):
    return [c for c in calls if c.startswith("systemctl ")]


class SetsTheProxy(unittest.TestCase):
    def test_sets_loopback_validates_it_restarts_companion_and_checks_every_public_host(self):
        proc, calls, after = run(CONFIG.format(value="null"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))
        validate = [c for c in calls if c.startswith("node ")]
        self.assertEqual(validate, ["node [trustedProxies: loopback] /opt/companion/config-tool.js validate"])
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])
        self.assertLess(calls.index(validate[0]), calls.index("systemctl restart companion"))
        checks = handshakes(calls)
        self.assertEqual(len(checks), len(HOSTS))
        for host, check in zip(HOSTS, checks):
            self.assertIn(f"Host: {host}", check)
            self.assertIn(f"Origin: https://{host}", check)
            self.assertTrue(check.endswith(" http://127.0.0.1:8000/trpc"), check)
        self.assertGreater(calls.index(checks[0]), calls.index("systemctl restart companion"))

    def test_adds_the_key_when_the_config_has_none(self):
        config = CONFIG.format(value="null").replace("trustedProxies: null\n", "")
        proc, calls, after = run(config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, config + "trustedProxies: loopback\n")
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])

    def test_adds_the_key_on_its_own_line_when_the_file_does_not_end_with_a_newline(self):
        config = "adminPort: 8000\nnotifications: true"
        proc, _calls, after = run(config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, config + "\ntrustedProxies: loopback\n")

    def test_replaces_a_key_written_with_a_space_before_the_colon(self):
        proc, _calls, after = run(CONFIG.format(value="null").replace("trustedProxies: null", "trustedProxies : null"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))


class LeavesItAlone(unittest.TestCase):
    def test_leaves_config_and_companion_alone_when_loopback_is_already_listed(self):
        for value in ("loopback", '"loopback"', "10.77.8.0/24, loopback", "loopback;10.77.8.0/24", "loopback  # tunnel"):
            with self.subTest(value=value):
                config = CONFIG.format(value=value)
                proc, calls, after = run(config)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(after, config)
                self.assertEqual(systemctl(calls), [])
                self.assertFalse([c for c in calls if c.startswith("node ")])
                self.assertEqual(len(handshakes(calls)), len(HOSTS))

    def test_refuses_to_override_a_proxy_list_someone_else_set(self):
        config = CONFIG.format(value="10.77.8.0/24")
        proc, calls, after = run(config)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertIn("10.77.8.0/24", proc.stderr)
        self.assertEqual(systemctl(calls), [])

    def test_refuses_shapes_it_cannot_edit_safely(self):
        shapes = {
            "duplicate key": CONFIG.format(value="null") + "trustedProxies: 10.0.0.0/8\n",
            "YAML list": CONFIG.format(value="").replace("trustedProxies: \n", "trustedProxies:\n  - 10.0.0.0/8\n"),
        }
        for name, config in shapes.items():
            with self.subTest(shape=name):
                proc, calls, after = run(config)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(after, config)
                self.assertEqual(systemctl(calls), [])


class Fails(unittest.TestCase):
    def test_fails_at_once_when_an_untouched_companion_refuses_the_public_handshake(self):
        proc, calls, _ = run(CONFIG.format(value="loopback"), ws_code="403", tries=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("403", proc.stderr)
        self.assertIn(HOSTS[0], proc.stderr)
        self.assertEqual(len(handshakes(calls)), 1)
        self.assertEqual(systemctl(calls), [])

    def test_restores_the_config_and_restarts_companion_on_it_when_the_check_fails_after_the_edit(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, ws_code="000", tries=3)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), ["systemctl restart companion", "systemctl restart companion"])
        self.assertEqual(len(handshakes(calls)), 3)
        second_restart = len(calls) - 1 - calls[::-1].index("systemctl restart companion")
        self.assertTrue(
            any(c.startswith("curl -sf") and c.endswith(" http://127.0.0.1:8000/") for c in calls[second_restart:]),
            "no readiness check after restoring",
        )
        self.assertIn("not answering", proc.stderr)
        self.assertIn("restored", proc.stderr)

    def test_restores_the_config_and_keeps_companion_running_when_validation_fails(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, validate_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), [])

    def test_fails_without_the_launch_config(self):
        proc, calls, _ = run(None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("config", proc.stderr)
        self.assertEqual(systemctl(calls), [])

    def test_fails_without_a_public_hostname(self):
        proc, calls, _ = run(CONFIG.format(value="null"), hosts=[])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
