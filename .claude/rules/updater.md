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
  - `companion.newlevel.media` and `companion-pp.newlevel.media` are both covered by the owner's hand-made Access app "newlevel" (policy "basic"; its own session and cookie settings). The tool only checks that app; it never changes it.
- **Login.** Every Companion host lets in the same people: the owner's reusable Access policy "basic", his "mail security" (the people he named, by e-mail).
  - The managed hosts (companion-snv, companion-update-snv, companion-update-pp) have their own apps with "basic", the same policy as his "newlevel" app (owner 2026-10-22); `plan` notes if "newlevel" ever stops using it.
  - Trade-off: an edit the owner makes to "basic" for another app reaches the managed apps too, the updater dashboards (stop + upgrade Companion) included. `plan`/`apply`/`install-token` refuse a loosened "basic", but only when someone runs them.
  - The tool only checks "basic", never changes it: exactly one policy by that name, `allow`, an include made only of `email` / `email_list` (require/exclude may only narrow it, so they are left to the owner). Anything else stops the run before the first write. `plan` prints how many distinct e-mail addresses and lists it names, never the e-mails.
  - Until 2026-10-22 the tool made its own policy "Companion owner (OTP email) · claude" with a single e-mail, and so refused the other people in "basic". It was deleted by hand once (no app used it). The tool never creates, changes or deletes a policy.
  - One-time PIN by e-mail; the account has no other identity provider and no WARP. Apps carry a 730h session (owner's choice of 2026-10-21; 120 days was offered and declined), but "basic" sets its own 24h, and a policy-level session overrides the app's; `plan` prints a note.
  - One Cloudflare login (team newlevelchurch, 730h organisation session) then opens every app whose policy allows that identity, without another PIN; an app session that ends (24h) is renewed through that login without a PIN.
  - A browser logged in as someone NOT in an app's policy is asked to log in again.
  - The PIN page says it e-mailed a code even for an address the policy does not allow, but sends nothing. "No PIN arrived" usually means the address is not in "basic".
- **Second layer.** Each ingress rule also has `originRequest.access.required` with the app's aud, so cloudflared on the rig itself rejects requests without a valid Access token. That covers a deleted app or a bypass policy. It does NOT cover a loosened allow policy (everyone, any PIN login, email_domain): those logins get a token with the same aud. That is why the policies themselves are checked to name people only.
- **Companion 5 refuses the UI WebSocket from the tunnel unless loopback is a trusted proxy.** Its tRPC upgrade handler rejects (403) a loopback client whose Host is not localhost/loopback, and also any Origin whose host differs from Host. cloudflared connects from loopback with the public Host, so the page loaded, `wss://…/trpc` got 502, and the UI sat on "connecting" while every 302 probe was green. Fix: `trustedProxies: loopback` in `/etc/companion/config.yaml` (launch.sh turns it into `--trusted-proxies`; update.sh's `config-tool init` never overwrites a set value; `config-tool validate` does NOT check the value's format). `host/ensure-companion-proxy.sh` sets it and fails unless a loopback handshake with the public Host and Origin answers 101.
  - It reads the value Companion will get from config-tool `generate`, the snippet launch.sh sources, and never parses the YAML itself. It edits only an unset value, then requires `generate` to read back exactly `loopback`. Probed on companion-pp 2026-10-21:
    - null, empty, `~` and `""` are unset;
    - quotes and comments are dropped;
    - an indented next line is the value;
    - with a duplicate key, the first wins;
    - ANY list (`[x]`, `[]`, `- x`) is a config-tool error, and launch.sh then starts Companion with NO flags (no extra module path).
    The script never restarts onto a file config-tool cannot read.
  - It restarts Companion when it changed the file. It also restarts once, after `validate`, on a 403 when the file already gives loopback: a hand edit, or an earlier run that died, leaves Companion running the older config. A 403 from a Companion it restarted fails at once.
  - Anything failing after its edit puts the old config back (temp file plus mv) and restarts Companion on it. If the restore itself fails, the backup is kept and its path printed.
  - The handshake goes over bash `/dev/tcp` and reads only the status line. A 101 keeps the WebSocket open, so curl would wait out its whole `-m` on every success, and an answer slower than that would read as dead.
  - deploy.sh runs it in step 7, before the tunnel starts, when Companion already has its launch config. It runs again in step 9, after the upgrade (which creates that config on a pre-5.0 rig), whenever this deploy started the tunnel or one is running.
  - Side effect: Companion sees the real client IP from X-Forwarded-For instead of 127.0.0.1.
  - Design trade-off (2026-10-21): trusting loopback switches off Companion's DNS-rebinding guard for a browser running ON the rig. Companion applies that guard only to loopback, so any LAN browser already bypasses it. The alternative, ingress to the rig's LAN name, would tie the tunnel to LAN DNS and addressing. Kept.
- **A 302 probe proves only the login gate.** The real-user check is a browser through the public URL: a temporary Access service token (`non_identity` policy) attached to the managed apps, Chromium sending `CF-Access-Client-Id/Secret` as extra headers, then the policy and token deleted and `plan` re-run clean. The covered hosts (companion-pp, companion.newlevel.media) belong to the owner's app and cannot be tested that way; their Companion is covered by the loopback handshake check.
- **The rig's `cloudflared.service` belongs to the presenter project** (tunnels presenter-prod / presenter-pp).
  - `cloudflared service install <token>` rewrites exactly that unit. That is how the companion tunnels sat dead unnoticed for weeks. Never use it for companion.
  - Companion runs its own `cloudflared-companion.service` (host/): `Restart=always`, DynamicUser, `TUNNEL_TOKEN` in `/etc/cloudflared-companion.env` (root 0600). It also has its own `--config /etc/cloudflared-companion.yml`, so cloudflared never falls back to the presenter's `/etc/cloudflared/config.yml`.
- **The tunnel runs http2, not QUIC (#33).** `protocol: http2` in `host/cloudflared-companion.yml`. On QUIC (the default) the snv tunnel logged 423 `timeout: no recent network activity` in 7 days, each stall flipping its health and sending a mail (12 mails by 10:48 on 2026-10-31), while the presenter tunnel on http2 beside it logged none. Ping stayed at 0 % loss, so a ping does not rule out these stalls. Count mails with `GET /accounts/{acc}/alerting/v3/history` and compare `protocol=` and reconnects per unit in the journal.
- **Cloudflare-side state is code: `host/cloudflare_companion.py`.**
  - `plan` (exit 3 = changes pending), `apply` (re-plans and must end clean), `status` (exit 1 = a tunnel is down).
  - It needs `COMPANION_ALERT_EMAILS` = the outage alert's recipients (see local memory); the alert is part of the desired state. `install-token` needs it too, because it refuses unless `plan` is clean. Who may log in is "basic", not this variable. Never commit e-mail addresses here (public repo).
  - Hostnames another Access app already covers (main domain OR extra destination: companion-pp is a destination of the owner's "newlevel" app) are only checked, never given a second app. A POST for a covered host answers 409. All apps are listed with paging, because a `domain=` filter misses extra destinations.
  - `plan`/`apply` also read the presenter tunnels on the rigs (presenter-prod / presenter-pp, routing `:80` on 2026-10-21; `status` does not). They stop on any of these:
    - a rule reaching `:8000` or `:8081`;
    - anything that can reach any port: `bastion`, `socks-proxy`, or `bastionMode` / `proxyType: socks` on a rule or tunnel-wide;
    - WARP routing;
    - a non-URL service outside the allowlist (`http_status:*`, `hello_world`, `unix:`);
    - a service URL they cannot parse;
    - a neighbour that is missing or managed on the rig.
    Unchecked is not safe: Companion trusts every loopback client, so such a rule would publish it under the presenter's Access.
  - `hosts <rig> [--ui]` prints a rig's hostnames from `TUNNELS` (no token needed); deploy.sh takes them from there, so the tunnel map has one copy.
  - It writes in a safe order: Access apps → alert → ingress → DNS. Every conflict is checked before the first write, so a hostname is never routed before its app exists.
  - The API token comes from `$CF_TOKEN_FILE`; the default is the `cloudflare-newlevel-full` key file in the secrets dir.
- **Run token.** `install-token <ssh-host>` picks the tunnel from the rig's `hostname` (companion-snv / linux-pp), so the snv token can never land on pp. It refuses unless `plan` is clean (Access in place), checks that the token's `t` is that tunnel, writes it over stdin, and restarts the unit.
- **`deploy.sh` step 7** (after the updater install, so the public dashboard always has its cross-site guard) installs the unit and FAILS when:
  - the token is missing (opt out with `COMPANION_TUNNEL=skip`);
  - the unit dies;
  - or the public URL does not answer `302` to `newlevelchurch.cloudflareaccess.com`.
  A `200` (public without login) stops the tunnel at once. Step 7 (before the tunnel starts) and step 9 (after the upgrade) also fail when Companion refuses the UI WebSocket from the tunnel.
- **Updater dashboard CSRF.** `GET /api/update/stream` stops and upgrades Companion. A link on another site, or one opened from mail or chat, would otherwise ride the Access cookie of anyone in "basic". So the updater answers 403 to `Sec-Fetch-Site: cross-site|same-site` and to any `Sec-Fetch-Mode: navigate` (`updater/backend/src/guard.rs`); router-level tests pin the wiring. Browsers send these headers only to the HTTPS public URLs; the LAN `http://<rig>:8081` has never had authentication. The managed apps also set SameSite=Lax.
- **Outage alert.** The notification "Companion tunnels health · claude" (`tunnel_health_event`, both tunnel ids, NO status filter) e-mails `COMPANION_ALERT_EMAILS` on every health change. The status filter's value format is undocumented, and a wrong value would never fire. Proof that it fires: `GET /accounts/{acc}/alerting/v3/history` after a controlled stop. In history, `policy_id` is the dashed UUID while the policies list gives the same id WITHOUT dashes, so compare them normalised. `alert_body.new_status` is e.g. `TUNNEL_STATUS_TYPE_DOWN` / `TUNNEL_STATUS_TYPE_HEALTHY`. Observed 2026-10-21:
- The DOWN mail went out 3 s after a stop, and HEALTHY 20–25 s after the start.
- A 10-minute outage sent HEALTHY on recovery.
- A deploy (step 7 restarts the unit) sends DOWN + HEALTHY per rig.
- A DOWN for the same tunnel shortly after its previous DOWN alert was not sent: 33 s later, and also 18 min later at the pp deploy. So repeated DOWNs are deduplicated per tunnel for some undocumented window.
- To test it, stop a tunnel that has had no recent DOWN alert, and read the history.
- **Tooling without a Companion upgrade.** `COMPANION_UPGRADE=skip ./deploy.sh` deploys this repo's pieces (udev, time sync, backup pusher, updater, tunnel) and leaves Companion at its version. Without it, step 8 upgrades Companion to the latest stable.
- **Scene buttons run through page 10.** Pages 2 and 3 press page 10, and each page 10 button loops pages 11 to 20 and presses the same row/column. On snv, page 15 (lights) runs only while the Home Assistant switch `SVETLA 01` (`switch.sonoff_10022c5f8d`) is on (#36), and a new page 15 button needs the same `logic_if`.
- **`update_button` cannot put actions inside a `logic_if`.** Nested actions need a config DB edit with Companion stopped and a DB backup first. Pick the DB of the running version, not simply the newest `v5.*` directory (`active_db()` in `module/verify-deploy.py`).
