#!/usr/bin/env python3
"""Run on a rig after a module deploy (as root: reads Companion's DB).

Usage: verify-deploy.py <module-id> <journal-since>

Every ENABLED connection of <module-id> must have logged the outcome of its
first health check since the restart: "Connected to ..." or "Resolume not
reachable: ..." (an Arena that is switched off is not a broken module).

Exit codes: 0 verified, 3 still waiting, 2 cannot verify (no DB, unknown
schema, journal unreadable), 4 errors logged for the module. Exit 4 is only
checked when the module has NO enabled connection; with connections, a module
that crashes never logs a health outcome, so the deploy times out and rolls
back instead.
"""
import glob
import json
import re
import sqlite3
import subprocess
import sys

module_id, since = sys.argv[1], sys.argv[2]

CONFIG = "/home/companion/.config/companion-nodejs"


def active_db():
    """The DB of the RUNNING Companion (v<major>.<minor>), not just the newest directory."""
    try:
        with open("/opt/companion/package.json") as f:
            major, minor = json.load(f)["version"].split(".")[:2]
        path = f"{CONFIG}/v{major}.{minor}/db.sqlite"
        if glob.glob(path):
            return path
    except (OSError, ValueError, KeyError):
        pass
    def version_key(path):
        return [int(n) for n in re.findall(r"/v(\d+)\.(\d+)/", path)[0]]
    dbs = sorted(glob.glob(f"{CONFIG}/v*.*/db.sqlite"), key=version_key)
    return dbs[-1] if dbs else None


db_path = active_db()
if not db_path:
    print("no Companion db.sqlite found")
    sys.exit(2)
db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
rows = []
for (value,) in db.execute("select value from instances"):
    try:
        row = json.loads(value)
    except (TypeError, ValueError):
        continue
    if isinstance(row, dict):
        rows.append(row)
if rows and not any("moduleId" in row for row in rows):
    print(f"{db_path}: instances have no moduleId field (unknown Companion DB schema)")
    sys.exit(2)
labels = [row.get("label") for row in rows if row.get("moduleId") == module_id and row.get("enabled")]
if any(not label for label in labels):
    print(f"an enabled {module_id} connection has no label in {db_path}")
    sys.exit(2)

journal = subprocess.run(
    ["journalctl", "-u", "companion", "--since", since, "--no-pager", "-o", "cat"],
    capture_output=True, text=True,
)
if journal.returncode != 0:
    print(f"journalctl failed: {journal.stderr.strip()}")
    sys.exit(2)
lines = re.sub(r"\x1b\[[0-9;]*m", "", journal.stdout).splitlines()

if not labels:
    bad = [line for line in lines if module_id in line and re.search(r"error|fail|crash", line, re.I)]
    if bad:
        print("\n".join(bad[:10]))
        sys.exit(4)
    print(f"no enabled {module_id} connection; no load errors logged")
    sys.exit(0)

pending = []
for label in labels:
    outcome = re.compile(rf"Instance/Connection/{re.escape(label)}\s+(Connected to .*|Resolume not reachable: .*)")
    hits = [m.group(1) for m in map(outcome.search, lines) if m]
    if hits:
        print(f"{label}: {hits[-1][:160]}")
    else:
        pending.append(label)
if pending:
    print("waiting for: " + ", ".join(pending))
    sys.exit(3)
sys.exit(0)
