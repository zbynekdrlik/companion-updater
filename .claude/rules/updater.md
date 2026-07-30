---
paths:
  - "updater/**"
  - "deploy.sh"
---

# companion-updater — mechanics and traps

## `update.sh` exits 0 when it installs NOTHING

`/usr/local/src/companionpi/update.sh stable` runs a version picker and, if the
picker writes no `/tmp/companion-version-selection`, prints `Skipping update`
and exits **0**. The picker prints `No matching stable build was found!` (also
exit 0) when its version whitelist rejects every published build.

**Exit status can never be the success signal.** Success = the version in
`/opt/companion/package.json` went UP. Anything else — unchanged, unreadable,
backwards — is an error. This is what `classify_outcome()` in
`updater/backend/src/update.rs` exists for; do not weaken it.

## The companion-pi checkout must be `git pull`ed FIRST

The picker lives in the checkout, and the checkout pins which Companion majors
it will install (the Apr 2026 copy whitelisted `^3 || ^4`, so it silently
refused every 5.x build). Upstream's own `companion-update` wrapper pulls
before running `update.sh`; anything that calls `update.sh` directly must do the
same, or it will install nothing forever with no visible error.

Upstream also rewrote the picker from `update-prompt/main.js` (node/yarn) to
`update-prompt/main.py` (python3) — do not assume the local file layout.

## Companion must be STOPPED across `update.sh`

`update.sh` does `rm -Rf /opt/companion` and re-extracts. Stop the service
first, start it after — and guarantee the start on every path. `CompanionDownGuard`
restarts it from `Drop` and leaves a marker in the state directory that
`reconcile_companion_state()` acts on at the next startup, which is what covers
a `kill -9` or the deploy's `systemctl stop companion-updater` tearing down the
cgroup mid-run. This box is a live A/V rig: leaving Companion down is the worst
possible outcome, worse than a failed update.

## The backend embeds the frontend — build order matters

`static_files.rs` has `include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist")` and
`frontend/dist` is gitignored, so **`trunk build` must run before anything
compiles the backend** — including `cargo clippy` and `cargo test`. A clean
checkout that goes straight to the backend fails with `proc macro panicked …
is not a directory`. `updater/build.sh` and the CI job both do it in that order.

## `deploy.sh` installs the binary BEFORE it triggers the upgrade

Otherwise the currently-installed (old) binary runs the upgrade and a fix to the
upgrade path can never take effect in the deploy that ships it. Keep that order.

## Verifying a deploy

The dashboard shows its own version (`Dashboard: v<semver>` from
`CARGO_PKG_VERSION`, id `updater-version`) — read it from the DOM, not from
curl, and compare with `updater/Cargo.toml`. `/api/status` reports the same
value in `updater_version`.

## Hosts

- `companion.lan` = companion-snv (x86_64), `companion-pp.lan` = the PP rig.
- Companion UI on `:8000`, updater dashboard on `:8081`.
- SSH user `newlevel` (password auth via `sshpass`; the value is not committed).
