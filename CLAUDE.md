# companion / companion-updater

Bitfocus Companion runs as a native systemd service (companion-pi) on the church
A/V machines. This repo holds the update dashboard (`updater/`, Rust + Leptos
WASM), the host udev rules and backup pusher (`host/`), and `deploy.sh`.

## Playbook router

- updater + deploy (update.sh traps, build order, deploy ordering, hosts) → `.claude/rules/updater.md` (auto-loads on its `paths:`)

## Local Build Policy

<!-- airuleset:local-builds=allowed -->

**Local builds (Tier 1) ENABLED.** Full `cargo build --release` / `trunk build` / `cargo test` allowed.
Reason: `deploy.sh` builds the binary locally and installs it over SSH — the dev machine IS the build target for this project.

## Always applies

- Deploy: `./deploy.sh` (override with `COMPANION_HOST=companion-pp.lan ./deploy.sh`). It builds locally and installs over SSH; there is no deploy pipeline.
- `companion/` is the retired Docker setup, kept for reference only — do not extend it.
- Never claim an upgrade succeeded from an exit status. See the updater rule.
