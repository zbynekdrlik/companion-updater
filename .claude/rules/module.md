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

- `GET /api/v1/composition[/layergroups/{g}]/columns/{n}` (about 1.6 KB each; 404 after the last column) for name lookup.
- `POST …/columns/{n}/connect` (204) to connect.
- `GET /api/v1/product` for health.
- Plain OSC goes through Companion's `oscSend`.

Never add a call that fetches `/api/v1/composition` itself; the test `never requests the whole composition` guards this.

## Layout and tests

- Plain CommonJS, no build step, same shape as the `presenter` module. `lib/*.js` is pure logic with no `@companion-module/base` import, so `node --test` can load it; `index.js` is the thin Companion wrapper.
- Tests use a real local HTTP server shaped like Arena 7.27's REST API. Do not mock the client.
- `manifest.json` `version` must equal `package.json` `version`, and `runtime.apiVersion` must equal the pinned `@companion-module/base` (1.13.6 runs on both Companion 4.3.1 (pp) and 5.0.6 (snv)). `lib/package.test.js` enforces both.

## Deploy

- `COMPANION_PASS=… module/deploy.sh <host>` (companion.lan = snv, 100.101.72.101 = pp). It deploys from committed code only.
- Both rigs launch Companion with `--extra-module-path /opt/companion-module-dev`. The module lives in `/opt/companion-module-dev/resolume-simple`, and connections use module version id `dev`, so version bumps never break a connection's pin.
- Companion must be restarted to load a new or changed module; the script does it.
- Resolume snv: `10.77.9.201`, webserver 8090, OSC input 7002. The buttons use layer group 2 ("G# Kontent") names.
- MCP `create_button` does not work on Companion 5.0.6 (returns `controlId: null`). Edit existing buttons with `update_button`, and press them through the HTTP API: `POST http://<host>:8000/api/location/<page>/<row>/<col>/press`.
