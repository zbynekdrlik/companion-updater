#!/usr/bin/env python3
"""Tests for host/ensure-companion-proxy.sh (run: python3 host/ensure_companion_proxy_test.py).

companion-updater#26: the Companion web UI loaded through the tunnel but never
connected. Companion 5 refuses a tRPC WebSocket from a loopback client unless the
Host is localhost or that client is a trusted proxy, and cloudflared on the rig
connects from loopback with the public Host. The script sets
`trustedProxies: loopback` in Companion's launch config, restarts Companion only
when needed, and FAILS unless a handshake with the public Host and Origin
answers 101. When anything fails after the edit, the old config is put back.

Companion is a small TCP server here that answers like the real one (a 101 keeps
the socket open, like a WebSocket does); `systemctl` and `cp` are stubs on PATH
that record their calls. Companion's config-tool (node) is a fake that reads
trustedProxies the way the real one does, as probed on companion-pp
(Companion 5.0.7) on 2026-10-21: null / empty / ~ / "" = unset; quotes and
comments dropped; a value on the next, indented line is the value; any YAML
list is an error (and launch.sh would then start Companion with NO flags);
with a duplicate key the first one wins.
"""
import os
import re
import select
import socketserver
import stat
import subprocess
import tempfile
import threading
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
    # The STUB_SYSTEMCTL_FAIL_AT-th call fails, like a unit that does not start.
    "systemctl": """#!/bin/bash
set -euo pipefail
n=$(( $(cat "${STUB_DIR}/systemctl_count" 2>/dev/null || echo 0) + 1 ))
echo "${n}" > "${STUB_DIR}/systemctl_count"
echo "systemctl $*" >> "${STUB_DIR}/calls"
if [ "${n}" = "${STUB_SYSTEMCTL_FAIL_AT}" ]; then
  echo "Job for companion.service failed." >&2
  exit 1
fi
if [ "${n}" = "${STUB_SYSTEMCTL_TERM_PARENT_AT}" ]; then
  kill -TERM "${PPID}"
fi
""",
    # config-tool: records what the config says when it is read, then validates or generates.
    "node": """#!/bin/bash
set -euo pipefail
echo "node [$(grep '^trustedProxies' "${COMPANION_CONFIG_FILE}" || true)] $*" >> "${STUB_DIR}/calls"
exec python3 "${STUB_DIR}/config_tool.py" "$@"
""",
    "config_tool.py": """import os, re, shlex, sys
path, cmd = os.environ["COMPANION_CONFIG_FILE"], sys.argv[-1]
lines = open(path).read().splitlines()
value, error = None, None
for i, line in enumerate(lines):
    if re.match(r"trustedProxies\\s*:", line):
        raw = re.sub(r"\\s+#.*$", "", line.split(":", 1)[1]).strip()
        if not raw:
            rest = [l for l in lines[i + 1:] if l.strip() and not l.strip().startswith("#")]
            if rest and rest[0].lstrip().startswith("-"):
                error = "list"
            elif rest and rest[0][:1].isspace():
                raw = rest[0].strip()
        if raw[:1] in "[{" and raw:
            error = "list"
        raw = raw[1:-1] if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\\"" else raw
        value = None if raw in ("", "null", "~") else raw
        break
if error:
    print('Error: Invalid value for "trustedProxies": Expected a single value, not a list or object', file=sys.stderr)
    sys.exit(1)
if cmd == "validate":
    if os.environ.get("STUB_VALIDATE_EXIT", "0") != "0":
        print("trustedProxies: rejected", file=sys.stderr)
        sys.exit(int(os.environ["STUB_VALIDATE_EXIT"]))
    print(path + " is valid.")
elif cmd == "generate":
    if value == "loopback" and os.environ.get("STUB_READS_AS"):
        value = os.environ["STUB_READS_AS"]
    flags = "--admin-port 8000 --extra-module-path /opt/companion-module-dev --enable-restricted-modules"
    print("set -- " + flags + ("" if value is None else " --trusted-proxies " + shlex.quote(value)))
""",
    # The STUB_CP_FAIL_AT-th copy behaves like a full disk: truncated target, exit 1.
    "cp": """#!/bin/bash
set -euo pipefail
n=$(( $(cat "${STUB_DIR}/cp_count" 2>/dev/null || echo 0) + 1 ))
echo "${n}" > "${STUB_DIR}/cp_count"
echo "cp $*" >> "${STUB_DIR}/calls"
if [ "${n}" = "${STUB_CP_FAIL_AT}" ]; then
  : > "${@: -1}"
  echo "cp: error writing '${@: -1}': No space left on device" >&2
  exit 1
fi
exec /bin/cp "$@"
""",
}


class FakeCompanion(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class CompanionHandler(socketserver.StreamRequestHandler):
    """One HTTP request: a WebSocket upgrade gets the next scripted answer, anything else 200."""

    def handle(self):
        lines = []
        while True:
            line = self.rfile.readline(4096)
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line.decode().rstrip("\r\n"))
        if not lines:
            return
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        upgrade = headers.get("upgrade", "").lower() == "websocket"
        srv = self.server
        with srv.lock:
            answer = srv.http_answer
            if upgrade:
                answer = srv.ws_answers[min(srv.ws_count, len(srv.ws_answers) - 1)]
                srv.ws_count += 1
            with open(srv.calls, "a") as f:
                f.write(f"http {lines[0]} host={headers.get('host', '')} origin={headers.get('origin', '')} "
                        f"upgrade={'yes' if upgrade else 'no'}\n")
        if answer == "close":
            return
        if upgrade:
            threading.Event().wait(srv.ws_delay)
        # "101-bare": a status line without a reason phrase, which HTTP allows.
        status = "HTTP/1.1 101" if answer == "101-bare" else f"HTTP/1.1 {answer} Scripted"
        self.wfile.write(f"{status}\r\nContent-Length: 0\r\n\r\n".encode())
        self.wfile.flush()
        if answer.startswith("101"):
            # A WebSocket stays open until the client hangs up (recv gives b"").
            readable, _, _ = select.select([self.request], [], [], 10)
            if readable:
                try:
                    self.request.recv(1)
                except ConnectionResetError:
                    return  # a reset is how a client that read only the status line hangs up


def run(config, hosts=HOSTS, ws=("101",), ws_delay=0.0, http_answer="200", validate_exit=0, cp_fail_at=0,
        systemctl_fail_at=0, systemctl_term_parent_at=0, reads_as="", tries=2, leftovers=None, stdout_lines=None):
    """config = the launch config's text (None = no file). Returns (proc, calls, config after).

    leftovers: a dict that receives the temp files the script left behind (in its TMPDIR, and
    config.yaml.restore next to the config), name -> content.
    stdout_lines: read that many lines of the script's stdout, then close it (a dead ssh session)."""
    tmp = tempfile.TemporaryDirectory()
    for name, body in STUBS.items():
        path = os.path.join(tmp.name, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    calls_path = os.path.join(tmp.name, "calls")
    open(calls_path, "w").close()
    config_file = os.path.join(tmp.name, "config.yaml")
    if config is not None:
        with open(config_file, "w") as f:
            f.write(config)
    server = FakeCompanion(("127.0.0.1", 0), CompanionHandler)
    server.lock, server.calls, server.ws_answers, server.ws_count, server.ws_delay = threading.Lock(), calls_path, list(ws), 0, ws_delay
    server.http_answer = http_answer
    script_tmp = os.path.join(tmp.name, "script-tmp")
    os.mkdir(script_tmp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(
        os.environ,
        PATH=tmp.name + os.pathsep + os.environ["PATH"],
        STUB_DIR=tmp.name,
        STUB_VALIDATE_EXIT=str(validate_exit),
        STUB_CP_FAIL_AT=str(cp_fail_at),
        STUB_SYSTEMCTL_FAIL_AT=str(systemctl_fail_at),
        STUB_SYSTEMCTL_TERM_PARENT_AT=str(systemctl_term_parent_at),
        STUB_READS_AS=reads_as,
        COMPANION_CONFIG_FILE=config_file,
        COMPANION_NODE=os.path.join(tmp.name, "node"),
        COMPANION_CONFIG_TOOL="/opt/companion/config-tool.js",
        COMPANION_ADDR="127.0.0.1",
        COMPANION_PORT=str(server.server_address[1]),
        PROXY_TRIES=str(tries),
        PROXY_INTERVAL="0",
        TMPDIR=script_tmp,
    )
    try:
        if stdout_lines is None:
            proc = subprocess.run(["bash", SCRIPT, *hosts], capture_output=True, text=True, env=env, timeout=60)
        else:
            child = subprocess.Popen(["bash", SCRIPT, *hosts], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            out = "".join(child.stdout.readline() for _ in range(stdout_lines))
            child.stdout.close()
            err = child.stderr.read()
            child.wait(timeout=60)
            proc = subprocess.CompletedProcess(child.args, child.returncode, out, err)
    finally:
        server.shutdown()
        server.server_close()
    with open(calls_path) as f:
        calls = f.read().splitlines()
    if leftovers is not None:
        for path in [os.path.join(script_tmp, n) for n in os.listdir(script_tmp)] + [config_file + ".restore"]:
            if os.path.exists(path):
                with open(path) as f:
                    leftovers[os.path.basename(path)] = f.read()
    after = None
    if os.path.exists(config_file):
        with open(config_file) as f:
            after = f.read()
    tmp.cleanup()
    return proc, calls, after


def handshakes(calls):
    return [c for c in calls if c.startswith("http ") and c.endswith("upgrade=yes")]


def systemctl(calls):
    return [c for c in calls if c.startswith("systemctl ")]


class SetsTheProxy(unittest.TestCase):
    def test_sets_loopback_validates_it_restarts_companion_and_checks_every_public_host(self):
        left = {}
        proc, calls, after = run(CONFIG.format(value="null"), leftovers=left)
        self.assertEqual(left, {}, "the backup is removed once the new config is proven")
        self.assertNotIn("could not restore", proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))
        validate = [c for c in calls if c.startswith("node ") and c.endswith(" validate")]
        self.assertEqual(validate, ["node [trustedProxies: loopback] /opt/companion/config-tool.js validate"])
        generated = [c for c in calls if c.startswith("node ") and c.endswith(" generate")]
        self.assertEqual(generated, ["node [trustedProxies: null] /opt/companion/config-tool.js generate",
                                     "node [trustedProxies: loopback] /opt/companion/config-tool.js generate"],
                         "the value Companion will get is read from config-tool before and after the edit")
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])
        self.assertLess(calls.index(validate[0]), calls.index("systemctl restart companion"))
        checks = handshakes(calls)
        self.assertEqual(checks, [f"http GET /trpc HTTP/1.1 host={h} origin=https://{h} upgrade=yes" for h in HOSTS])
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

    def test_replaces_an_empty_value_in_any_spelling(self):
        for value in ("null", "", "~", '""'):
            with self.subTest(value=value):
                proc, _calls, after = run(CONFIG.format(value=value))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(after, CONFIG.format(value="loopback"))

    def test_replaces_a_key_written_with_a_space_before_the_colon(self):
        proc, _calls, after = run(CONFIG.format(value="null").replace("trustedProxies: null", "trustedProxies : null"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))

    def test_reads_a_status_line_without_a_reason_phrase(self):
        proc, calls, _ = run(CONFIG.format(value="loopback"), ws=("101-bare",), tries=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(systemctl(calls), [])

    def test_waits_for_a_slow_handshake_instead_of_calling_it_dead(self):
        proc, calls, _ = run(CONFIG.format(value="loopback"), ws_delay=2.5, tries=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(systemctl(calls), [])


class LeavesItAlone(unittest.TestCase):
    def test_leaves_config_and_companion_alone_when_loopback_is_already_listed(self):
        for value in ("loopback", '"loopback"', "10.77.8.0/24, loopback", "loopback;10.77.8.0/24",
                      "loopback  # tunnel"):
            with self.subTest(value=value):
                config = CONFIG.format(value=value)
                proc, calls, after = run(config)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(after, config)
                self.assertEqual(systemctl(calls), [])
                self.assertFalse([c for c in calls if c.endswith(" validate")])
                self.assertEqual(len(handshakes(calls)), len(HOSTS))

    def test_restarts_companion_once_when_its_running_config_is_older_than_the_file(self):
        config = CONFIG.format(value="loopback")
        proc, calls, after = run(config, ws=("403", "101"), tries=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])
        self.assertEqual([c for c in calls if c.endswith(" validate")],
                         ["node [trustedProxies: loopback] /opt/companion/config-tool.js validate"],
                         "the file is validated before Companion is restarted on it")

    def test_never_restarts_a_stale_companion_onto_a_config_that_does_not_validate(self):
        config = CONFIG.format(value="loopback")
        proc, calls, after = run(config, ws=("403", "101"), validate_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), [])

    def test_refuses_to_override_a_proxy_list_someone_else_set(self):
        for value in ("10.77.8.0/24", "10.77.8.0/24, myloopback", "\n  10.77.8.0/24"):
            with self.subTest(value=value):
                config = CONFIG.format(value=value)
                proc, calls, after = run(config)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(after, config)
                self.assertIn("10.77.8.0/24", proc.stderr)
                self.assertEqual(systemctl(calls), [])

    def test_refuses_shapes_it_cannot_edit_safely(self):
        listed = CONFIG.format(value="").replace("trustedProxies: \n", "trustedProxies:\n{items}")
        shapes = {
            "duplicate key": CONFIG.format(value="loopback") + "trustedProxies: 10.0.0.0/8\n",
            "YAML list": listed.format(items="  - 10.0.0.0/8\n"),
            "YAML list at key indent": listed.format(items="- 10.0.0.0/8\n"),
            "YAML list after a comment": listed.format(items="  # lan\n\n  - 10.0.0.0/8\n"),
            "flow list with loopback": CONFIG.format(value="[loopback]"),
            "empty flow list": CONFIG.format(value="[]"),
        }
        for name, config in shapes.items():
            with self.subTest(shape=name):
                proc, calls, after = run(config)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(after, config)
                self.assertEqual(systemctl(calls), [])
                if name == "duplicate key":
                    self.assertIn("sets trustedProxies 2 times", proc.stderr)
                else:
                    self.assertIn("config-tool cannot read", proc.stderr)


class Fails(unittest.TestCase):
    def test_rolls_back_when_config_tool_reads_the_edit_as_something_else(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, reads_as="loopback 10.77.8.0/24")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), [])
        self.assertIn("loopback 10.77.8.0/24", proc.stderr)

    def test_never_restarts_into_a_config_config_tool_cannot_read(self):
        # launch.sh ignores a failing generate and starts Companion with no flags at all.
        config = CONFIG.format(value="loopback") + "badKey: [\n"
        proc, calls, after = run(config.replace("trustedProxies: loopback", "trustedProxies: [loopback]"), ws=("403",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(systemctl(calls), [])
        self.assertEqual(handshakes(calls), [])

    def test_fails_without_waiting_when_companion_still_refuses_after_one_restart(self):
        # A 403 from a freshly restarted Companion is its answer, not a start-up state.
        proc, calls, after = run(CONFIG.format(value="loopback"), ws=("403",), tries=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("403", proc.stderr)
        self.assertIn(HOSTS[0], proc.stderr)
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])
        self.assertEqual(len(handshakes(calls)), 2)
        self.assertEqual(after, CONFIG.format(value="loopback"))

    def test_rolls_back_at_once_when_companion_refuses_after_the_edit(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, ws=("403",), tries=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(len(handshakes(calls)), 1)
        self.assertEqual(systemctl(calls), ["systemctl restart companion", "systemctl restart companion"])

    def test_a_failed_restart_after_the_edit_still_restores_and_restarts_on_the_old_config(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, systemctl_fail_at=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), ["systemctl restart companion", "systemctl restart companion"])
        self.assertIn("restored", proc.stderr)

    def test_restores_the_config_and_restarts_companion_on_it_when_the_check_fails_after_the_edit(self):
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, ws=("close",), tries=3)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), ["systemctl restart companion", "systemctl restart companion"])
        self.assertEqual(len(handshakes(calls)), 3)
        second_restart = len(calls) - 1 - calls[::-1].index("systemctl restart companion")
        self.assertTrue(
            any(c.startswith("http GET / ") and c.endswith("upgrade=no") for c in calls[second_restart:]),
            "no readiness check after restoring",
        )
        self.assertIn("not answering", proc.stderr)
        self.assertIn("restored", proc.stderr)
        self.assertNotIn("did not come back", proc.stderr)

    def test_a_redirect_counts_as_companion_being_back(self):
        proc, _calls, after = run(CONFIG.format(value="null"), ws=("close",), http_answer="302")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, CONFIG.format(value="null"))
        self.assertIn("restored", proc.stderr)
        self.assertNotIn("did not come back", proc.stderr)

    def test_a_signal_during_the_restore_does_not_cut_it_short(self):
        # The second restart is the restore's; the deploy is killed right then.
        config = CONFIG.format(value="null")
        proc, calls, after = run(config, ws=("close",), systemctl_term_parent_at=2)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        second_restart = len(calls) - 1 - calls[::-1].index("systemctl restart companion")
        self.assertTrue(any(c.startswith("http GET / ") for c in calls[second_restart:]), "the restore still waits for Companion")
        self.assertIn("restored to its previous content", proc.stderr)

    def test_a_dead_session_on_the_final_report_keeps_the_proven_config(self):
        left = {}
        # stdout: config-tool's "is valid.", "restarting Companion...", then the report.
        proc, calls, after = run(CONFIG.format(value="null"), stdout_lines=2, leftovers=left)
        self.assertEqual(proc.returncode, 141, proc.stderr)
        self.assertEqual(after, CONFIG.format(value="loopback"))
        self.assertEqual(systemctl(calls), ["systemctl restart companion"])
        self.assertNotIn("restored", proc.stderr)
        self.assertEqual(left, {})

    def test_says_so_when_companion_does_not_come_back_after_the_restore(self):
        left = {}
        config = CONFIG.format(value="null")
        proc, _calls, after = run(config, ws=("close",), http_answer="503", leftovers=left)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertIn("did not come back", proc.stderr)
        self.assertEqual(left, {}, "the backup is removed once it is restored")

    def test_a_failed_backup_never_touches_the_live_config(self):
        config, left = CONFIG.format(value="null"), {}
        proc, calls, after = run(config, cp_fail_at=1, leftovers=left)
        self.assertEqual(left, {}, "the half-written backup is removed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, config)
        self.assertEqual(systemctl(calls), [])
        self.assertIn("could not back up", proc.stderr)
        self.assertNotIn("restore", proc.stderr)
        self.assertNotIn("backup kept", proc.stderr)

    def test_a_failed_restore_keeps_the_backup_and_never_truncates_the_config(self):
        original, left = CONFIG.format(value="null"), {}
        proc, calls, after = run(original, ws=("close",), cp_fail_at=2, tries=2, leftovers=left)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(after, CONFIG.format(value="loopback"), "the live config must stay whole")
        found = re.search(r"backup kept at (\S+)", proc.stderr)
        self.assertIsNotNone(found, proc.stderr)
        self.assertEqual(left, {os.path.basename(found.group(1)): original})
        self.assertNotIn("restored to its previous content", proc.stderr)

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
