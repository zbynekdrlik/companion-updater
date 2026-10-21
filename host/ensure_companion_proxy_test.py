#!/usr/bin/env python3
"""Tests for host/ensure-companion-proxy.sh (run: python3 host/ensure_companion_proxy_test.py).

companion-updater#26: the Companion web UI loaded through the tunnel but never
connected. Companion 5 refuses a tRPC WebSocket from a loopback client unless the
Host is localhost or that client is a trusted proxy, and cloudflared on the rig
connects from loopback with the public Host. The script sets
`trustedProxies: loopback` in Companion's launch config, restarts Companion only
when it changed it, and FAILS unless a handshake with the public Host and Origin
answers 101.

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
    "curl": """#!/bin/bash
set -euo pipefail
echo "curl $*" >> "${STUB_DIR}/calls"
printf '%s' "${STUB_WS_CODE}"
""",
    "node": """#!/bin/bash
set -euo pipefail
echo "node ${COMPANION_CONFIG_FILE} $*" >> "${STUB_DIR}/calls"
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


class EnsureCompanionProxy(unittest.TestCase):
    def test_sets_loopback_restarts_companion_and_checks_every_public_host(self):
        proc, calls, after = run(CONFIG.format(value="null"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))
        validate = [c for c in calls if c.startswith("node ")]
        self.assertEqual(len(validate), 1)
        self.assertTrue(validate[0].endswith("/opt/companion/config-tool.js validate"), validate)
        self.assertIn("systemctl restart companion", calls)
        self.assertLess(calls.index(validate[0]), calls.index("systemctl restart companion"))
        checks = handshakes(calls)
        for host in HOSTS:
            self.assertTrue(
                any(f"Host: {host}" in c and f"Origin: https://{host}" in c and "/trpc" in c for c in checks),
                f"no handshake for {host}: {checks}",
            )
        self.assertGreater(calls.index(checks[0]), calls.index("systemctl restart companion"))

    def test_adds_the_key_when_the_config_has_none(self):
        config = CONFIG.format(value="null").replace("trustedProxies: null\n", "")
        proc, calls, after = run(config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, config + "trustedProxies: loopback\n")
        self.assertIn("systemctl restart companion", calls)

    def test_leaves_config_and_companion_alone_when_already_set(self):
        config = CONFIG.format(value="loopback")
        proc, calls, after = run(config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, config)
        self.assertFalse([c for c in calls if c.startswith("systemctl")], calls)
        self.assertEqual(len(handshakes(calls)), len(HOSTS))

    def test_fails_when_companion_refuses_the_public_handshake(self):
        proc, calls, _ = run(CONFIG.format(value="loopback"), ws_code="403", tries=3)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("403", proc.stderr)
        self.assertIn(HOSTS[0], proc.stderr)
        self.assertEqual(len(handshakes(calls)), 3)

    def test_restores_the_config_and_keeps_companion_running_when_validation_fails(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, validate_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertFalse([c for c in calls if c.startswith("systemctl")], calls)

    def test_refuses_to_override_a_proxy_list_someone_else_set(self):
        config = CONFIG.format(value="10.77.8.0/24")
        proc, calls, after = run(config)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertIn("10.77.8.0/24", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("systemctl")], calls)

    def test_fails_without_the_launch_config(self):
        proc, calls, _ = run(None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("config", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("systemctl")], calls)

    def test_fails_without_a_public_hostname(self):
        proc, calls, _ = run(CONFIG.format(value="null"), hosts=[])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
