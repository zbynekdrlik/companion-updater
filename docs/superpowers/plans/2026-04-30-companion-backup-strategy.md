# Companion Backup Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add off-machine GitHub backups (hourly, 30-day history) and an upgrade safety gate that aborts/rolls back when Companion upgrades silently drop data.

**Architecture:** Two independent components. (1) A bash script + systemd timer on each Companion host pushes the latest `.companionconfig` to a private GitHub repo using a deploy key. (2) A new `safety` module in the existing `companion-updater` Rust binary takes pre/post snapshots of Companion's HTTP export, parses counts (connections, pages, buttons, triggers), and triggers a programmatic restore if any count decreases.

**Tech Stack:** bash, systemd timers, git, GitHub deploy keys, Rust (axum + tokio + reqwest + serde_json), Leptos.

**Spec:** `docs/superpowers/specs/2026-04-30-companion-backup-strategy-design.md`

**SSH access:** `sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@<host>` where `<host>` is `companion.lan` (SNV) or `companion-pp.lan` (PP).

**Branch policy:** Work on `dev` (already current).

---

## Pre-work: Bump workspace version

**Why:** This is a feature release; the workspace version on `dev` should be ahead of `main`.

- [ ] **Step 1: Bump updater workspace version**

In `/home/newlevel/devel/companion/updater/Cargo.toml`, change:

```toml
[workspace.package]
version = "0.1.0"
```

to:

```toml
[workspace.package]
version = "0.2.0-dev.1"
```

- [ ] **Step 2: Verify backend still builds**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater
```

Expected: clean build, no errors.

- [ ] **Step 3: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/Cargo.toml
git commit -m "Bump updater to 0.2.0-dev.1 for backup strategy feature"
```

---

## Pre-work 2: Create the private GitHub backup repo

**Why:** Both backup pusher hosts need this repo to exist before they can clone it.

- [ ] **Step 1: Create the repo via gh**

```bash
gh repo create zbynekdrlik/companion-backups \
  --private \
  --description "Off-machine hourly backups of Companion .companionconfig exports" \
  --confirm
```

Expected: repo created at `https://github.com/zbynekdrlik/companion-backups`.

- [ ] **Step 2: Initialize with a README**

```bash
mkdir -p /tmp/companion-backups-init
cd /tmp/companion-backups-init
git init -b main
cat > README.md <<'EOF'
# Companion Backups

Automated hourly backups of Bitfocus Companion configuration from production hosts.

## Layout

- `companion-snv/latest.companionconfig` — most recent backup from companion.lan (SNV).
- `companion-snv/history/` — last 30 days of hourly backups (older auto-deleted).
- `companion-pp/latest.companionconfig` — most recent backup from companion-pp.lan.
- `companion-pp/history/` — last 30 days of hourly backups.

## Source

The companion-pi auto-export at `/home/companion/.config/companion-nodejs/v4.X/backups/` on each host. Pushed by `companion-backup-push.timer` every hour at minute 1 (after Companion's own hourly backup runs at minute 0).

To restore from one of these files, use Companion's web UI → **Import / Export → Import configuration**.
EOF
mkdir -p companion-snv/history companion-pp/history
touch companion-snv/history/.gitkeep companion-pp/history/.gitkeep
git add .
git commit -m "Initial commit"
git remote add origin git@github.com:zbynekdrlik/companion-backups.git
git push -u origin main
cd /home/newlevel/devel/companion
rm -rf /tmp/companion-backups-init
```

Expected: repo on GitHub now has README and empty `companion-snv/`, `companion-pp/` skeletons.

- [ ] **Step 3: Verify repo is reachable**

```bash
gh repo view zbynekdrlik/companion-backups --json visibility,defaultBranchRef
```

Expected: `{"visibility":"PRIVATE","defaultBranchRef":{"name":"main"}}` (or similar).

---

## Phase A: Backup pusher (Component 1)

### Task A1: Investigate Companion's import API on companion-pp.lan

**Why:** The safety gate's rollback step (Task B6) needs to programmatically import a `.companionconfig`. The spec flags this as needing empirical verification. Doing this investigation now (before any code changes) lets us pin down the exact HTTP requests in subsequent tasks.

**Files:** none (research only).

- [ ] **Step 1: Confirm export endpoint shape**

```bash
curl -sSI "http://companion-pp.lan:8000/int/export/full" | head -10
```

Expected: HTTP 200 with `Content-Type: application/json` and `Content-Disposition: attachment; filename="...companionconfig"`. Note the actual content type and any auth headers required.

- [ ] **Step 2: Download a fresh export**

```bash
curl -sS -o /tmp/pp-export.companionconfig "http://companion-pp.lan:8000/int/export/full"
ls -la /tmp/pp-export.companionconfig
file /tmp/pp-export.companionconfig
head -c 200 /tmp/pp-export.companionconfig
```

Expected: file size > 100 KB, content starts with JSON `{"version":...,"type":"full"...}` or similar.

- [ ] **Step 3: Probe the import endpoint**

In a browser DevTools session against `http://companion-pp.lan:8000/import-export`, click "Import configuration" and select the file. Watch the Network tab for the actual request method, URL, and body shape. Capture as a `curl` command using "Copy as cURL".

Record: the URL path, HTTP method, multipart vs JSON body, any session/auth header.

- [ ] **Step 4: Test the captured curl form**

Re-run the captured curl against companion-pp.lan with the file from Step 2. Verify it works (idempotent — importing the same export over the same instance is a no-op functionally; counts stay equal).

Compare connection count before and after via `mcp__companion-pp__list_connections` (or via `curl http://companion-pp.lan:8000/api/connections` if that endpoint exists).

- [ ] **Step 5: Document findings in this plan**

The findings landed in **Task B2's note + step-1 implementation** (the original "Implementation TODO" block was replaced with concrete API details and a working Rust body). The headline points are:

- **Transport:** tRPC v10 over WebSocket at `ws://<host>:8000/trpc` — there is no HTTP `/int/import/...` route at all.
- **Sequence:** `importExport.prepareImport.start` → `uploadChunk` (base64, 64 KiB chunks) → `complete` (SHA-1 hex checksum) → `importExport.importFull` (with a `ResetConfig` of `"reset-and-import"` per section).
- **Auth:** none (Companion's admin server has no auth).
- **Companion must be running.** The endpoint is in-process; no out-of-band import path exists. The `db.sqlite` swap fallback is NOT used — keeping the upgrade safety gate in-process simplifies the design and avoids touching SQLite directly.
- **Idempotency: confirmed** by round-trip on companion-pp.lan v4.3.1 (export → re-import same file → re-export, counts unchanged: 39 instances / 99 pages / 43 triggers).

- [ ] **Step 6: Commit findings**

```bash
cd /home/newlevel/devel/companion
git add docs/superpowers/plans/2026-04-30-companion-backup-strategy.md
git commit -m "Pin down Companion import API for safety rollback"
```

---

### Task A2: Create backup pusher bash script

**Files:**
- Create: `host/companion-backup-push.sh`

- [ ] **Step 1: Write the script**

`/home/newlevel/devel/companion/host/companion-backup-push.sh`:

```bash
#!/bin/bash
set -euo pipefail

# Push the most recent Companion .companionconfig backup to the private
# GitHub backup repo. Runs hourly via systemd timer.
#
# Required state:
#   /var/lib/companion-backup/repo            — clone of the backup repo
#   /var/lib/companion-backup/last-pushed.sha256 — hash of last successful push
#   /root/.ssh/companion_backup_id_ed25519    — deploy key (read+write to the
#                                              backup repo only)
# Required env (provided by systemd unit):
#   MACHINE — "companion-snv" or "companion-pp"

REPO_DIR="/var/lib/companion-backup/repo"
HASH_FILE="/var/lib/companion-backup/last-pushed.sha256"
SSH_KEY="/root/.ssh/companion_backup_id_ed25519"
COMPANION_BACKUPS_GLOB="/home/companion/.config/companion-nodejs/v4.*/backups"

if [ -z "${MACHINE:-}" ]; then
  echo "ERROR: MACHINE env var not set" >&2
  exit 1
fi

# 1. Find the most recent .companionconfig across all version subdirs.
LATEST="$(find ${COMPANION_BACKUPS_GLOB} -maxdepth 1 -name '*.companionconfig' -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr | head -n1 | awk '{print $2}')"

if [ -z "${LATEST}" ] || [ ! -f "${LATEST}" ]; then
  echo "ERROR: no .companionconfig found under ${COMPANION_BACKUPS_GLOB}" >&2
  exit 1
fi

# 2. Hash compare with last-pushed.
NEW_HASH="$(sha256sum "${LATEST}" | awk '{print $1}')"
LAST_HASH=""
[ -f "${HASH_FILE}" ] && LAST_HASH="$(cat "${HASH_FILE}")"

if [ "${NEW_HASH}" = "${LAST_HASH}" ]; then
  echo "Backup unchanged (${NEW_HASH:0:12}); skipping push."
  exit 0
fi

# 3. Sync the repo before staging.
export GIT_SSH_COMMAND="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes"
cd "${REPO_DIR}"
git pull --ff-only origin main

# 4. Stage the new file as both latest and a timestamped history entry.
mkdir -p "${MACHINE}/history"
cp "${LATEST}" "${MACHINE}/latest.companionconfig"
cp "${LATEST}" "${MACHINE}/history/$(basename "${LATEST}")"

# 5. Prune history older than 30 days.
find "${MACHINE}/history" -name '*.companionconfig' -mtime +30 -delete

# 6. Stage, commit, push.
git add "${MACHINE}"
if git diff --cached --quiet; then
  echo "No staged changes after copy (file already in tree); recording hash and exiting."
  echo "${NEW_HASH}" > "${HASH_FILE}"
  exit 0
fi

ISO="$(date -Iseconds)"
git commit -m "Hourly backup ${ISO} [${MACHINE}]"
git push origin main

echo "${NEW_HASH}" > "${HASH_FILE}"
echo "Pushed backup ${NEW_HASH:0:12} for ${MACHINE}."
```

- [ ] **Step 2: Validate syntax**

```bash
bash -n /home/newlevel/devel/companion/host/companion-backup-push.sh
```

Expected: no output (clean parse).

- [ ] **Step 3: Run shellcheck if available**

```bash
which shellcheck && shellcheck /home/newlevel/devel/companion/host/companion-backup-push.sh || echo "shellcheck not installed; skipped"
```

Expected: zero issues, or `shellcheck not installed`.

- [ ] **Step 4: Commit**

```bash
cd /home/newlevel/devel/companion
chmod +x host/companion-backup-push.sh
git add host/companion-backup-push.sh
git commit -m "Add backup pusher script for hourly GitHub sync"
```

---

### Task A3: Create systemd unit and timer

**Files:**
- Create: `host/companion-backup-push.service`
- Create: `host/companion-backup-push.timer`

- [ ] **Step 1: Write the service unit**

`/home/newlevel/devel/companion/host/companion-backup-push.service`:

```ini
[Unit]
Description=Push Companion backup to GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
EnvironmentFile=/etc/default/companion-backup-push
ExecStart=/usr/local/bin/companion-backup-push.sh
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 2: Write the timer unit**

`/home/newlevel/devel/companion/host/companion-backup-push.timer`:

```ini
[Unit]
Description=Hourly Companion backup push

[Timer]
OnCalendar=*:01:00
Persistent=true
Unit=companion-backup-push.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Validate with systemd-analyze**

```bash
systemd-analyze verify /home/newlevel/devel/companion/host/companion-backup-push.service 2>&1 | grep -v 'EnvironmentFile=/etc/default/companion-backup-push.*not exist' || true
systemd-analyze verify /home/newlevel/devel/companion/host/companion-backup-push.timer 2>&1
```

Expected: no errors except the missing `EnvironmentFile` warning (it doesn't exist on the dev machine — that's fine, it's deployed to the targets).

- [ ] **Step 4: Commit**

```bash
cd /home/newlevel/devel/companion
git add host/companion-backup-push.service host/companion-backup-push.timer
git commit -m "Add systemd unit and timer for backup pusher"
```

---

### Task A4: Create one-time deploy-key setup helper

**Why:** Each Companion host needs a unique deploy key. This script automates the per-host setup so we don't have to remember the steps.

**Files:**
- Create: `host/setup-backup-key.sh`

- [ ] **Step 1: Write the helper**

`/home/newlevel/devel/companion/host/setup-backup-key.sh`:

```bash
#!/bin/bash
set -euo pipefail

# One-time setup of the Companion backup pusher on a single host.
# Must be run AS ROOT on the target host.
#
# Steps:
#   1. Generate a deploy keypair at /root/.ssh/companion_backup_id_ed25519.
#   2. Print the public key for manual addition to the GitHub repo's
#      Deploy Keys (read+write).
#   3. Wait for confirmation, then clone the repo.
#   4. Write /etc/default/companion-backup-push with the MACHINE identity.
#   5. Enable + start the timer.

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

REPO_URL="git@github.com:zbynekdrlik/companion-backups.git"
KEY_PATH="/root/.ssh/companion_backup_id_ed25519"
REPO_DIR="/var/lib/companion-backup/repo"
ENV_FILE="/etc/default/companion-backup-push"
HOSTNAME_LOWER="$(hostname | tr '[:upper:]' '[:lower:]')"

# Determine MACHINE identity from hostname.
case "${HOSTNAME_LOWER}" in
  companion-snv*|*-snv|snv*) MACHINE="companion-snv" ;;
  companion-pp*|linux-pp*|*-pp|pp*) MACHINE="companion-pp" ;;
  *)
    echo "ERROR: cannot determine MACHINE from hostname '${HOSTNAME_LOWER}'." >&2
    echo "       Override by exporting MACHINE before running this script." >&2
    [ -z "${MACHINE:-}" ] && exit 1
    ;;
esac
echo "MACHINE = ${MACHINE}"

# Generate keypair if not present.
if [ ! -f "${KEY_PATH}" ]; then
  install -d -m 0700 /root/.ssh
  ssh-keygen -t ed25519 -N '' -f "${KEY_PATH}" -C "companion-backup-push@$(hostname)"
  echo
  echo "=== Public key (add as Deploy Key with WRITE access on the backup repo) ==="
  cat "${KEY_PATH}.pub"
  echo "==========================================================================="
  echo "Visit: https://github.com/zbynekdrlik/companion-backups/settings/keys"
  echo
  read -rp "Press ENTER once the key is added on GitHub..." _
fi

# Clone the repo.
install -d -m 0700 /var/lib/companion-backup
if [ ! -d "${REPO_DIR}/.git" ]; then
  GIT_SSH_COMMAND="ssh -i ${KEY_PATH} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes" \
    git clone "${REPO_URL}" "${REPO_DIR}"
  git -C "${REPO_DIR}" config core.sshCommand \
    "ssh -i ${KEY_PATH} -o StrictHostKeyChecking=no -o IdentitiesOnly=yes"
  git -C "${REPO_DIR}" config user.name "companion-backup-push"
  git -C "${REPO_DIR}" config user.email "companion-backup-push@$(hostname).local"
fi

# Write the environment file.
cat > "${ENV_FILE}" <<EOF
MACHINE=${MACHINE}
EOF
chmod 0644 "${ENV_FILE}"

# Reload systemd if the units are already in place; enable + start timer.
systemctl daemon-reload
if systemctl list-unit-files companion-backup-push.timer --no-legend | grep -q '.'; then
  systemctl enable --now companion-backup-push.timer
  systemctl status companion-backup-push.timer --no-pager | head -10
else
  echo "WARN: companion-backup-push.timer not yet installed; deploy via deploy.sh first."
fi

echo "Setup complete for ${MACHINE}."
```

- [ ] **Step 2: Validate syntax**

```bash
bash -n /home/newlevel/devel/companion/host/setup-backup-key.sh
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
cd /home/newlevel/devel/companion
chmod +x host/setup-backup-key.sh
git add host/setup-backup-key.sh
git commit -m "Add one-time setup helper for backup deploy key + timer"
```

---

## Phase B: Upgrade safety gate (Component 2)

### Task B1: Add Counts struct + JSON parsing with TDD

**Files:**
- Create: `updater/backend/src/safety.rs`
- Modify: `updater/backend/src/main.rs` (add `mod safety;`)

- [ ] **Step 1: Write the module skeleton with failing tests**

`/home/newlevel/devel/companion/updater/backend/src/safety.rs`:

```rust
//! Pre/post upgrade safety: snapshot Companion's full export, count critical
//! entities, compare, and trigger rollback if any count decreased.
//!
//! This module is intentionally count-only — full diffs are noisy and
//! version-fragile. Counts catch the failure mode that bit us on 2026-04-29:
//! a v4.2 → v4.3 migration silently dropped buttons.

use serde::Serialize;

/// Counts of entities in a `.companionconfig` export.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Counts {
    pub connections: usize,
    pub pages_with_content: usize,
    pub buttons: usize,
    pub triggers: usize,
}

impl Counts {
    /// `true` if any field in `post` is strictly less than the same field in `self`.
    pub fn any_decreased(&self, post: &Counts) -> bool {
        post.connections < self.connections
            || post.pages_with_content < self.pages_with_content
            || post.buttons < self.buttons
            || post.triggers < self.triggers
    }

    /// Per-field difference: `self - post` (i.e., how many were lost).
    /// Negative deltas (gains) are clamped to 0.
    pub fn lost(&self, post: &Counts) -> Counts {
        Counts {
            connections: self.connections.saturating_sub(post.connections),
            pages_with_content: self.pages_with_content.saturating_sub(post.pages_with_content),
            buttons: self.buttons.saturating_sub(post.buttons),
            triggers: self.triggers.saturating_sub(post.triggers),
        }
    }
}

/// Parse a Companion `.companionconfig` JSON byte stream and compute `Counts`.
///
/// Schema (Companion v4.3, observed in real exports):
///   { "instances": { ... },                 // map of connection id -> spec
///     "pages":     [ ... ] | { ... },       // list or map of page entries
///     "triggers":  { ... } }                // map of trigger id -> spec
///
/// Each page entry contains a `controls` map (row -> col -> bank id). A page
/// "has content" if `controls` has at least one row with at least one column.
pub fn count_from_json(json: &[u8]) -> Result<Counts, String> {
    let v: serde_json::Value =
        serde_json::from_slice(json).map_err(|e| format!("parse companionconfig: {e}"))?;

    let connections = v
        .get("instances")
        .and_then(|x| x.as_object())
        .map(|o| o.len())
        .unwrap_or(0);

    let triggers = v
        .get("triggers")
        .and_then(|x| x.as_object())
        .map(|o| o.len())
        .unwrap_or(0);

    let (pages_with_content, buttons) = count_pages(&v);

    Ok(Counts {
        connections,
        pages_with_content,
        buttons,
        triggers,
    })
}

fn count_pages(v: &serde_json::Value) -> (usize, usize) {
    let pages = match v.get("pages") {
        Some(p) => p,
        None => return (0, 0),
    };
    let entries: Vec<&serde_json::Value> = if let Some(arr) = pages.as_array() {
        arr.iter().collect()
    } else if let Some(obj) = pages.as_object() {
        obj.values().collect()
    } else {
        return (0, 0);
    };

    let mut pages_with_content = 0usize;
    let mut buttons = 0usize;
    for page in entries {
        let controls = match page.get("controls").and_then(|c| c.as_object()) {
            Some(c) => c,
            None => continue,
        };
        let mut count_on_page = 0usize;
        for row in controls.values() {
            if let Some(cols) = row.as_object() {
                count_on_page += cols.len();
            }
        }
        if count_on_page > 0 {
            pages_with_content += 1;
            buttons += count_on_page;
        }
    }
    (pages_with_content, buttons)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_json_object_yields_zeros() {
        let c = count_from_json(b"{}").unwrap();
        assert_eq!(c, Counts::default());
    }

    #[test]
    fn counts_connections_in_instances_map() {
        let json = br#"{"instances":{"a":{},"b":{},"c":{}}}"#;
        assert_eq!(count_from_json(json).unwrap().connections, 3);
    }

    #[test]
    fn counts_triggers_in_triggers_map() {
        let json = br#"{"triggers":{"t1":{},"t2":{}}}"#;
        assert_eq!(count_from_json(json).unwrap().triggers, 2);
    }

    #[test]
    fn counts_buttons_in_pages_array() {
        let json = br#"{"pages":[
            {"controls":{"0":{"0":"bank:a","1":"bank:b"},"1":{"0":"bank:c"}}},
            {"controls":{"0":{"0":"bank:d"}}},
            {"controls":{}}
        ]}"#;
        let c = count_from_json(json).unwrap();
        assert_eq!(c.buttons, 4);
        assert_eq!(c.pages_with_content, 2);
    }

    #[test]
    fn counts_buttons_in_pages_object() {
        let json = br#"{"pages":{"1":{"controls":{"0":{"0":"x"}}}}}"#;
        let c = count_from_json(json).unwrap();
        assert_eq!(c.buttons, 1);
        assert_eq!(c.pages_with_content, 1);
    }

    #[test]
    fn empty_pages_dont_count() {
        let json = br#"{"pages":[{"controls":{}},{"controls":{"0":{}}}]}"#;
        let c = count_from_json(json).unwrap();
        assert_eq!(c.buttons, 0);
        assert_eq!(c.pages_with_content, 0);
    }

    #[test]
    fn invalid_json_errors() {
        assert!(count_from_json(b"not json").is_err());
    }

    #[test]
    fn any_decreased_detects_drop() {
        let pre = Counts { connections: 41, pages_with_content: 20, buttons: 200, triggers: 47 };
        let post_same = pre;
        let post_more = Counts { connections: 42, ..pre };
        let post_less_buttons = Counts { buttons: 199, ..pre };
        assert!(!pre.any_decreased(&post_same));
        assert!(!pre.any_decreased(&post_more));
        assert!(pre.any_decreased(&post_less_buttons));
    }

    #[test]
    fn lost_clamps_gains_to_zero() {
        let pre = Counts { connections: 10, pages_with_content: 5, buttons: 50, triggers: 8 };
        let post = Counts { connections: 12, pages_with_content: 5, buttons: 48, triggers: 8 };
        let lost = pre.lost(&post);
        assert_eq!(lost.connections, 0);
        assert_eq!(lost.buttons, 2);
    }
}
```

- [ ] **Step 2: Add module declaration**

In `/home/newlevel/devel/companion/updater/backend/src/main.rs`, change the existing `mod` declarations from:

```rust
mod bitfocus;
mod companion;
mod static_files;
mod update;
mod version;
```

to:

```rust
mod bitfocus;
mod companion;
mod safety;
mod static_files;
mod update;
mod version;
```

The `safety` module's items are not yet wired into the router; later tasks call them. To suppress dead-code warnings until then, add `#[allow(dead_code)]` only on the `mod safety;` line:

```rust
#[allow(dead_code)]
mod safety;
```

- [ ] **Step 3: Run tests**

```bash
cd /home/newlevel/devel/companion/updater
cargo test -p companion-updater safety
```

Expected: 9/9 tests pass.

- [ ] **Step 4: Verify clean build**

```bash
cargo build -p companion-updater
```

Expected: no errors, no warnings.

- [ ] **Step 5: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/backend/src/safety.rs updater/backend/src/main.rs
git commit -m "Add safety counts module with TDD coverage"
```

---

### Task B2: Add export/import HTTP helpers to safety module

**Files:**
- Modify: `updater/backend/src/safety.rs`

**Why:** The safety flow needs to call Companion's HTTP API to (a) fetch a fresh full export and (b) import a saved export back during rollback. Both functions return `Result<…, String>` so they slot into the existing `update.rs` error handling.

**Note (pinned by Task A1, 2026-04-29 against companion-pp.lan running v4.3.1):**

There is **no HTTP `/int/import/...` endpoint**. Companion's UI imports a `.companionconfig` over **tRPC v10 over a WebSocket at `ws://<host>:8000/trpc`**. The full restore procedure is a 4-call sequence — `prepareImport.start` → `prepareImport.uploadChunk` (repeated) → `prepareImport.complete` → `importFull` — all under the `importExport` router.

Verified findings:

- **Transport:** WebSocket upgrade at `/trpc` (server returns HTTP 101). All calls are JSON-RPC-shaped tRPC messages over that single socket.
- **Authentication:** none. Companion's admin server has no auth at all (same as the existing `/int/export/full` GET).
- **Companion must be running.** The endpoint is implemented in the Companion process itself; if the service is stopped, the WS connect fails. (There is no out-of-band import — replacing `db.sqlite` would be the only "Companion stopped" alternative, and we are NOT going to do that.)
- **Idempotency: confirmed.** Round-trip on companion-pp (export → import the same file → re-export) preserved all counts: 39 instances, 99 pages, 43 triggers. Re-importing an export of the current state is a safe no-op for our rollback case (the rollback file IS a previous good state of the same Companion).
- **`.companionconfig` content** is gzip-compressed JSON (Companion gunzips on import; the wire format is the raw gzipped bytes from `/int/export/full`).
- **Upload size cap:** 524 MB (`524288000` bytes), per the `ImportExport/Controller` ChunkedUploader. Our exports are ~180 KB, so we are 3 orders of magnitude under the cap.

The four mutations under `importExport` (with their zod-validated input shapes):

| tRPC path | Input | Returns |
| --- | --- | --- |
| `importExport.prepareImport.start` | `{ name: string, size: number (1..524288000) }` | `sessionId: string` |
| `importExport.prepareImport.uploadChunk` | `{ sessionId, offset: number≥0, data: base64-string }` | bytes-received-so-far |
| `importExport.prepareImport.complete` | `{ sessionId, expectedChecksum: 40-char SHA1 hex }` | `[err\|null, summary]` |
| `importExport.importFull` | `{ config: ResetConfig }` | `null` (errors thrown as tRPC errors) |

`ResetConfig` (from the live Companion source):

```ts
{
  buttons:             "unchanged" | "reset-and-import" | "reset",
  surfaces: {
    known:             "unchanged" | "reset-and-import" | "reset",
    instances:         "unchanged" | "reset-and-import" | "reset",
    remote:            "unchanged" | "reset-and-import" | "reset",
  },
  triggers:            "unchanged" | "reset-and-import" | "reset",
  customVariables:     "unchanged" | "reset-and-import" | "reset",
  expressionVariables: "unchanged" | "reset-and-import" | "reset",
  connections:         "unchanged" | "reset",  // NOTE: no "reset-and-import"; connections come back via importFull's apply pass
  userconfig:          "unchanged" | "reset",
}
```

For our rollback use case we send `"reset-and-import"` for everything that supports it, `"reset"` for `connections`, and `"unchanged"` for `userconfig` (don't wipe global Companion settings).

**Implications for B2 step 1:** the original assumption (`POST /int/import/full` multipart) is WRONG. The new implementation uses `tokio-tungstenite` for the WebSocket and `sha1`/`base64` for the chunk-upload protocol. The function signature stays the same — `Result<(), String>` — so callers in Task B5 are unaffected.

- [ ] **Step 1: Append HTTP helpers to safety.rs**

Add to the end of `/home/newlevel/devel/companion/updater/backend/src/safety.rs` (before the `#[cfg(test)]` block):

```rust
/// Local URL of the running Companion admin server.
const COMPANION_BASE: &str = "http://127.0.0.1:8000";

/// Fetch a full Companion export as JSON bytes.
pub async fn fetch_export(client: &reqwest::Client) -> Result<Vec<u8>, String> {
    let url = format!("{COMPANION_BASE}/int/export/full");
    let resp = client
        .get(&url)
        .timeout(std::time::Duration::from_secs(60))
        .send()
        .await
        .map_err(|e| format!("export request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "export endpoint returned {}",
            resp.status()
        ));
    }
    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| format!("read export body: {e}"))
}

/// Companion tRPC WebSocket endpoint (same host/port as the HTTP admin server,
/// just upgraded to WS at the `/trpc` path).
const COMPANION_TRPC_WS: &str = "ws://127.0.0.1:8000/trpc";

/// Maximum size of a single base64 upload chunk (raw bytes, before encoding).
/// 64 KiB matches what the official UI uses and stays well under any tRPC
/// message-size limits.
const IMPORT_CHUNK_BYTES: usize = 64 * 1024;

/// Import a full Companion configuration from raw `.companionconfig` bytes
/// (gzip-compressed JSON, as produced by `/int/export/full`).
///
/// This drives Companion's tRPC import flow over a WebSocket at
/// `ws://127.0.0.1:8000/trpc`:
///
/// 1. `importExport.prepareImport.start` — register an upload session
/// 2. `importExport.prepareImport.uploadChunk` — base64-chunked bytes
/// 3. `importExport.prepareImport.complete` — SHA-1 checksum to commit
/// 4. `importExport.importFull` — apply with a reset-and-import config
///
/// Companion MUST be running and reachable on localhost:8000. There is no
/// authentication. Re-importing the current export is a verified no-op
/// (counts unchanged), which is what makes auto-rollback safe.
///
/// The `client` parameter is unused in this body (kept for API symmetry with
/// `fetch_export`); the WebSocket is opened directly with `tokio-tungstenite`.
pub async fn import_companionconfig(
    _client: &reqwest::Client,
    bytes: Vec<u8>,
) -> Result<(), String> {
    use base64::Engine as _;
    use futures::{SinkExt as _, StreamExt as _};
    use sha1::{Digest as _, Sha1};
    use tokio_tungstenite::tungstenite::Message;

    let total_size = bytes.len();
    if total_size == 0 {
        return Err("import: refusing to send empty .companionconfig".into());
    }
    let mut hasher = Sha1::new();
    hasher.update(&bytes);
    let sha1_hex = format!("{:x}", hasher.finalize());

    let (mut ws, _resp) = tokio_tungstenite::connect_async(COMPANION_TRPC_WS)
        .await
        .map_err(|e| format!("import: ws connect failed: {e}"))?;

    // Tiny tRPC v10 client: each call is a JSON message with an incrementing
    // numeric id; the matching response carries the same id.
    let mut next_id: u64 = 1;
    async fn call(
        ws: &mut tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        id: u64,
        path: &str,
        input: serde_json::Value,
    ) -> Result<serde_json::Value, String> {
        let msg = serde_json::json!({
            "id": id,
            "method": "mutation",
            "params": { "path": path, "input": input },
        });
        ws.send(Message::Text(msg.to_string()))
            .await
            .map_err(|e| format!("import: ws send {path}: {e}"))?;
        loop {
            let next = ws
                .next()
                .await
                .ok_or_else(|| format!("import: ws closed waiting for {path}"))?;
            let frame = next.map_err(|e| format!("import: ws recv {path}: {e}"))?;
            let txt = match frame {
                Message::Text(t) => t,
                Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
                Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => continue,
                Message::Close(_) => return Err(format!("import: ws closed mid-call ({path})")),
            };
            let v: serde_json::Value = serde_json::from_str(&txt)
                .map_err(|e| format!("import: bad json from {path}: {e}"))?;
            if v.get("id").and_then(|x| x.as_u64()) != Some(id) {
                continue; // unrelated message (subscription event, etc.)
            }
            if let Some(err) = v.get("error") {
                return Err(format!("import: tRPC error on {path}: {err}"));
            }
            return Ok(v.get("result").cloned().unwrap_or(serde_json::Value::Null));
        }
    }

    // 1. start
    let id = next_id; next_id += 1;
    let r = call(
        &mut ws,
        id,
        "importExport.prepareImport.start",
        serde_json::json!({ "name": "rollback.companionconfig", "size": total_size }),
    ).await?;
    let session_id = r
        .pointer("/data")
        .and_then(|x| x.as_str())
        .ok_or_else(|| format!("import: start returned no sessionId: {r}"))?
        .to_string();

    // 2. upload chunks
    let mut offset = 0usize;
    while offset < total_size {
        let end = (offset + IMPORT_CHUNK_BYTES).min(total_size);
        let chunk_b64 = base64::engine::general_purpose::STANDARD.encode(&bytes[offset..end]);
        let id = next_id; next_id += 1;
        call(
            &mut ws,
            id,
            "importExport.prepareImport.uploadChunk",
            serde_json::json!({
                "sessionId": session_id,
                "offset": offset,
                "data": chunk_b64,
            }),
        ).await?;
        offset = end;
    }

    // 3. complete (parses + stores in pendingImport on the server)
    let id = next_id; next_id += 1;
    call(
        &mut ws,
        id,
        "importExport.prepareImport.complete",
        serde_json::json!({
            "sessionId": session_id,
            "expectedChecksum": sha1_hex,
        }),
    ).await?;

    // 4. importFull — apply with full reset-and-import. Note the asymmetry:
    //    `connections` and `userconfig` only accept "unchanged" | "reset"
    //    (no "reset-and-import"); connections are re-created by the apply pass.
    let id = next_id; next_id += 1;
    call(
        &mut ws,
        id,
        "importExport.importFull",
        serde_json::json!({
            "config": {
                "buttons":             "reset-and-import",
                "surfaces": {
                    "known":           "reset-and-import",
                    "instances":       "reset-and-import",
                    "remote":          "reset-and-import",
                },
                "triggers":            "reset-and-import",
                "customVariables":     "reset-and-import",
                "expressionVariables": "reset-and-import",
                "connections":         "reset",
                "userconfig":          "unchanged",
            }
        }),
    ).await?;

    let _ = ws.close(None).await;
    Ok(())
}

/// Wait until Companion's `/api/version` endpoint returns 2xx, polling every
/// 2 seconds for up to `timeout`.
pub async fn wait_until_healthy(
    client: &reqwest::Client,
    timeout: std::time::Duration,
) -> Result<(), String> {
    let start = std::time::Instant::now();
    let url = format!("{COMPANION_BASE}/api/version");
    while start.elapsed() < timeout {
        match client.get(&url).timeout(std::time::Duration::from_secs(3)).send().await {
            Ok(r) if r.status().is_success() => return Ok(()),
            _ => {
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        }
    }
    Err(format!("Companion not healthy within {:?}", timeout))
}
```

- [ ] **Step 2: Add WebSocket / hash / base64 deps to Cargo.toml**

The new tRPC import flow needs a WebSocket client (`tokio-tungstenite`), `sha1` for the chunk-upload checksum Companion verifies, and `base64` for chunk encoding. `reqwest`'s features stay as-is — multipart is no longer needed because the import is not HTTP.

In `/home/newlevel/devel/companion/updater/backend/Cargo.toml`, append to `[dependencies]`:

```toml
tokio-tungstenite = { version = "0.24", default-features = false, features = ["connect"] }
sha1 = "0.10"
base64 = "0.22"
```

(`reqwest` keeps `features = ["json", "rustls-tls"]` — no `multipart` needed.)

- [ ] **Step 3: Build**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater
```

Expected: clean build (the new functions are not yet called, but `#[allow(dead_code)]` on the module suppresses warnings).

- [ ] **Step 4: Verify existing safety tests still pass**

```bash
cargo test -p companion-updater safety
```

Expected: 9/9 tests pass (no new tests yet — these helpers are integration-tested in deploy verification).

- [ ] **Step 5: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/backend/src/safety.rs updater/backend/Cargo.toml
git commit -m "Add HTTP export/import/health helpers to safety module"
```

---

### Task B3: Add new SSE event variants to UpdateEvent

**Files:**
- Modify: `updater/backend/src/update.rs`

- [ ] **Step 1: Replace UpdateEvent enum and add tests**

In `/home/newlevel/devel/companion/updater/backend/src/update.rs`, replace the existing `UpdateEvent` enum (currently lines ~16-22):

```rust
#[derive(Clone, Debug, serde::Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum UpdateEvent {
    Progress { message: String },
    Complete { message: String },
    Error { message: String },
}
```

with:

```rust
#[derive(Clone, Debug, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum UpdateEvent {
    Progress {
        message: String,
    },
    Complete {
        message: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        diff: Option<crate::safety::Counts>,
    },
    Error {
        message: String,
    },
    SafetyPre {
        counts: crate::safety::Counts,
    },
    SafetyPost {
        counts: crate::safety::Counts,
    },
    SafetyRollback {
        message: String,
        lost: crate::safety::Counts,
    },
}
```

Note: changed `rename_all = "lowercase"` to `"snake_case"` so `SafetyPre` serializes as `"safety_pre"` (matches the spec) instead of `"safetypre"`. The existing variants `Progress`/`Complete`/`Error` serialize identically under both rules, so this is backwards compatible with the frontend.

The existing `Complete` variant gains an optional `diff` field. Existing call sites passing `UpdateEvent::Complete { message: ... }` need the field added; the `Option<>` wrapper plus `skip_serializing_if` keeps the on-the-wire shape identical when no diff is provided.

- [ ] **Step 2: Update existing call sites in run_update**

Still in `update.rs`, find the existing call (near the end of `run_update`):

```rust
let _ = tx
    .send(UpdateEvent::Complete {
        message: format!(
            "Update complete. Now running {}",
            crate::version::format(&new_version)
        ),
    })
    .await;
```

Change to:

```rust
let _ = tx
    .send(UpdateEvent::Complete {
        message: format!(
            "Update complete. Now running {}",
            crate::version::format(&new_version)
        ),
        diff: None,
    })
    .await;
```

(Task B5 will replace this with the safety-wrapped version that supplies a real diff.)

- [ ] **Step 3: Replace existing tests with the new shape**

In the same file, replace the entire `#[cfg(test)] mod tests { ... }` block with:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::safety::Counts;

    #[test]
    fn event_progress_serializes() {
        let e = UpdateEvent::Progress { message: "hello".into() };
        assert_eq!(
            serde_json::to_string(&e).unwrap(),
            r#"{"type":"progress","message":"hello"}"#
        );
    }

    #[test]
    fn event_complete_no_diff_serializes_compatibly() {
        let e = UpdateEvent::Complete { message: "done".into(), diff: None };
        assert_eq!(
            serde_json::to_string(&e).unwrap(),
            r#"{"type":"complete","message":"done"}"#
        );
    }

    #[test]
    fn event_complete_with_diff_serializes() {
        let e = UpdateEvent::Complete {
            message: "done".into(),
            diff: Some(Counts { connections: 0, pages_with_content: 0, buttons: 0, triggers: 0 }),
        };
        assert_eq!(
            serde_json::to_string(&e).unwrap(),
            r#"{"type":"complete","message":"done","diff":{"connections":0,"pages_with_content":0,"buttons":0,"triggers":0}}"#
        );
    }

    #[test]
    fn event_error_serializes() {
        let e = UpdateEvent::Error { message: "boom".into() };
        assert_eq!(
            serde_json::to_string(&e).unwrap(),
            r#"{"type":"error","message":"boom"}"#
        );
    }

    #[test]
    fn event_safety_pre_serializes_with_snake_case_tag() {
        let e = UpdateEvent::SafetyPre {
            counts: Counts { connections: 41, pages_with_content: 20, buttons: 250, triggers: 47 },
        };
        let s = serde_json::to_string(&e).unwrap();
        assert!(s.starts_with(r#"{"type":"safety_pre","counts":"#), "got {s}");
    }

    #[test]
    fn event_safety_post_serializes_with_snake_case_tag() {
        let e = UpdateEvent::SafetyPost {
            counts: Counts::default(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert!(s.starts_with(r#"{"type":"safety_post","counts":"#), "got {s}");
    }

    #[test]
    fn event_safety_rollback_includes_lost_counts() {
        let e = UpdateEvent::SafetyRollback {
            message: "rolled back".into(),
            lost: Counts { connections: 0, pages_with_content: 0, buttons: 5, triggers: 0 },
        };
        let s = serde_json::to_string(&e).unwrap();
        assert!(s.contains(r#""type":"safety_rollback""#), "got {s}");
        assert!(s.contains(r#""buttons":5"#), "got {s}");
    }
}
```

- [ ] **Step 4: Now that the safety module is referenced from non-test code, remove the `#[allow(dead_code)]` from main.rs**

In `/home/newlevel/devel/companion/updater/backend/src/main.rs`, change:

```rust
#[allow(dead_code)]
mod safety;
```

back to:

```rust
mod safety;
```

(`safety::Counts` is now referenced from `update.rs::UpdateEvent` so it's no longer dead code; `fetch_export`/`import_companionconfig`/`wait_until_healthy` will get used in Task B5.)

- [ ] **Step 5: Build + tests**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater 2>&1 | tail -20
cargo test -p companion-updater 2>&1 | tail -20
```

Expected: clean build. All tests pass. Note: B2's HTTP helpers may now show dead-code warnings since they're not yet called from non-test code. If that happens, add `#[allow(dead_code)]` on each of `fetch_export`, `import_companionconfig`, and `wait_until_healthy` in `safety.rs` (they're wired up in Task B5).

- [ ] **Step 6: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/backend/src/update.rs updater/backend/src/main.rs updater/backend/src/safety.rs
git commit -m "Add SafetyPre/Post/Rollback variants to UpdateEvent"
```

---

### Task B4: Add StateDirectory to systemd unit

**Files:**
- Modify: `updater/companion-updater.service`

- [ ] **Step 1: Update service unit**

`/home/newlevel/devel/companion/updater/companion-updater.service` — replace contents with:

```ini
[Unit]
Description=Bitfocus Companion Update Dashboard
After=network-online.target companion.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/companion-updater
Restart=on-failure
RestartSec=10
Environment=RUST_LOG=info
StateDirectory=companion-updater
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
```

`StateDirectory=companion-updater` causes systemd to create `/var/lib/companion-updater/` (mode 0700, owned by `root` since `User=root`) and pass `STATE_DIRECTORY=/var/lib/companion-updater` as an env var. The safety module reads/writes its snapshot files there.

- [ ] **Step 2: Validate**

```bash
systemd-analyze verify /home/newlevel/devel/companion/updater/companion-updater.service 2>&1 | head -10
```

Expected: only the unrelated "Command /usr/local/bin/companion-updater is not executable" warning (the binary doesn't exist on the dev machine — that's fine).

- [ ] **Step 3: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/companion-updater.service
git commit -m "Add StateDirectory to companion-updater unit for safety snapshots"
```

---

### Task B5: Wire safety hooks into run_update

**Files:**
- Modify: `updater/backend/src/update.rs`

- [ ] **Step 1: Replace run_update with the safety-wrapped version**

In `/home/newlevel/devel/companion/updater/backend/src/update.rs`, replace the entire `run_update` function with:

```rust
/// Spawn the update process and stream events through `tx`.
///
/// Wraps the existing update.sh + systemctl restart flow with a safety gate:
///   1. Pre-upgrade: fetch full export, parse counts, archive, emit SafetyPre.
///   2. Run update.sh stable.
///   3. systemctl restart companion; wait for healthy.
///   4. Post-upgrade: fetch full export, parse counts, emit SafetyPost.
///   5. If any count decreased: import the pre-upgrade snapshot, restart again,
///      emit SafetyRollback. Otherwise emit Complete with diff.
pub async fn run_update(tx: mpsc::Sender<UpdateEvent>) {
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(180))
        .build()
        .expect("reqwest client");

    // 1. Pre-upgrade snapshot.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Taking pre-upgrade snapshot...".into(),
    }).await;

    let pre_bytes = match crate::safety::fetch_export(&http).await {
        Ok(b) => b,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("pre-upgrade snapshot failed: {e}"),
            }).await;
            return;
        }
    };
    let pre_counts = match crate::safety::count_from_json(&pre_bytes) {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("pre-upgrade parse failed: {e}"),
            }).await;
            return;
        }
    };
    if let Err(e) = save_snapshot(&pre_bytes).await {
        let _ = tx.send(UpdateEvent::Error {
            message: format!("could not persist pre-upgrade snapshot: {e}"),
        }).await;
        return;
    }
    let _ = tx.send(UpdateEvent::SafetyPre { counts: pre_counts }).await;

    // 2. Run update.sh stable.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Starting update (stable channel)...".into(),
    }).await;
    if let Err(e) = run_update_script(&tx).await {
        let _ = tx.send(UpdateEvent::Error { message: e }).await;
        return;
    }

    // 3. Restart Companion + wait for healthy.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Restarting companion service...".into(),
    }).await;
    match Command::new("sudo")
        .args(["systemctl", "restart", "companion"])
        .status()
        .await
    {
        Ok(s) if s.success() => {}
        Ok(s) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("systemctl restart exited with {s}"),
            }).await;
            return;
        }
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("systemctl restart failed: {e}"),
            }).await;
            return;
        }
    }
    if let Err(e) =
        crate::safety::wait_until_healthy(&http, std::time::Duration::from_secs(60)).await
    {
        let _ = tx.send(UpdateEvent::Error {
            message: format!("Companion did not return to healthy: {e}"),
        }).await;
        return;
    }

    // 4. Post-upgrade snapshot.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Taking post-upgrade snapshot...".into(),
    }).await;
    let post_bytes = match crate::safety::fetch_export(&http).await {
        Ok(b) => b,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("post-upgrade snapshot failed: {e}"),
            }).await;
            return;
        }
    };
    let post_counts = match crate::safety::count_from_json(&post_bytes) {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("post-upgrade parse failed: {e}"),
            }).await;
            return;
        }
    };
    let _ = tx.send(UpdateEvent::SafetyPost { counts: post_counts }).await;

    // 5. Compare; rollback if any decrease.
    if pre_counts.any_decreased(&post_counts) {
        let lost = pre_counts.lost(&post_counts);
        let _ = tx.send(UpdateEvent::Progress {
            message: format!(
                "Data loss detected (lost {} connections, {} buttons, {} triggers). Rolling back...",
                lost.connections, lost.buttons, lost.triggers
            ),
        }).await;

        if let Err(e) = crate::safety::import_companionconfig(&http, pre_bytes).await {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("rollback import failed: {e}"),
            }).await;
            return;
        }
        if let Err(e) =
            crate::safety::wait_until_healthy(&http, std::time::Duration::from_secs(60)).await
        {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("Companion did not return to healthy after rollback: {e}"),
            }).await;
            return;
        }
        let _ = tx.send(UpdateEvent::SafetyRollback {
            message: "Data loss detected; rolled back to pre-upgrade state.".into(),
            lost,
        }).await;
        return;
    }

    // 6. Success.
    let new_version = crate::companion::read_installed_version()
        .await
        .unwrap_or_else(|_| "unknown".into());
    let diff = crate::safety::Counts {
        connections: post_counts.connections.saturating_sub(pre_counts.connections),
        pages_with_content: post_counts
            .pages_with_content
            .saturating_sub(pre_counts.pages_with_content),
        buttons: post_counts.buttons.saturating_sub(pre_counts.buttons),
        triggers: post_counts.triggers.saturating_sub(pre_counts.triggers),
    };
    let _ = tx.send(UpdateEvent::Complete {
        message: format!(
            "Update complete. Now running {}",
            crate::version::format(&new_version)
        ),
        diff: Some(diff),
    }).await;
}

async fn save_snapshot(bytes: &[u8]) -> Result<(), String> {
    let state_dir = std::env::var("STATE_DIRECTORY")
        .unwrap_or_else(|_| "/var/lib/companion-updater".to_string());
    tokio::fs::create_dir_all(&state_dir)
        .await
        .map_err(|e| format!("create state dir: {e}"))?;
    let path = format!("{state_dir}/pre-upgrade.companionconfig");
    tokio::fs::write(&path, bytes)
        .await
        .map_err(|e| format!("write {path}: {e}"))?;

    let archive_dir = format!("{state_dir}/pre-upgrade-archive");
    tokio::fs::create_dir_all(&archive_dir)
        .await
        .map_err(|e| format!("create archive dir: {e}"))?;
    let ts = chrono::Local::now().format("%Y%m%dT%H%M%S").to_string();
    let archived = format!("{archive_dir}/{ts}.companionconfig");
    tokio::fs::write(&archived, bytes)
        .await
        .map_err(|e| format!("write {archived}: {e}"))?;
    prune_archive(&archive_dir).await;
    Ok(())
}

async fn prune_archive(dir: &str) {
    let now = std::time::SystemTime::now();
    let cutoff = now - std::time::Duration::from_secs(7 * 24 * 60 * 60);
    let mut rd = match tokio::fs::read_dir(dir).await {
        Ok(r) => r,
        Err(_) => return,
    };
    while let Ok(Some(entry)) = rd.next_entry().await {
        let meta = match entry.metadata().await {
            Ok(m) => m,
            Err(_) => continue,
        };
        let modified = meta.modified().unwrap_or(now);
        if modified < cutoff {
            let _ = tokio::fs::remove_file(entry.path()).await;
        }
    }
}

async fn run_update_script(tx: &mpsc::Sender<UpdateEvent>) -> Result<(), String> {
    let mut child = Command::new("sudo")
        .args(["bash", UPDATE_SCRIPT, "stable"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn update.sh: {e}"))?;

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let tx_out = tx.clone();
    let stdout_handle = tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = tx_out.send(UpdateEvent::Progress { message: line }).await;
        }
    });
    let tx_err = tx.clone();
    let stderr_handle = tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = tx_err.send(UpdateEvent::Progress { message: line }).await;
        }
    });

    let status = child.wait().await.map_err(|e| format!("update.sh wait failed: {e}"))?;
    let _ = stdout_handle.await;
    let _ = stderr_handle.await;
    if !status.success() {
        return Err(format!("update.sh exited with {status}"));
    }
    Ok(())
}
```

The original `Duration` import at the top of the file is no longer used; remove the line `use std::time::Duration;` if rust complains, otherwise leave it.

- [ ] **Step 2: Build**

```bash
cd /home/newlevel/devel/companion/updater
cargo build -p companion-updater 2>&1 | tail -30
```

Expected: clean build. If any of the safety module's HTTP helpers (`fetch_export`, `import_companionconfig`, `wait_until_healthy`) had `#[allow(dead_code)]` annotations from Task B3 step 5, remove them now — they're called above.

- [ ] **Step 3: Run all tests**

```bash
cargo test -p companion-updater 2>&1 | tail -10
```

Expected: all tests pass. The unit tests for `UpdateEvent` serialization from B3 still cover the wire format; the safety wrapper itself is integration-tested in deploy verification (Phase C).

- [ ] **Step 4: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/backend/src/update.rs updater/backend/src/safety.rs
git commit -m "Wire pre/post safety snapshots and rollback into run_update"
```

---

### Task B6: Render new SSE events in the frontend

**Files:**
- Modify: `updater/frontend/src/components/progress_log.rs`
- Modify: `updater/frontend/src/components/update_button.rs`
- Modify: `updater/frontend/style.css`

- [ ] **Step 1: Update progress_log to handle safety event types**

`/home/newlevel/devel/companion/updater/frontend/src/components/progress_log.rs` — replace contents with:

```rust
use leptos::prelude::*;

#[component]
pub fn ProgressLog(lines: ReadSignal<Vec<(String, String)>>) -> impl IntoView {
    let visible = move || !lines.get().is_empty();
    let each_fn = move || lines.get().into_iter().enumerate().collect::<Vec<_>>();
    let key_fn = |(i, _): &(usize, (String, String))| *i;
    let children_fn = move |(_, (kind, msg)): (usize, (String, String))| {
        let cls = match kind.as_str() {
            "error" => "line error",
            "complete" => "line success",
            "safety_pre" => "line safety-pre",
            "safety_post" => "line safety-post",
            "safety_rollback" => "line safety-rollback",
            _ => "line",
        };
        view! { <div class={cls}>{msg}</div> }
    };

    view! {
        <Show when=visible fallback=|| view! { <span></span> }>
            <div class="progress-container active">
                <div class="progress-log">
                    <For
                        each=each_fn
                        key=key_fn
                        children=children_fn
                    />
                </div>
            </div>
        </Show>
    }
}
```

- [ ] **Step 2: Extend update_button to handle the new event tags**

In `/home/newlevel/devel/companion/updater/frontend/src/components/update_button.rs`, find the SSE message handler (the `Closure::<dyn FnMut(MessageEvent)>::new(...)` that parses `UpdateEvent`). Replace the parsing block — currently:

```rust
let parsed: UpdateEvent = match serde_json::from_str(&data) {
    Ok(p) => p,
    Err(_) => return,
};
set_progress_lines.update(|v| {
    v.push((parsed.kind.clone(), parsed.message.clone()));
});
if parsed.kind == "complete" || parsed.kind == "error" {
    es_for_msg.close();
    set_updating.set(false);
    // ... refresh
}
```

— with a more permissive parser that handles all event variants:

```rust
let val: serde_json::Value = match serde_json::from_str(&data) {
    Ok(v) => v,
    Err(_) => return,
};
let kind = val.get("type").and_then(|t| t.as_str()).unwrap_or("").to_string();
let message = if let Some(m) = val.get("message").and_then(|m| m.as_str()) {
    m.to_string()
} else if kind == "safety_pre" || kind == "safety_post" {
    // Format counts inline.
    let c = val.get("counts").cloned().unwrap_or_default();
    let connections = c.get("connections").and_then(|x| x.as_u64()).unwrap_or(0);
    let pages = c.get("pages_with_content").and_then(|x| x.as_u64()).unwrap_or(0);
    let buttons = c.get("buttons").and_then(|x| x.as_u64()).unwrap_or(0);
    let triggers = c.get("triggers").and_then(|x| x.as_u64()).unwrap_or(0);
    let label = if kind == "safety_pre" { "Pre-upgrade snapshot" } else { "Post-upgrade snapshot" };
    format!(
        "{label}: {connections} connections, {pages} pages, {buttons} buttons, {triggers} triggers"
    )
} else {
    "(no message)".to_string()
};

set_progress_lines.update(|v| {
    v.push((kind.clone(), message));
});

let terminal = matches!(kind.as_str(), "complete" | "error" | "safety_rollback");
if terminal {
    es_for_msg.close();
    set_updating.set(false);
    // existing status-refresh logic stays exactly as it is below this point
}
```

The `UpdateEvent` Rust struct in `update_button.rs` is no longer used; you can remove its `#[derive(Deserialize)] struct UpdateEvent { kind, message }` since we now parse via `serde_json::Value`. (Also remove its imports if no longer referenced.)

- [ ] **Step 3: Add CSS classes for safety events**

Append to `/home/newlevel/devel/companion/updater/frontend/style.css`:

```css
/* Safety gate events */
.progress-log .line.safety-pre,
.progress-log .line.safety-post {
    color: #ffc107;
    font-weight: 500;
}
.progress-log .line.safety-rollback {
    color: #ff6b6b;
    background: rgba(255, 107, 107, 0.1);
    border-left: 3px solid #ff6b6b;
    padding: 0.25rem 0.5rem;
    margin: 0.25rem 0;
    font-weight: 600;
}
```

- [ ] **Step 4: Build the frontend**

```bash
cd /home/newlevel/devel/companion/updater/frontend
trunk build --release 2>&1 | tail -10
```

Expected: clean build. New WASM bundle in `dist/`.

- [ ] **Step 5: Build the backend (re-embeds dist)**

```bash
cd /home/newlevel/devel/companion/updater
cargo build --release -p companion-updater 2>&1 | tail -10
```

Expected: clean build.

- [ ] **Step 6: Commit**

```bash
cd /home/newlevel/devel/companion
git add updater/frontend/src/components/progress_log.rs \
        updater/frontend/src/components/update_button.rs \
        updater/frontend/style.css
git commit -m "Render safety_pre/post/rollback events in dashboard"
```

---

### Task B7: Update deploy.sh to install backup pusher and route upgrades through updater

**Files:**
- Modify: `deploy.sh`

- [ ] **Step 1: Add backup pusher installation step**

In `/home/newlevel/devel/companion/deploy.sh`, add this section AFTER step 2 ("Install udev rules") and BEFORE step 3 ("Update Companion"). Renumber subsequent steps in the echo lines (step 3 becomes [3/8], step 4 → [4/8], etc.) so `[X/Y]` reflects the new total of 8 steps.

```bash
# Step 3: Install backup pusher (script + systemd units; deploy key set up separately)
echo "[3/8] Installing backup pusher..."
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.sh"
remote "sudo install -m 0755 /tmp/companion-backup-push.sh /usr/local/bin/companion-backup-push.sh && rm /tmp/companion-backup-push.sh"
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.service" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.service"
remote "sudo install -m 0644 /tmp/companion-backup-push.service /etc/systemd/system/companion-backup-push.service && rm /tmp/companion-backup-push.service"
remote_copy "${SCRIPT_DIR}/host/companion-backup-push.timer" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/companion-backup-push.timer"
remote "sudo install -m 0644 /tmp/companion-backup-push.timer /etc/systemd/system/companion-backup-push.timer && rm /tmp/companion-backup-push.timer"
remote_copy "${SCRIPT_DIR}/host/setup-backup-key.sh" "${COMPANION_USER}@${COMPANION_HOST}:/tmp/setup-backup-key.sh"
remote "sudo install -m 0755 /tmp/setup-backup-key.sh /usr/local/sbin/setup-backup-key.sh && rm /tmp/setup-backup-key.sh"
remote "sudo systemctl daemon-reload"
# Enable timer only if /etc/default/companion-backup-push exists (i.e., setup-backup-key.sh has been run).
if remote "test -f /etc/default/companion-backup-push"; then
  remote "sudo systemctl enable --now companion-backup-push.timer"
  echo "  Backup pusher enabled."
else
  echo "  Backup pusher installed but NOT yet enabled."
  echo "  Run on the host:    sudo /usr/local/sbin/setup-backup-key.sh"
  echo "  Then re-run this deploy or:  sudo systemctl enable --now companion-backup-push.timer"
fi
```

- [ ] **Step 2: Replace the direct update.sh call with an HTTP call to the updater**

In the same `deploy.sh`, find the "Update Companion via companion-update" step (currently `[3/7]`, becomes `[4/8]`) and replace its body. Currently:

```bash
echo "[3/7] Updating Companion..."
if remote "command -v companion-update >/dev/null 2>&1"; then
  remote "sudo bash /usr/local/src/companionpi/update.sh stable"
  echo "  Companion updated."
else
  echo "  companion-update not found — companion-pi may not be installed."
  echo "  Install with: curl https://raw.githubusercontent.com/bitfocus/companion-pi/main/install.sh | sudo bash"
  exit 1
fi
```

Change to:

```bash
echo "[4/8] Updating Companion via companion-updater (safety-gated)..."
# Stream the safety-gated update endpoint. We grep for the terminal event in
# the SSE stream: complete = success, safety_rollback = data loss + reverted,
# error = anything else. The first match wins; we then stop reading.
TERMINAL_LINE="$(remote "curl -fsS --no-buffer --max-time 1800 -H 'Accept: text/event-stream' http://127.0.0.1:8081/api/update/stream 2>&1 | grep -m1 -E '\"type\":\"(complete|error|safety_rollback)\"'" || true)"
echo "  ${TERMINAL_LINE}"
case "${TERMINAL_LINE}" in
  *'"type":"complete"'*)
    echo "  Companion updated successfully."
    ;;
  *'"type":"safety_rollback"'*)
    echo "  ERROR: data loss detected during upgrade; updater rolled back."
    exit 1
    ;;
  *'"type":"error"'*)
    echo "  ERROR: upgrade failed (see updater journal: sudo journalctl -u companion-updater -n 100)."
    exit 1
    ;;
  *)
    echo "  ERROR: did not receive a terminal event from updater."
    exit 1
    ;;
esac
```

The systemctl restart no longer needs to happen here — the updater already does it before the post-snapshot. So the next step (previously `[4/7] Restarting Companion service...`) is removed entirely.

- [ ] **Step 3: Renumber the remaining echo lines**

After the changes above, the script should have steps `[1/8]` through `[8/8]`. Walk through the file and ensure every `[N/X]` matches the new numbering. The full sequence is:

1. `[1/8] Testing connection...`
2. `[2/8] Installing udev rules...`
3. `[3/8] Installing backup pusher...`
4. `[4/8] Updating Companion via companion-updater (safety-gated)...`
5. `[5/8] Building companion-updater...` (existing local build step)
6. `[6/8] Deploying companion-updater...` (existing binary + service deploy step)
7. `[7/8] (formerly companion-updater Docker step — should be gone already from the previous PR)`
8. `[8/8] Health check`

Inspect with `grep -n "\[.\?[0-9]\?/[0-9]\?\]" deploy.sh` to verify.

- [ ] **Step 4: Validate**

```bash
bash -n /home/newlevel/devel/companion/deploy.sh
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
cd /home/newlevel/devel/companion
git add deploy.sh
git commit -m "Route upgrades through updater safety gate; install backup pusher"
```

---

## Phase C: Deploy + verify

### Task C1: Deploy to companion-pp.lan first (lower-stakes target)

**Why:** companion-pp.lan was just freshly migrated and has known-good state. Deploying here first lets us validate before touching companion.lan (the primary production machine).

- [ ] **Step 1: Run setup-backup-key.sh on companion-pp.lan**

```bash
sshpass -p 'newlevel' ssh -t -o StrictHostKeyChecking=no newlevel@companion-pp.lan "sudo /usr/local/sbin/setup-backup-key.sh"
```

Wait — this is the FIRST deploy, so `setup-backup-key.sh` doesn't exist on the host yet. We need to deploy first (which copies the script), then run setup-backup-key, then the timer can be enabled. Run this sequence:

```bash
cd /home/newlevel/devel/companion
COMPANION_HOST=companion-pp.lan ./deploy.sh
# At this point the deploy installs the script and timer files but does NOT enable the timer
# (because /etc/default/companion-backup-push doesn't exist yet).

# Now run the one-time setup interactively (it pauses for the user to add the deploy key on GitHub):
sshpass -p 'newlevel' ssh -t -o StrictHostKeyChecking=no newlevel@companion-pp.lan "sudo /usr/local/sbin/setup-backup-key.sh"
# Copy the printed public key to https://github.com/zbynekdrlik/companion-backups/settings/keys
# (allow write access). Press ENTER on the SSH session to continue.
# The script clones the repo, writes /etc/default/companion-backup-push, and enables the timer.

# Re-deploy is optional but harmless:
COMPANION_HOST=companion-pp.lan ./deploy.sh
```

Expected: First deploy completes, including the safety-gated update which is a no-op on stable (Counts equal, Complete with `diff` of all zeros). After `setup-backup-key.sh`, `systemctl status companion-backup-push.timer` on companion-pp.lan reports `active (waiting)`.

- [ ] **Step 2: Manually trigger the timer to verify push works**

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion-pp.lan \
  "sudo systemctl start companion-backup-push.service && sleep 5 && sudo journalctl -u companion-backup-push.service -n 30 --no-pager"
```

Expected: log shows "Pushed backup ... for companion-pp." or "Backup unchanged; skipping push." If first run and unchanged, force a no-op-of-the-no-op edge: it's expected to push on first run.

- [ ] **Step 3: Verify the file appeared on GitHub**

```bash
gh api repos/zbynekdrlik/companion-backups/contents/companion-pp/latest.companionconfig --jq '{path, size, sha}'
```

Expected: returns a file blob with size > 100000.

- [ ] **Step 4: Verify safety-gated update worked**

The deploy in Step 1 already triggered an upgrade through the gate. Verify the journal:

```bash
sshpass -p 'newlevel' ssh -o StrictHostKeyChecking=no newlevel@companion-pp.lan \
  "sudo journalctl -u companion-updater -n 100 --no-pager | grep -E 'safety_pre|safety_post|complete' | tail -5"
```

Expected: shows `safety_pre`, `safety_post`, and `complete` events logged in order.

---

### Task C2: Deploy to companion.lan (primary production)

- [ ] **Step 1: Deploy + setup**

```bash
cd /home/newlevel/devel/companion
./deploy.sh   # default host is companion.lan
sshpass -p 'newlevel' ssh -t -o StrictHostKeyChecking=no newlevel@companion.lan "sudo /usr/local/sbin/setup-backup-key.sh"
# Add the printed key to GitHub deploy keys. Press ENTER.
```

Expected: deploy completes, timer enabled, first push commits to `companion-snv/` on GitHub.

- [ ] **Step 2: Verify file appeared**

```bash
gh api repos/zbynekdrlik/companion-backups/contents/companion-snv/latest.companionconfig --jq '{path, size, sha}'
```

Expected: file present, size > 100000.

- [ ] **Step 3: Verify the dashboard renders safety events**

Open `http://companion.lan:8081/` in Playwright, click "Update Now", watch the progress log. Should see `Pre-upgrade snapshot:`, then update.sh output, then `Post-upgrade snapshot:`, then `Update complete. Now running v...`. Browser console: zero errors, zero warnings.

---

## Phase D: Update README and ship

### Task D1: Update README and push

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Backups & Safety" section after the Architecture section**

Insert into `/home/newlevel/devel/companion/README.md`:

```markdown
## Backups & Safety

### Off-machine backups

Each Companion host pushes the latest `.companionconfig` export to a private
GitHub repo (`zbynekdrlik/companion-backups`) hourly via systemd timer.
Last 30 days of hourly snapshots are retained per machine.

Setup on a new host (one-time):

```bash
sudo /usr/local/sbin/setup-backup-key.sh
# Add the printed deploy key (with write access) to the backup repo on GitHub.
```

### Upgrade safety gate

The Rust `companion-updater` wraps every Companion upgrade with a pre/post
snapshot. If any of `connections`, `pages_with_content`, `buttons`, or
`triggers` decreases between snapshots, the updater automatically imports
the pre-upgrade snapshot back and surfaces the rollback in the dashboard.

This is the gate that would have caught the v4.2 → v4.3 silent button drop
on 2026-04-29.
```

- [ ] **Step 2: Commit**

```bash
cd /home/newlevel/devel/companion
git add README.md
git commit -m "Document backup pusher and upgrade safety gate"
```

- [ ] **Step 3: Push**

```bash
git push origin dev
```

---

### Task D2: Open PR

- [ ] **Step 1: Create PR**

```bash
gh pr create --base main --head dev \
  --title "Off-machine backups + upgrade safety gate" \
  --body "$(cat <<'EOF'
## Summary
- Hourly push of `.companionconfig` to private repo `zbynekdrlik/companion-backups` (30-day history retention)
- Pre/post snapshot comparison wraps every Companion upgrade; auto-rollback if any count of connections/pages/buttons/triggers drops
- Both companion.lan and companion-pp.lan now use the safety-gated update path; deploy.sh routes through it

Addresses the silent data loss incident from 2026-04-29 (v4.2 → v4.3 dropped LIGHTS 03 button and was not noticed for ~36 hours).

## Test plan
- [x] safety::count_from_json unit tests (9/9 pass)
- [x] UpdateEvent serialization tests (7/7 pass)
- [x] companion-pp.lan deployed; safety_pre/post events visible; backup file in GitHub
- [x] companion.lan deployed; same
- [x] Browser console clean on dashboard

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Check PR is mergeable + clean**

```bash
sleep 5
gh pr view --json number,mergeable,mergeStateStatus
```

Expected: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`.

Wait for explicit user merge instruction.
