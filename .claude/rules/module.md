---
paths:
  - "module/**"
---

# resolume-simple — our own Companion module for Resolume

## Why it exists (do not "simplify" it back to WebSocket)

- `resolume-arena` (official) in WebSocket mode stalls ~10 s after every column change on our big composition. Arena pushes the whole 14.5 MB composition to every WS client after each change, and the module pegs a core parsing it, so actions time out.
- `generic-osc` resolves the hostname once at init, so a DNS hiccup at boot leaves it silently sending nowhere.
- Evidence and measurements are on companion-updater#10.

So the module only uses tiny REST calls:

- `GET /api/v1/composition[/layergroups/{g}]/columns/{n}` (about 1.6 KB each) and `GET /api/v1/composition/decks/{n}` (about 360 B each) for name lookup. Both answer 404 after the last item.
- `POST …/columns/{n}/connect` and `POST …/decks/{n}/select` (204) to act.
- `GET /api/v1/product` for health.
- Plain OSC goes through Companion's `oscSend`.

Never add a call that fetches `/api/v1/composition` itself. The allowlist `ALLOWED_REQUEST` in `testing/fake-arena.js` is asserted by the tests, so a new endpoint has to be added there deliberately.

Timeouts (measured, not guessed — #10, 2026-10-01): Arena 7.28 on companion-snv stalled its webserver 708× in a week (median 3 s, p90 8 s) while in use, but answered in about 14 ms when idle. So:

- Presses (and name lookups) wait up to `ACTION_TIMEOUT_MS` = 5 s.
- The `/product` health check uses 2 s.
- The status turns red only after 2 consecutive failed checks.

Do not shorten these without new measurements.

Behaviour the tests pin down:

- The last press wins per list, even if that newer press then fails. Columns and decks have independent POST queues.
- `#` in an Arena name also matches the number Arena shows for it.
- An invalid layer group is refused and never falls back to the composition.
- "Connect column by number" (group + N) exists for rigs whose Arena was off at setup time (companion-pp). By-number and by-name presses on the same list share last-press-wins.

## Layout and tests

- Plain CommonJS, no build step, same shape as the `presenter` module. `lib/*.js` is pure logic with no `@companion-module/base` import. `index.js` is the Companion wrapper.
- Tests use a real local HTTP server shaped like Arena 7.27's REST API (`testing/fake-arena.js`). Do not mock the client.
- `index.test.js` loads `index.js` with only the SDK runtime (`InstanceBase`, `runEntrypoint`) stubbed.
- Only `index.js`, `lib/` (without tests), `companion/` and the production dependencies are deployed.
- `manifest.json` `version` must equal `package.json` `version`, and `runtime.apiVersion` must equal the pinned `@companion-module/base` (1.13.6 runs on both Companion 4.3.1 (pp) and 5.0.6 (snv)). `lib/package.test.js` enforces both.

## Deploy

- `COMPANION_PASS=… module/deploy.sh <host>` (companion.lan = snv, 100.101.72.101 = pp). It deploys only committed, pushed code.
- A remote EXIT trap always restarts Companion.
- NEVER leave a second copy of a module inside `/opt/companion-module-dev`. Companion loads every directory there, and a duplicate id silently wins: a `resolume-simple.old` backup in that directory made Companion run the old version. The fallback lives in `/opt/companion-module-backup/<id>`, and the verify script fails if the id appears more than once.
- A deploy that passes verification stamps `${DEST}/.verified`, and the backup copy is always a verified module. At install time a stamped `DEST` becomes the backup; an unstamped leftover from a half-failed deploy is simply replaced.
- Verification (`module/verify-deploy.py`, run as root on the rig, reading the DB of the RUNNING Companion version) requires every enabled resolume-simple connection to log the outcome of its first health check after the restart. Either `Connected to …` or `Resolume not reachable: …` counts, because a switched-off Arena is not a broken module.
- Any failure (install, restart, verification, unknown DB schema) rolls back to the backup in `/opt/companion-module-backup/<id>`.
- The duplicate-id check fails closed on purpose. If another directory with the same module id appears in `/opt/companion-module-dev` (for example a manual copy), every deploy fails verification and rolls back until someone removes that directory by hand; the verify output names it.
- The verify script exits 0 (ok), 3 (waiting), 2 (cannot verify), or 4 (module errors logged; only checked when no connection exists).
- Both rigs launch Companion with `--extra-module-path /opt/companion-module-dev`. The module lives in `/opt/companion-module-dev/resolume-simple`, and connections use module version id `dev`, so version bumps never break a connection's pin.
- Companion must be restarted to load a new or changed module; the script does it.
- Connection hosts are ALWAYS DNS names, never IPs (owner rule). The module resolves the name on every request. Resolume snv: `resolume.lan` (= resolume-snv.lan, 10.77.9.201), webserver 8090, OSC input 7002. The buttons use layer group 2 ("G# Kontent") names.
- The songs Resolume is `songs-snv.lan` = 10.77.9.212. The AbleSet trigger selects a deck by `$(AbleSet:activeSongName)`.
- Companion's "Add connection" list shows `<manufacturer>: <product>` and MERGES equal names. With products `["Arena"]` the module was invisible next to the official `Resolume: Arena`, so ours is `Resolume: Arena Simple` (guarded by `lib/package.test.js`). Connections are created in the web UI (MCP cannot create them).
- MCP `create_button` does not work on Companion 5.0.6 (returns `controlId: null`). Edit existing buttons with `update_button`, and press them through the HTTP API: `POST http://<host>:8000/api/location/<page>/<row>/<col>/press`.

## Reading the rigs' logs

- In `journalctl -u companion` a connection label is followed by an ANSI colour reset (`Instance/Connection/<label>\x1b[0m <message>`). So `grep 'Connection/resolume '` and `journalctl -g 'Connection/x '` match NOTHING. Strip the colour codes first (`sed 's/\x1b\[[0-9;]*m//g'`) and use `grep -a`.
- A week of journal is several million lines. Run the scan in the background with a generous timeout; a 60 s foreground call times out.
- Successful presses log at debug, so the journal shows only failures (`… failed`), skipped presses, and health changes.
- Companion restarts that show `Stopping companion.service … Started` come from deploys (e.g. SSH from dev2), not crashes; see companion-updater#7.

## companion-pp specifics

- Companion 5.0.6 since 2026-10-01. The active DB is `v5.0`; an old `v4.3/db.sqlite` is still on disk, so do not read it by mistake.
- The pp buttons use `connect_column_by_number` (group 2) because resolume-pp was off at migration time. The same buttons also drive cg Arena (`cg_resolume`, group 1 + layer 13/9 clips); leave those alone.
- Page 11 r3c0 is an old "reset connections" button pointing at connection ids that no longer exist.

