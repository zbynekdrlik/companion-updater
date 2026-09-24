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

Behaviour the tests pin down:

- The last press wins per list, even if that newer press then fails. Columns and decks have independent POST queues.
- `#` in an Arena name also matches the number Arena shows for it.
- An invalid layer group is refused and never falls back to the composition.

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
- Any failure (install, restart, verification, unknown DB schema) rolls back to `.old`.
- The verify script exits 0 (ok), 3 (waiting), 2 (cannot verify), or 4 (module errors logged; only checked when no connection exists).
- Both rigs launch Companion with `--extra-module-path /opt/companion-module-dev`. The module lives in `/opt/companion-module-dev/resolume-simple`, and connections use module version id `dev`, so version bumps never break a connection's pin.
- Companion must be restarted to load a new or changed module; the script does it.
- Resolume snv: `10.77.9.201`, webserver 8090, OSC input 7002. The buttons use layer group 2 ("G# Kontent") names.
- The songs Resolume is `songs-snv.lan` = 10.77.9.212. The AbleSet trigger selects a deck by `$(AbleSet:activeSongName)`.
- Companion's "Add connection" list shows `<manufacturer>: <product>` and MERGES equal names. With products `["Arena"]` the module was invisible next to the official `Resolume: Arena`, so ours is `Resolume: Arena Simple` (guarded by `lib/package.test.js`). Connections are created in the web UI (MCP cannot create them).
- MCP `create_button` does not work on Companion 5.0.6 (returns `controlId: null`). Edit existing buttons with `update_button`, and press them through the HTTP API: `POST http://<host>:8000/api/location/<page>/<row>/<col>/press`.
