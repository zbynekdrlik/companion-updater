#!/usr/bin/env python3
"""Run on a rig after a module deploy (as root: reads Companion's DB).

Usage: verify-deploy.py <module-id> <journal-since>

Every ENABLED connection of <module-id> must have logged the outcome of its
first health check since the restart: "Connected to ..." or "Resolume not
reachable: ..." (an Arena that is switched off is not a broken module).

Exit codes: 0 verified, 3 still waiting, 2 cannot verify (no DB, unknown
schema, journal unreadable), 4 errors logged for the module.
"""
import glob
import json
import re
import sqlite3
import subprocess
import sys

module_id, since = sys.argv[1], sys.argv[2]

dbs = sorted(glob.glob("/home/companion/.config/companion-nodejs/v*/db.sqlite"))
if not dbs:
    print("no Companion db.sqlite found")
    sys.exit(2)
db = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True)
rows = [json.loads(value) for (value,) in db.execute("select value from instances")]
if rows and not any("moduleId" in row for row in rows):
    print(f"{dbs[-1]}: instances have no moduleId field (unknown Companion DB schema)")
    sys.exit(2)
labels = [row["label"] for row in rows if row.get("moduleId") == module_id and row.get("enabled")]

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
