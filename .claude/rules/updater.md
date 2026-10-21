---
paths:
  - "updater/**"
  - "deploy.sh"
  - "host/**"
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

## Time sync

`deploy.sh` step 3 runs `host/ensure-timesync.sh`. It runs `timedatectl set-ntp true` and fails unless the clock reports `NTPSynchronized=yes`. timesyncd was disabled on both rigs out of the box (#19).

The NTP server comes from DHCP:

- pp uses `10.77.8.1`, its router.
- snv uses `strih.lan` (10.77.9.202), which itself runs about 0.9 s off.

"Synchronised" only means synchronised to that server. Measure the real offset with an SNTP query to `pool.ntp.org`, not with `timedatectl`.

## Web UI over Cloudflare (#26)

- **What serves what.**
  - `companion-snv.newlevel.media` and `companion-pp.newlevel.media` serve the Companion UI on `:8000`.
  - `companion-update-snv.newlevel.media` and `companion-update-pp.newlevel.media` serve the updater dashboard on `:8081`.
  - The older alias `companion.newlevel.media` → snv `:8000` also serves the snv Companion UI.
  - `companion.newlevel.media` and `companion-pp.newlevel.media` are both covered by the owner's hand-made Access app "newlevel" (policy "basic": the owner plus two people he chose; its own session and cookie settings). The tool only checks that app; it never changes it.
- **Login.** The managed hosts (companion-snv, companion-update-snv, companion-update-pp) are behind Cloudflare Access: one-time PIN to the owner's e-mail, reusable policy "Companion owner (OTP email) · claude", 730h session like the other newlevel apps (owner's choice of 2026-10-21; 120 days was offered and declined). A device logged in to any newlevel app gets in through the organisation's shared login without another PIN. The account has no other identity provider and no WARP.
- **Second layer.** Each ingress rule also has `originRequest.access.required` with the app's aud, so cloudflared on the rig itself rejects requests without a valid Access token. That covers a deleted app or a bypass policy. It does NOT cover a loosened allow policy (everyone, any PIN login, email_domain): those logins get a token with the same aud. That is why the policies themselves are checked to name people only.
- **Companion 5 refuses the UI WebSocket from the tunnel unless loopback is a trusted proxy.** Its tRPC upgrade handler rejects (403) a loopback client whose Host is not localhost/loopback, and also any Origin whose host differs from Host. cloudflared connects from loopback with the public Host, so the page loaded, `wss://…/trpc` got 502, and the UI sat on "connecting" while every 302 probe was green. Fix: `trustedProxies: loopback` in `/etc/companion/config.yaml` (launch.sh turns it into `--trusted-proxies`; update.sh's `config-tool init` never overwrites a set value; `config-tool validate` does NOT check the value's format). `host/ensure-companion-proxy.sh` sets it, restarts Companion only on change, and fails unless a loopback handshake with the public Host and Origin answers 101. deploy.sh runs it in step 7 and again after the upgrade. Side effect: Companion sees the real client IP from X-Forwarded-For instead of 127.0.0.1.
- **A 302 probe proves only the login gate.** The real-user check is a browser through the public URL: a temporary Access service token (`non_identity` policy) attached to the managed apps, Chromium sending `CF-Access-Client-Id/Secret` as extra headers, then the policy and token deleted and `plan` re-run clean. The covered hosts (companion-pp, companion.newlevel.media) belong to the owner's app and cannot be tested that way; their Companion is covered by the loopback handshake check.
- **The rig's `cloudflared.service` belongs to the presenter project** (tunnels presenter-prod / presenter-pp).
  - `cloudflared service install <token>` rewrites exactly that unit. That is how the companion tunnels sat dead unnoticed for weeks. Never use it for companion.
  - Companion runs its own `cloudflared-companion.service` (host/): `Restart=always`, DynamicUser, `TUNNEL_TOKEN` in `/etc/cloudflared-companion.env` (root 0600). It also has its own `--config /etc/cloudflared-companion.yml`, so cloudflared never falls back to the presenter's `/etc/cloudflared/config.yml`.
- **Cloudflare-side state is code: `host/cloudflare_companion.py`.**
  - `plan` (exit 3 = changes pending), `apply` (re-plans and must end clean), `status` (exit 1 = a tunnel is down).
  - It needs `COMPANION_ACCESS_EMAILS` = the owner's e-mail (see local memory); `install-token` needs it too, because it refuses unless `plan` is clean. Never commit e-mail addresses here (public repo).
  - Hostnames another Access app already covers (main domain OR extra destination: companion-pp is a destination of the owner's "newlevel" app) are only checked, never given a second app. A POST for a covered host answers 409. All apps are listed with paging, because a `domain=` filter misses extra destinations.
  - It writes in a safe order: Access policy and apps → alert → ingress → DNS. Every conflict is checked before the first write, so a hostname is never routed before its app exists.
  - The API token comes from `$CF_TOKEN_FILE`; the default is the `cloudflare-newlevel-full` key file in the secrets dir.
- **Run token.** `install-token <ssh-host>` picks the tunnel from the rig's `hostname` (companion-snv / linux-pp), so the snv token can never land on pp. It refuses unless `plan` is clean (Access in place), checks that the token's `t` is that tunnel, writes it over stdin, and restarts the unit.
- **`deploy.sh` step 7** (after the updater install, so the public dashboard always has its cross-site guard) installs the unit and FAILS when:
  - the token is missing (opt out with `COMPANION_TUNNEL=skip`);
  - the unit dies;
  - or the public URL does not answer `302` to `newlevelchurch.cloudflareaccess.com`.
  A `200` (public without login) stops the tunnel at once.
- **Updater dashboard CSRF.** `GET /api/update/stream` stops and upgrades Companion. A link on another site, or one opened from mail or chat, would otherwise ride the owner's Access cookie. So the updater answers 403 to `Sec-Fetch-Site: cross-site|same-site` and to any `Sec-Fetch-Mode: navigate` (`updater/backend/src/guard.rs`); router-level tests pin the wiring. Browsers send these headers only to the HTTPS public URLs; the LAN `http://<rig>:8081` has never had authentication. The managed apps also set SameSite=Lax.
- **Outage alert.** The notification "Companion tunnels health · claude" (`tunnel_health_event`, both tunnel ids, NO status filter) e-mails the owner on every health change. The status filter's value format is undocumented, and a wrong value would never fire. Proof that it fires: `GET /accounts/{acc}/alerting/v3/history` after a controlled stop.
- **Tooling without a Companion upgrade.** `COMPANION_UPGRADE=skip ./deploy.sh` deploys this repo's pieces (udev, time sync, backup pusher, updater, tunnel) and leaves Companion at its version. Without it, step 8 upgrades Companion to the latest stable.
