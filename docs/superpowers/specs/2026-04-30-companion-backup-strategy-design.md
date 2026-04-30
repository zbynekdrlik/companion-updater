# Companion Backup Strategy — Off-Machine Backups + Upgrade Safety Gate

## Problem

On 2026-04-29, Companion's automatic v4.2.6 → v4.3.1 upgrade silently dropped buttons (LIGHTS 03 confirmed missing; possibly more). The loss went unnoticed for ~36 hours until the user happened to use a missing button. Two structural issues made this incident bad:

1. **No off-machine backups** — all backups (hourly `.companionconfig` exports) live on the same SSD as the live database. A disk failure or `rm -rf` mistake takes both with it.
2. **No detection of silent data loss** — the upgrade reported success even though it dropped data. We had no automated comparison of pre/post state.

## Goal

Add two independent safety nets that together prevent (and survive) future incidents like this one:

1. **Off-machine GitHub backups** — push hourly `.companionconfig` exports to a private GitHub repo, keeping the last 30 days of history.
2. **Upgrade safety gate** — wrap every Companion upgrade with a pre/post snapshot comparison. If counts drop, automatically roll back and surface the failure.

## Scope

- **In scope:**
  - New private GitHub repo `zbynekdrlik/companion-backups`.
  - Bash script + systemd timer on each Companion host (`companion.lan`, `companion-pp.lan`) to push backups.
  - New `safety` module in the existing Rust `companion-updater` crate that wraps `update.sh` calls.
  - Update the `companion-updater` SSE protocol to emit safety events.
  - Update `deploy.sh` to call the updater's HTTP endpoint for upgrades (instead of running `update.sh` directly), so the safety gate covers all upgrade paths.
- **Out of scope:**
  - Detecting individual button content changes (only counts are checked).
  - Real-time backup on every Companion edit (still hourly).
  - Backups for any other data on these hosts (only Companion).

## Component 1: GitHub backup pusher

### Repository layout

Private repo `zbynekdrlik/companion-backups`:

```
companion-backups/
├── companion-snv/
│   ├── latest.companionconfig                       (overwritten when changed)
│   └── history/
│       ├── backup-2026-04-30-1559.companionconfig
│       ├── backup-2026-04-30-1659.companionconfig
│       └── ...                                      (last 30 days, hourly)
├── companion-pp/
│   └── (same layout)
└── README.md
```

Filenames mirror Companion's own `backup-YYYY-MM-DD-HHMM.companionconfig` naming. Git history additionally provides a per-commit timeline; users can browse either GitHub's directory listing or the commit log.

### Host-side pusher

**Files installed on each Companion host:**

- `/usr/local/bin/companion-backup-push.sh` — bash script (mode 0755).
- `/etc/systemd/system/companion-backup-push.service` — systemd unit (Type=oneshot).
- `/etc/systemd/system/companion-backup-push.timer` — systemd timer firing at minute 1 of every hour (1 minute after Companion's hourly backup writes its file at minute 0).
- `/var/lib/companion-backup/repo/` — local clone of the GitHub repo (working tree).
- `/root/.ssh/companion_backup_id_ed25519` — deploy key with read+write access to the backup repo only (mode 0600, owned by root).

**Script logic (`companion-backup-push.sh`):**

1. Resolve machine identity from `hostname` (`companion-snv` if hostname starts with `companion-snv`, `companion-pp` if hostname starts with `linux-pp` or `companion-pp`). Stored in env var `MACHINE` (passed via systemd unit). Fail loudly if unknown.
2. Find the most recent `.companionconfig` in `/home/companion/.config/companion-nodejs/v4.*/backups/` (latest mtime across version subdirs).
3. Hash it (sha256). Compare with `/var/lib/companion-backup/last-pushed.sha256`.
4. If hash unchanged → exit 0 silently.
5. If changed:
   - `cd /var/lib/companion-backup/repo`
   - `git pull --ff-only origin main` (rebase against any remote changes; abort if conflict)
   - Copy file to `${MACHINE}/latest.companionconfig` and `${MACHINE}/history/$(basename file)`
   - Delete files in `${MACHINE}/history/` older than 30 days (`find ... -mtime +30 -delete`)
   - `git add ${MACHINE}/`
   - `git commit -m "Hourly backup ${ISO_TIMESTAMP} [${MACHINE}]"`
   - `GIT_SSH_COMMAND="ssh -i /root/.ssh/companion_backup_id_ed25519 -o StrictHostKeyChecking=no" git push origin main`
   - On push success → write the hash to `last-pushed.sha256`.
6. All output goes to journald via systemd (`tee /var/log/companion-backup-push.log` not used).

**Systemd unit file:**

```ini
[Unit]
Description=Push Companion backup to GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/local/bin/companion-backup-push.sh
StandardOutput=journal
StandardError=journal
```

**Systemd timer:**

```ini
[Unit]
Description=Hourly Companion backup push

[Timer]
OnCalendar=*:01:00
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` ensures missed runs (machine offline at the hour) trigger as soon as the machine comes back, exactly once.

### Error handling

- If `git pull` finds conflicts → abort, log error, exit 1. The script does not auto-resolve. Conflicts should be impossible in practice (each machine writes its own subdirectory) but a manual edit by a human could create one.
- If `git push` fails (network down, GitHub outage, auth failure) → log error, exit 1. Do NOT update `last-pushed.sha256` so the next run retries the same file.
- Systemd surfaces failed timer runs in `systemctl status companion-backup-push.timer` and `systemctl --failed`.

### Deploy key setup

Procedure (run once per machine, manually or via a setup script):

```bash
# On each Companion host
sudo ssh-keygen -t ed25519 -N '' -f /root/.ssh/companion_backup_id_ed25519 \
    -C "companion-backup-push@$(hostname)"
sudo cat /root/.ssh/companion_backup_id_ed25519.pub
# → Add this public key as a Deploy Key (read+write enabled) on the
#   zbynekdrlik/companion-backups GitHub repo.
sudo git clone --depth 1 -c core.sshCommand="ssh -i /root/.ssh/companion_backup_id_ed25519" \
    git@github.com:zbynekdrlik/companion-backups.git \
    /var/lib/companion-backup/repo
sudo git -C /var/lib/companion-backup/repo config core.sshCommand \
    "ssh -i /root/.ssh/companion_backup_id_ed25519 -o StrictHostKeyChecking=no"
```

The deploy key approach (rather than a personal access token) limits blast radius: the key works only on the backup repo, has no access to other repos, and rotates by replacing the deploy key.

## Component 2: Upgrade safety gate

### Where it lives

Inside the existing `companion-updater` Rust crate (deployed as a systemd service on each host). The updater already runs `update.sh stable` via `tokio::process::Command` in `updater/backend/src/update.rs`. We add a new module that wraps that call with safety hooks.

### Files affected

- Create: `updater/backend/src/safety.rs` — pre/post snapshot, parsing, count comparison, restore.
- Modify: `updater/backend/src/update.rs` — call into `safety` around `update.sh`.
- Modify: `updater/backend/src/main.rs` — add new SSE event types to the response stream, add module declaration.
- Modify: `updater/frontend/src/components/progress_log.rs` — render safety events with distinct styling.
- Modify: `deploy.sh` — replace `remote "sudo bash /usr/local/src/companionpi/update.sh stable"` with an HTTP call to `companion-updater`'s update endpoint, so deploys also flow through the safety gate.

### State directory

`/var/lib/companion-updater/` (created by the systemd unit's `StateDirectory=companion-updater`). Holds:

- `pre-upgrade.companionconfig` — most recent pre-upgrade snapshot (overwritten each upgrade).
- `pre-upgrade-archive/<timestamp>.companionconfig` — kept for 7 days, audit trail.

### Counts compared

The pre and post `.companionconfig` files are JSON. The safety module parses them and computes:

- `connections` — number of entries in `instances` (the connections table).
- `pages_with_content` — count of pages in `pages` whose `controls` object has at least one non-empty row.
- `buttons` — total count of bank entries across all pages (sum of all controls).
- `triggers` — number of entries in `triggers`.

These keys are stable across Companion v4.x. If a future Companion release changes the export format, the safety module fails closed (refuses upgrade with a clear error) rather than silently miscounting.

### Flow

```
Update request received (from web UI button click or HTTP API call from deploy.sh)
  │
  ├─ Cooldown / running check (existing logic, unchanged)
  │
  ├─ SAFETY: pre-upgrade snapshot
  │    GET http://127.0.0.1:8000/int/export/full
  │    save bytes → /var/lib/companion-updater/pre-upgrade.companionconfig
  │    archive copy → /var/lib/companion-updater/pre-upgrade-archive/<ts>.companionconfig
  │    parse + compute pre-counts
  │    SSE event: { type: "safety_pre", counts: { connections, pages_with_content, buttons, triggers } }
  │
  ├─ Run sudo bash /usr/local/src/companionpi/update.sh stable
  │    SSE events: { type: "progress", message: "<line>" }  (existing)
  │
  ├─ sudo systemctl restart companion
  │    Poll http://127.0.0.1:8000/api/version every 2s for up to 60s until 200 OK
  │
  ├─ SAFETY: post-upgrade snapshot
  │    Same GET + parse, compute post-counts
  │    SSE event: { type: "safety_post", counts: { ... } }
  │
  ├─ Compare counts:
  │    if any of (connections, pages_with_content, buttons, triggers) decreased:
  │      → SAFETY: rollback
  │
  ├─ Rollback (if triggered):
  │    sudo systemctl stop companion
  │    POST pre-upgrade.companionconfig to http://127.0.0.1:8000/int/import/full
  │      (or, if Companion is stopped, replace v4.X/db.sqlite directly using a pre-upgrade backup)
  │      → MUST verify which method actually works in v4.3.1 during the implementation phase.
  │      → Default plan: start Companion first, then call its import API with "Full Reset & Import"
  │        on the pre-upgrade file, mirroring the manual restore done on 2026-04-30.
  │    sudo systemctl restart companion
  │    Poll for healthy
  │    SSE event: { type: "safety_rollback", message: "Data loss detected (lost N buttons, M connections); rolled back to pre-upgrade state.", lost: { ... } }
  │    Update cooldown timer normally (the upgrade attempt was processed, even if reverted)
  │
  └─ Success path (counts ≥ pre):
     SSE event: { type: "complete", message: "Update complete. Now running v<X>", diff: { connections: 0, ... } }
```

### SSE event additions

```rust
pub enum UpdateEvent {
    Progress { message: String },                    // existing
    Complete { message: String },                    // existing — gains optional diff field
    Error { message: String },                       // existing
    SafetyPre { counts: Counts },                    // new
    SafetyPost { counts: Counts },                   // new
    SafetyRollback { message: String, lost: Counts },// new
}

pub struct Counts {
    pub connections: usize,
    pub pages_with_content: usize,
    pub buttons: usize,
    pub triggers: usize,
}
```

### Frontend rendering

`progress_log.rs` styles new event types with distinct CSS classes:

- `safety_pre` — neutral info color, "Pre-upgrade snapshot: 41 connections, 99 pages, ... buttons"
- `safety_post` — same for post
- `safety_rollback` — red alert banner, larger text, plus the lost counts

`update_button.rs` treats `safety_rollback` as a terminal failure event (closes EventSource, button returns to enabled, but with a red flash to draw attention).

### deploy.sh changes

Replace the existing step that runs `update.sh` directly via SSH:

```bash
# OLD
remote "sudo bash /usr/local/src/companionpi/update.sh stable"
remote "sudo systemctl restart companion"
```

with an HTTP call that goes through the updater's safety gate:

```bash
# NEW — relies on the updater being installed and running
curl -fsS --max-time 1800 -N "http://${COMPANION_HOST}:8081/api/update/stream" | tee /dev/stderr | grep -q '"type":"complete"' || {
  echo "ERROR: update via companion-updater failed or rolled back"
  exit 1
}
```

(Exact form depends on SSE handling in bash — implementation may use `curl` with a timeout and grep for specific terminal events. The plan task will pin this down.)

## Verification

1. **Backup pusher**:
   - On both companion.lan and companion-pp.lan, after deploy, the timer is `active` and `enabled`.
   - First successful run produces a commit on the GitHub repo with the latest `.companionconfig` for that machine.
   - Second run (within an hour, no Companion changes) is a no-op — `last-pushed.sha256` unchanged, no new commit.
   - After 30 days of running, files in `history/` older than 30 days are deleted by the script.
2. **Upgrade safety gate**:
   - Manually trigger an upgrade from a state where Companion is already at the latest stable. Expected: `safety_pre` and `safety_post` events fire, counts equal, `complete` event with `diff.{connections,buttons,triggers,pages_with_content}` all `0`.
   - Manually corrupt `v4.X/db.sqlite` with a smaller subset (delete some triggers), trigger an upgrade. Expected: rollback fires, original db.sqlite restored, web UI returns to the original state.

## Rollback

Both components are independent of Companion itself. Disabling either has no effect on Companion's operation:

- `sudo systemctl disable --now companion-backup-push.timer` stops backups.
- The Rust updater binary can be reverted to a version without the safety module by redeploying an older binary; existing pre-upgrade snapshots remain on disk.

## Files to be created or modified in this repo

```
updater/backend/src/safety.rs                                       (new)
updater/backend/src/update.rs                                       (modify)
updater/backend/src/main.rs                                         (modify)
updater/frontend/src/components/progress_log.rs                     (modify)
updater/companion-updater.service                                   (modify — add StateDirectory)
host/companion-backup-push.sh                                       (new)
host/companion-backup-push.service                                  (new)
host/companion-backup-push.timer                                    (new)
host/setup-backup-key.sh                                            (new — one-time setup helper)
deploy.sh                                                           (modify)
README.md                                                           (modify)
```
