//! Run the companion-pi update script and stream its output.
//!
//! Spawns `sudo bash /usr/local/src/companionpi/update.sh stable` and yields
//! each line of combined stdout/stderr. After the child exits with success,
//! restarts the companion service.

use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};
use std::sync::Arc;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;

const DEFAULT_COMPANIONPI_DIR: &str = "/usr/local/src/companionpi";

/// Path of the companion-pi checkout. Overridable so tests can drive the real
/// code paths against a throwaway directory.
fn companionpi_dir() -> String {
    std::env::var("COMPANIONPI_DIR").unwrap_or_else(|_| DEFAULT_COMPANIONPI_DIR.to_string())
}

/// Path of the update script that installs Companion.
fn update_script() -> String {
    std::env::var("COMPANION_UPDATE_SCRIPT")
        .unwrap_or_else(|_| format!("{}/update.sh", companionpi_dir()))
}

/// Whether privileged commands are wrapped in `sudo`. Only tests turn this off.
fn use_sudo() -> bool {
    std::env::var("COMPANION_UPDATE_SUDO").as_deref() != Ok("0")
}

/// True when `line` is one of the companion-pi "installed nothing" markers.
///
/// `update.sh` prints "Skipping update" whenever the version picker wrote no
/// selection file, and the picker prints "No matching <branch> build was found!"
/// when its version whitelist rejects every published build, or "The latest
/// build of <branch> (<version>) is already installed". All three exit 0, so the
/// exit status alone cannot tell success from a silent no-op.
///
/// The "already installed" phrase is only accepted together with "build", so
/// ordinary apt/npm chatter inside update.sh ("libfontconfig1 is already
/// installed") does not match.
pub(crate) fn is_skip_marker(line: &str) -> bool {
    line.contains("Skipping update")
        || line.contains("build was found!")
        || (line.contains("is already installed") && line.contains("build"))
}

/// What actually happened to `/opt/companion` during a run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Outcome {
    /// The installed version went up — the upgrade landed. Carries a note when
    /// the new version is still behind the latest advertised build.
    Applied { still_behind: Option<String> },
    /// Nothing changed, and nothing needed to: we already run the newest build.
    AlreadyLatest,
    /// The run cannot be called a success. The reason is in the message.
    Failed(String),
}

/// Decide whether an update run actually installed anything.
///
/// `pre`/`post` are the versions read from `/opt/companion/package.json` around
/// the run; `latest` is the newest stable build Bitfocus advertises, or `None`
/// when that lookup failed; `skipped` is true when any [`is_skip_marker`] line
/// appeared in the script output.
///
/// Only an INCREASE in the installed version counts as success. Everything the
/// function cannot positively verify is a failure — a run whose result is
/// unknown must never be presented to the operator as "up to date".
pub(crate) fn classify_outcome(
    pre: &str,
    post: &str,
    latest: Option<&str>,
    skipped: bool,
) -> Outcome {
    use std::cmp::Ordering;

    if crate::version::parse(post).is_empty() {
        return Outcome::Failed(format!(
            "Cannot verify the update: the installed version reads as {post:?}, \
             which is not a version number. Check /opt/companion/package.json."
        ));
    }

    match crate::version::compare(pre, post) {
        Ordering::Less => {
            let still_behind = latest
                .filter(|l| crate::version::is_update_available(post, l))
                .map(crate::version::format);
            Outcome::Applied { still_behind }
        }
        Ordering::Greater => Outcome::Failed(format!(
            "Update went BACKWARDS: {} was installed over {}. \
             The installer picked an older build.",
            crate::version::format(post),
            crate::version::format(pre)
        )),
        Ordering::Equal => match latest {
            // Nothing moved and we never learned what the newest build is, so we
            // cannot claim we are current. Say so instead of inventing success.
            None => Outcome::Failed(format!(
                "Cannot confirm the update: still running {} and the list of \
                 available builds could not be fetched, so there is no proof \
                 anything was installed. Check the network and try again.",
                crate::version::format(post)
            )),
            Some(l) if crate::version::is_update_available(post, l) => {
                let reason = if skipped {
                    "the companion-pi installer reported it had no matching build to install \
                     (its version whitelist may not cover this release yet)"
                } else {
                    "the update script exited successfully but /opt/companion is unchanged"
                };
                Outcome::Failed(format!(
                    "Update did NOT apply: still running {} while {} is available — {}.",
                    crate::version::format(post),
                    crate::version::format(l),
                    reason
                ))
            }
            Some(_) => Outcome::AlreadyLatest,
        },
    }
}

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

/// Spawn the update process and stream events through `tx`.
///
/// Returns true only when the run ended in a verified success — the caller uses
/// that to decide whether to start the retry cooldown.
///
/// Wraps the existing update.sh flow with a safety gate:
///   1. Pre-upgrade: fetch full export, parse counts, archive, emit SafetyPre.
///   2. Read the installed version + the latest advertised build.
///   3. `git pull` the companion-pi checkout (its picker decides what installs).
///   4. Stop Companion, run update.sh stable, start Companion, wait for healthy.
///   5. Post-upgrade: fetch full export, parse counts, emit SafetyPost.
///   6. If any count decreased: import the pre-upgrade snapshot, emit
///      SafetyRollback.
///   7. Compare the installed version before/after; anything not provably
///      installed is reported as an error, never as success.
pub async fn run_update(tx: mpsc::Sender<UpdateEvent>) -> bool {
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
            return false;
        }
    };
    let pre_counts = match crate::safety::count_from_companionconfig(&pre_bytes) {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("pre-upgrade parse failed: {e}"),
            }).await;
            return false;
        }
    };
    if let Err(e) = save_snapshot(&pre_bytes).await {
        let _ = tx.send(UpdateEvent::Error {
            message: format!("could not persist pre-upgrade snapshot: {e}"),
        }).await;
        return false;
    }
    let _ = tx.send(UpdateEvent::SafetyPre { counts: pre_counts }).await;

    // 2. Record the ground truth we compare against at the end.
    let pre_version = match crate::companion::read_installed_version().await {
        Ok(v) => v,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("cannot read installed version: {e}"),
            }).await;
            return false;
        }
    };
    // None means "we do not know" — never silently substituted with the current
    // version, which would turn an unverifiable run into a green "up to date".
    let latest_version = match crate::bitfocus::fetch_latest_stable_linux(&http).await {
        Ok(v) => Some(v),
        Err(e) => {
            tracing::warn!(error = %e, "could not fetch latest stable version");
            let _ = tx.send(UpdateEvent::Progress {
                message: format!("WARNING: could not fetch the list of available builds: {e}"),
            }).await;
            None
        }
    };
    tracing::info!(
        pre = %pre_version,
        latest = latest_version.as_deref().unwrap_or("unknown"),
        "update run starting"
    );

    // 3. Self-update the companion-pi tooling FIRST. Upstream's own
    //    `companion-update` wrapper does this; skipping it leaves an old version
    //    picker in place that silently refuses every newer major release.
    update_companionpi_checkout(&tx).await;

    // 4. Stop Companion, then run update.sh stable. update.sh deletes and
    //    recreates /opt/companion, so the service must not be running.
    //
    //    From here until the guard is disarmed, Companion is down. The guard
    //    starts it again from Drop, so even a panic or an early return cannot
    //    leave the rig dark; the marker file it writes lets a NEXT process
    //    (after a kill -9 or a `systemctl stop companion-updater` that tears
    //    down this cgroup mid-run) finish the job on startup.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Stopping companion service...".into(),
    }).await;
    let mut guard = CompanionDownGuard::arm().await;
    if let Err(e) = systemctl(&["stop", "companion"]).await {
        guard.disarm();
        let _ = tx.send(UpdateEvent::Error { message: e }).await;
        return false;
    }

    let _ = tx.send(UpdateEvent::Progress {
        message: "Starting update (stable channel)...".into(),
    }).await;
    let skipped = match run_update_script(&tx).await {
        Ok(skipped) => skipped,
        Err(e) => {
            // Never leave the rig with Companion down because the script failed.
            let _ = tx.send(UpdateEvent::Progress {
                message: "Update script failed — restarting Companion...".into(),
            }).await;
            let message = match systemctl(&["start", "companion"]).await {
                Ok(()) => {
                    guard.disarm();
                    format!("{e} (Companion was restarted.)")
                }
                Err(start_err) => format!(
                    "COMPANION IS STOPPED — start it manually: sudo systemctl start companion. \
                     Update failed: {e}. Restart also failed: {start_err}"
                ),
            };
            let _ = tx.send(UpdateEvent::Error { message }).await;
            return false;
        }
    };

    // 5. Start Companion + wait for healthy.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Starting companion service...".into(),
    }).await;
    if let Err(e) = systemctl(&["start", "companion"]).await {
        let _ = tx.send(UpdateEvent::Error {
            message: format!(
                "COMPANION IS STOPPED — start it manually: sudo systemctl start companion. \
                 Cause: {e}"
            ),
        }).await;
        return false;
    }
    guard.disarm();
    if let Err(e) =
        crate::safety::wait_until_healthy(&http, std::time::Duration::from_secs(60)).await
    {
        let _ = tx.send(UpdateEvent::Error {
            message: format!("Companion did not return to healthy: {e}"),
        }).await;
        return false;
    }

    // 6. Post-upgrade snapshot.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Taking post-upgrade snapshot...".into(),
    }).await;
    let post_bytes = match crate::safety::fetch_export(&http).await {
        Ok(b) => b,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("post-upgrade snapshot failed: {e}"),
            }).await;
            return false;
        }
    };
    let post_counts = match crate::safety::count_from_companionconfig(&post_bytes) {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("post-upgrade parse failed: {e}"),
            }).await;
            return false;
        }
    };
    let _ = tx.send(UpdateEvent::SafetyPost { counts: post_counts }).await;

    // 7. Compare; rollback if any decrease.
    if pre_counts.any_decreased(&post_counts) {
        let lost = pre_counts.lost(&post_counts);
        let _ = tx.send(UpdateEvent::Progress {
            message: format!(
                "Data loss detected (lost {} connections, {} buttons, {} triggers). Rolling back...",
                lost.connections, lost.buttons, lost.triggers
            ),
        }).await;

        if let Err(e) = crate::safety::import_companionconfig(pre_bytes).await {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("rollback import failed: {e}"),
            }).await;
            return false;
        }
        if let Err(e) =
            crate::safety::wait_until_healthy(&http, std::time::Duration::from_secs(60)).await
        {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("Companion did not return to healthy after rollback: {e}"),
            }).await;
            return false;
        }
        let _ = tx.send(UpdateEvent::SafetyRollback {
            message: "Data loss detected; rolled back to pre-upgrade state.".into(),
            lost,
        }).await;
        return false;
    }

    // 8. Did anything actually install? Exit status 0 does NOT mean it did, and
    //    a version we cannot read is not a version that went up.
    let new_version = match crate::companion::read_installed_version().await {
        Ok(v) => v,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!(
                    "Cannot verify the update: the installed version could not be read \
                     after the run ({e}). Your configuration was left untouched."
                ),
            }).await;
            return false;
        }
    };
    let outcome = classify_outcome(
        &pre_version,
        &new_version,
        latest_version.as_deref(),
        skipped,
    );
    tracing::info!(
        pre = %pre_version, post = %new_version,
        latest = latest_version.as_deref().unwrap_or("unknown"),
        skipped, ?outcome, "update run finished"
    );
    let summary = match outcome {
        Outcome::Failed(message) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("{message} Your configuration was left untouched."),
            }).await;
            return false;
        }
        Outcome::AlreadyLatest => format!(
            "Nothing to install — already running the latest build {}",
            crate::version::format(&new_version)
        ),
        Outcome::Applied { still_behind } => {
            let mut s = format!(
                "Update complete. Now running {} (was {})",
                crate::version::format(&new_version),
                crate::version::format(&pre_version)
            );
            if let Some(latest) = still_behind {
                s.push_str(&format!(" — note: {latest} is available, still behind"));
            }
            s
        }
    };

    let diff = crate::safety::Counts {
        connections: post_counts.connections.saturating_sub(pre_counts.connections),
        pages_with_content: post_counts
            .pages_with_content
            .saturating_sub(pre_counts.pages_with_content),
        buttons: post_counts.buttons.saturating_sub(pre_counts.buttons),
        triggers: post_counts.triggers.saturating_sub(pre_counts.triggers),
    };
    let _ = tx.send(UpdateEvent::Complete {
        message: summary,
        diff: Some(diff),
    }).await;
    true
}

/// Path of the marker written while Companion is deliberately stopped.
fn companion_down_marker() -> std::path::PathBuf {
    std::path::PathBuf::from(state_dir()).join("companion-stopped-by-updater")
}

/// Keeps the promise that Companion comes back up.
///
/// Armed right before `systemctl stop companion`, disarmed once it has been
/// started again. While armed it holds a marker file on disk AND starts
/// Companion from `Drop`, which covers the two ways the ordinary code path can
/// be bypassed: a panic inside the update task, and this process being killed
/// (`kill -9`, or the `systemctl stop companion-updater` that a deploy runs,
/// which tears down the whole cgroup including update.sh). The marker is what
/// [`reconcile_companion_state`] reads on the next startup.
struct CompanionDownGuard {
    armed: bool,
}

impl CompanionDownGuard {
    async fn arm() -> Self {
        let marker = companion_down_marker();
        if let Some(parent) = marker.parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
        }
        if let Err(e) = tokio::fs::write(&marker, b"companion stopped for update\n").await {
            // Not fatal: Drop still restarts Companion in-process. Only the
            // cross-restart recovery is lost, so say so loudly.
            tracing::error!(error = %e, path = ?marker, "could not write companion-down marker");
        }
        Self { armed: true }
    }

    fn disarm(&mut self) {
        if self.armed {
            self.armed = false;
            let marker = companion_down_marker();
            if let Err(e) = std::fs::remove_file(&marker) {
                if e.kind() != std::io::ErrorKind::NotFound {
                    tracing::warn!(error = %e, path = ?marker, "could not remove companion-down marker");
                }
            }
        }
    }
}

impl Drop for CompanionDownGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        tracing::error!(
            "update task ended while Companion was stopped — starting it from the guard"
        );
        let ok = blocking_systemctl(&["start", "companion"]);
        if ok {
            self.disarm();
        } else {
            tracing::error!("COMPANION IS STOPPED and could not be started from the guard");
        }
    }
}

/// Synchronous `systemctl` for use from `Drop`, where awaiting is not possible.
fn blocking_systemctl(args: &[&str]) -> bool {
    let mut cmd = if use_sudo() {
        let mut c = std::process::Command::new("sudo");
        c.arg("systemctl");
        c
    } else {
        std::process::Command::new("systemctl")
    };
    cmd.args(args)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// On startup, finish an update run that was killed while Companion was down.
///
/// Only acts when the marker from [`CompanionDownGuard`] is present, so a
/// Companion an operator stopped by hand is never started behind their back.
pub fn reconcile_companion_state() {
    let marker = companion_down_marker();
    if !marker.exists() {
        return;
    }
    tracing::error!(
        path = ?marker,
        "found a companion-down marker: a previous update run was interrupted while \
         Companion was stopped — starting Companion now"
    );
    if blocking_systemctl(&["start", "companion"]) {
        let _ = std::fs::remove_file(&marker);
        tracing::info!("Companion started; marker cleared");
    } else {
        tracing::error!("COMPANION IS STOPPED and could not be started — start it manually");
    }
}

/// Run `systemctl <args>` (via sudo unless disabled), mapping any non-success
/// into a readable error.
async fn systemctl(args: &[&str]) -> Result<(), String> {
    let mut cmd = if use_sudo() {
        let mut c = Command::new("sudo");
        c.arg("systemctl");
        c
    } else {
        Command::new("systemctl")
    };
    tracing::info!(?args, "running systemctl");
    match cmd.args(args).status().await {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!("systemctl {} exited with {s}", args.join(" "))),
        Err(e) => Err(format!("systemctl {} failed: {e}", args.join(" "))),
    }
}

/// `git pull` the companion-pi checkout so the version picker that update.sh
/// invokes is the current one.
///
/// A failure here is reported but not fatal: the run continues and the
/// end-of-run version check catches a resulting no-op, which is a far more
/// useful error than aborting on a transient network hiccup.
async fn update_companionpi_checkout(tx: &mpsc::Sender<UpdateEvent>) {
    let _ = tx.send(UpdateEvent::Progress {
        message: "Updating companion-pi installer (git pull)...".into(),
    }).await;
    let dir = companionpi_dir();
    let mut cmd = if use_sudo() {
        let mut c = Command::new("sudo");
        c.arg("git");
        c
    } else {
        Command::new("git")
    };
    let output = cmd.args(["-C", &dir, "pull", "--ff-only"]).output().await;
    match output {
        Ok(out) => {
            for stream in [&out.stdout, &out.stderr] {
                for line in String::from_utf8_lossy(stream).lines() {
                    if !line.trim().is_empty() {
                        let _ = tx.send(UpdateEvent::Progress {
                            message: line.to_string(),
                        }).await;
                    }
                }
            }
            if out.status.success() {
                tracing::info!("companion-pi checkout updated");
            } else {
                tracing::warn!(status = %out.status, "git pull of companion-pi failed");
                let _ = tx.send(UpdateEvent::Progress {
                    message: format!(
                        "WARNING: could not update the companion-pi installer ({}). \
                         Continuing with the installed copy.",
                        out.status
                    ),
                }).await;
            }
        }
        Err(e) => {
            tracing::warn!(error = %e, "could not run git pull for companion-pi");
            let _ = tx.send(UpdateEvent::Progress {
                message: format!("WARNING: could not run git pull in {dir}: {e}"),
            }).await;
        }
    }
}

/// Directory systemd gives us for persistent state (`StateDirectory=`).
fn state_dir() -> String {
    std::env::var("STATE_DIRECTORY").unwrap_or_else(|_| "/var/lib/companion-updater".to_string())
}

async fn save_snapshot(bytes: &[u8]) -> Result<(), String> {
    let state_dir = state_dir();
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
    // Microsecond suffix avoids a collision if two archives ever land in the
    // same second (the 5-minute cooldown makes this practically impossible,
    // but the suffix is free insurance).
    let ts = chrono::Local::now().format("%Y%m%dT%H%M%S%6f").to_string();
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

/// Run `update.sh stable`, streaming its output.
///
/// Returns whether the script announced that it installed nothing (see
/// [`is_skip_marker`]) — the exit status is 0 either way.
async fn run_update_script(tx: &mpsc::Sender<UpdateEvent>) -> Result<bool, String> {
    let script = update_script();
    let mut cmd = if use_sudo() {
        let mut c = Command::new("sudo");
        c.arg("bash");
        c
    } else {
        Command::new("bash")
    };
    let mut child = cmd
        .args([script.as_str(), "stable"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn {script}: {e}"))?;

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    let skipped = Arc::new(AtomicBool::new(false));

    let stdout_handle = tokio::spawn(stream_lines(stdout, tx.clone(), Arc::clone(&skipped)));
    let stderr_handle = tokio::spawn(stream_lines(stderr, tx.clone(), Arc::clone(&skipped)));

    let status = child.wait().await;
    let _ = stdout_handle.await;
    let _ = stderr_handle.await;
    let status = status.map_err(|e| format!("{script} wait failed: {e}"))?;
    if !status.success() {
        return Err(format!("{script} exited with {status}"));
    }
    Ok(skipped.load(AtomicOrdering::SeqCst))
}

/// Forward one output stream to the browser, flagging skip markers on the way.
///
/// Sends are non-blocking on purpose. A browser tab that stopped reading (a
/// half-open connection after a Wi-Fi drop) would otherwise fill the channel,
/// then the pipe, then block update.sh itself — with Companion stopped and no
/// timeout. Progress lines are advisory and every one of them is in the journal
/// via `tracing`, so dropping them when the client cannot keep up is the safe
/// trade.
async fn stream_lines<R>(reader: R, tx: mpsc::Sender<UpdateEvent>, skipped: Arc<AtomicBool>)
where
    R: tokio::io::AsyncRead + Unpin,
{
    let mut lines = BufReader::new(reader).lines();
    let mut dropped = 0usize;
    while let Ok(Some(line)) = lines.next_line().await {
        tracing::info!(target: "update_script", "{line}");
        if is_skip_marker(&line) {
            skipped.store(true, AtomicOrdering::SeqCst);
        }
        if tx.try_send(UpdateEvent::Progress { message: line }).is_err() {
            dropped += 1;
        }
    }
    if dropped > 0 {
        tracing::warn!(dropped, "progress lines dropped: the client was not reading");
    }
}

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
    fn skip_marker_matches_update_script_skip_line() {
        assert!(is_skip_marker("Skipping update"));
    }

    #[test]
    fn skip_marker_matches_picker_no_build_line() {
        assert!(is_skip_marker("No matching stable build was found!"));
    }

    #[test]
    fn skip_marker_matches_already_installed_line() {
        assert!(is_skip_marker(
            "The latest build of stable (v5.0.2+9665-stable) is already installed"
        ));
    }

    #[test]
    fn skip_marker_ignores_ordinary_progress() {
        assert!(!is_skip_marker("Extracting..."));
        assert!(!is_skip_marker("Installing from https://example.com/x.tar.gz"));
    }

    #[test]
    fn skip_marker_ignores_apt_already_installed_chatter() {
        // update.sh installs OS packages; their "already installed" lines must
        // not be mistaken for the picker declining to install Companion.
        assert!(!is_skip_marker("libfontconfig1 is already installed"));
    }

    /// Regression: companion-snv reported "Update complete. Now running v4.3.4"
    /// while v5.0.2 was the latest stable and nothing had been installed — the
    /// stale companion-pi picker printed "No matching stable build was found!",
    /// update.sh printed "Skipping update" and exited 0.
    #[test]
    fn outcome_failed_when_skipped_and_newer_version_available() {
        let outcome = classify_outcome("4.3.4", "4.3.4", Some("v5.0.2"), true);
        match outcome {
            Outcome::Failed(msg) => {
                assert!(msg.contains("4.3.4"), "got {msg}");
                assert!(msg.contains("5.0.2"), "got {msg}");
            }
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn outcome_failed_when_version_unchanged_without_marker() {
        assert!(matches!(
            classify_outcome("4.3.4", "4.3.4", Some("v5.0.2"), false),
            Outcome::Failed(_)
        ));
    }

    #[test]
    fn outcome_applied_when_version_changed() {
        assert_eq!(
            classify_outcome("4.3.4", "5.0.2", Some("v5.0.2"), false),
            Outcome::Applied { still_behind: None }
        );
    }

    #[test]
    fn outcome_applied_even_if_a_skip_marker_appeared_but_version_moved() {
        assert_eq!(
            classify_outcome("4.3.4", "5.0.2", Some("v5.0.2"), true),
            Outcome::Applied { still_behind: None }
        );
    }

    #[test]
    fn outcome_applied_flags_a_version_that_is_still_behind() {
        assert_eq!(
            classify_outcome("4.3.4", "4.3.5", Some("v5.0.2"), false),
            Outcome::Applied {
                still_behind: Some("v5.0.2".into())
            }
        );
    }

    #[test]
    fn outcome_already_latest_when_unchanged_and_current_is_newest() {
        assert_eq!(
            classify_outcome("5.0.2+9300", "5.0.2+9300", Some("v5.0.2"), true),
            Outcome::AlreadyLatest
        );
    }

    #[test]
    fn outcome_already_latest_when_installed_is_ahead_of_latest() {
        assert_eq!(
            classify_outcome("5.1.0", "5.1.0", Some("v5.0.2"), false),
            Outcome::AlreadyLatest
        );
    }

    /// A version we could not read parses to nothing, which used to compare as
    /// 0.0.0 — i.e. "lower than pre", i.e. a reported success for a run nobody
    /// verified.
    #[test]
    fn outcome_failed_when_post_version_is_unreadable() {
        match classify_outcome("4.3.4", "unknown", Some("v5.0.2"), false) {
            Outcome::Failed(msg) => assert!(msg.contains("not a version number"), "got {msg}"),
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    /// A downgrade is a changed version, but never a success.
    #[test]
    fn outcome_failed_when_version_went_backwards() {
        match classify_outcome("4.3.4", "4.2.6", Some("v5.0.2"), false) {
            Outcome::Failed(msg) => assert!(msg.contains("BACKWARDS"), "got {msg}"),
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    /// Without the list of builds there is no proof anything was installed, so
    /// an unchanged version must not be dressed up as "already latest".
    #[test]
    fn outcome_failed_when_unchanged_and_latest_is_unknown() {
        match classify_outcome("4.3.4", "4.3.4", None, true) {
            Outcome::Failed(msg) => assert!(msg.contains("could not be fetched"), "got {msg}"),
            other => panic!("expected Failed, got {other:?}"),
        }
    }

    #[test]
    fn outcome_applied_when_version_moved_even_with_latest_unknown() {
        assert_eq!(
            classify_outcome("4.3.4", "5.0.2", None, false),
            Outcome::Applied { still_behind: None }
        );
    }

    /// Write an executable throwaway script and point run_update_script at it.
    fn fake_update_script(name: &str, body: &str) -> std::path::PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = std::env::temp_dir().join(format!("companion-updater-test-{name}.sh"));
        std::fs::write(&path, body).expect("write fake script");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
            .expect("chmod fake script");
        path
    }

    async fn run_fake_script(name: &str, body: &str) -> (Result<bool, String>, Vec<String>) {
        let path = fake_update_script(name, body);
        std::env::set_var("COMPANION_UPDATE_SCRIPT", &path);
        std::env::set_var("COMPANION_UPDATE_SUDO", "0");

        let (tx, mut rx) = mpsc::channel::<UpdateEvent>(64);
        let result = run_update_script(&tx).await;
        drop(tx);

        std::env::remove_var("COMPANION_UPDATE_SCRIPT");
        std::env::remove_var("COMPANION_UPDATE_SUDO");
        let _ = std::fs::remove_file(&path);

        let mut lines = vec![];
        while let Ok(event) = rx.try_recv() {
            if let UpdateEvent::Progress { message } = event {
                lines.push(message);
            }
        }
        (result, lines)
    }

    /// Drives the real script runner against throwaway scripts. All three cases
    /// live in ONE test because they set process-wide env vars, and cargo runs
    /// tests of the same binary in parallel threads.
    #[tokio::test]
    async fn script_runner_distinguishes_skip_install_and_failure() {
        // The reported bug at the script boundary: update.sh declines to install
        // anything and still exits 0. The runner must say so.
        let (result, lines) = run_fake_script(
            "skip",
            "#!/usr/bin/env bash\necho 'No matching stable build was found!' >&2\n\
             echo 'Skipping update'\nexit 0\n",
        )
        .await;
        assert_eq!(result, Ok(true), "lines: {lines:?}");
        assert!(
            lines.iter().any(|l| l.contains("Skipping update")),
            "output was not streamed: {lines:?}"
        );

        let (result, lines) = run_fake_script(
            "install",
            "#!/usr/bin/env bash\necho 'Installing from https://example.invalid/x.tar.gz'\n\
             echo 'Extracting...'\nexit 0\n",
        )
        .await;
        assert_eq!(result, Ok(false), "lines: {lines:?}");

        let (result, _) =
            run_fake_script("fail", "#!/usr/bin/env bash\necho 'boom' >&2\nexit 3\n").await;
        match result {
            Err(msg) => assert!(msg.contains("exited with"), "got {msg}"),
            Ok(v) => panic!("expected Err, got Ok({v})"),
        }
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
