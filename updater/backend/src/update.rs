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

const UPDATE_SCRIPT: &str = "/usr/local/src/companionpi/update.sh";
const COMPANIONPI_DIR: &str = "/usr/local/src/companionpi";

/// Lines the companion-pi tooling prints when it deliberately installs nothing.
///
/// `update.sh` prints "Skipping update" whenever the version picker wrote no
/// selection file, and the picker itself prints "No matching <branch> build was
/// found!" when its version whitelist rejects every published build. Both paths
/// exit 0, so the exit status alone cannot tell success from a silent no-op.
const SKIP_MARKERS: [&str; 3] = [
    "Skipping update",
    "build was found!",
    "is already installed",
];

/// What actually happened to `/opt/companion` during a run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The installed version changed — the upgrade landed.
    Applied,
    /// Nothing changed, and nothing needed to: we already run the newest build.
    AlreadyLatest,
    /// Nothing changed although a newer build exists. The reason is in the message.
    Failed(String),
}

/// True when `line` is one of the companion-pi "installed nothing" markers.
pub fn is_skip_marker(line: &str) -> bool {
    SKIP_MARKERS.iter().any(|m| line.contains(m))
}

/// Decide whether an update run actually installed anything.
///
/// `pre`/`post` are the versions read from `/opt/companion/package.json` around
/// the run; `latest` is the newest stable build Bitfocus advertises; `skipped`
/// is true when any [`is_skip_marker`] line appeared in the script output.
///
/// A changed version always wins — the markers are advisory, the installed
/// version is the ground truth.
pub fn classify_outcome(pre: &str, post: &str, latest: &str, skipped: bool) -> Outcome {
    if crate::version::compare(pre, post) != std::cmp::Ordering::Equal {
        return Outcome::Applied;
    }
    if !crate::version::is_update_available(post, latest) {
        return Outcome::AlreadyLatest;
    }
    let reason = if skipped {
        "the companion-pi installer reported it had no matching build to install \
         (its version whitelist may not cover this release yet)"
    } else {
        "the update script exited successfully but /opt/companion is unchanged"
    };
    Outcome::Failed(format!(
        "Update did NOT apply: still running {} while {} is available — {}.",
        crate::version::format(post),
        crate::version::format(latest),
        reason
    ))
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
    let pre_counts = match crate::safety::count_from_companionconfig(&pre_bytes) {
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

    // 2. Record the ground truth we compare against at the end.
    let pre_version = match crate::companion::read_installed_version().await {
        Ok(v) => v,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("cannot read installed version: {e}"),
            }).await;
            return;
        }
    };
    let latest_version = crate::bitfocus::fetch_latest_stable_linux(&http)
        .await
        .unwrap_or_else(|e| {
            tracing::warn!(error = %e, "could not fetch latest stable version");
            pre_version.clone()
        });
    tracing::info!(pre = %pre_version, latest = %latest_version, "update run starting");

    // 3. Self-update the companion-pi tooling FIRST. Upstream's own
    //    `companion-update` wrapper does this; skipping it leaves an old version
    //    picker in place that silently refuses every newer major release.
    update_companionpi_checkout(&tx).await;

    // 4. Stop Companion, then run update.sh stable. update.sh deletes and
    //    recreates /opt/companion, so the service must not be running.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Stopping companion service...".into(),
    }).await;
    if let Err(e) = systemctl(&["stop", "companion"]).await {
        let _ = tx.send(UpdateEvent::Error { message: e }).await;
        return;
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
            let _ = systemctl(&["start", "companion"]).await;
            let _ = tx.send(UpdateEvent::Error { message: e }).await;
            return;
        }
    };

    // 5. Start Companion + wait for healthy.
    let _ = tx.send(UpdateEvent::Progress {
        message: "Starting companion service...".into(),
    }).await;
    if let Err(e) = systemctl(&["start", "companion"]).await {
        let _ = tx.send(UpdateEvent::Error { message: e }).await;
        return;
    }
    if let Err(e) =
        crate::safety::wait_until_healthy(&http, std::time::Duration::from_secs(60)).await
    {
        let _ = tx.send(UpdateEvent::Error {
            message: format!("Companion did not return to healthy: {e}"),
        }).await;
        return;
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
            return;
        }
    };
    let post_counts = match crate::safety::count_from_companionconfig(&post_bytes) {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(UpdateEvent::Error {
                message: format!("post-upgrade parse failed: {e}"),
            }).await;
            return;
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

    // 8. Did anything actually install? Exit status 0 does NOT mean it did.
    let new_version = crate::companion::read_installed_version()
        .await
        .unwrap_or_else(|_| "unknown".into());
    let outcome = classify_outcome(&pre_version, &new_version, &latest_version, skipped);
    tracing::info!(
        pre = %pre_version, post = %new_version, latest = %latest_version,
        skipped, ?outcome, "update run finished"
    );
    let summary = match outcome {
        Outcome::Failed(message) => {
            let _ = tx.send(UpdateEvent::Error { message }).await;
            return;
        }
        Outcome::AlreadyLatest => format!(
            "Nothing to install — already running the latest build {}",
            crate::version::format(&new_version)
        ),
        Outcome::Applied => format!(
            "Update complete. Now running {} (was {})",
            crate::version::format(&new_version),
            crate::version::format(&pre_version)
        ),
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
}

/// Run `sudo systemctl <args>`, mapping any non-success into a readable error.
async fn systemctl(args: &[&str]) -> Result<(), String> {
    let mut argv = vec!["systemctl"];
    argv.extend_from_slice(args);
    tracing::info!(?argv, "running systemctl");
    match Command::new("sudo").args(&argv).status().await {
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
    let output = Command::new("sudo")
        .args(["git", "-C", COMPANIONPI_DIR, "pull", "--ff-only"])
        .output()
        .await;
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
                message: format!("WARNING: could not run git pull in {COMPANIONPI_DIR}: {e}"),
            }).await;
        }
    }
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
    let mut child = Command::new("sudo")
        .args(["bash", UPDATE_SCRIPT, "stable"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn update.sh: {e}"))?;

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    let skipped = Arc::new(AtomicBool::new(false));

    let tx_out = tx.clone();
    let skipped_out = Arc::clone(&skipped);
    let stdout_handle = tokio::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            tracing::info!(target: "update_script", "{line}");
            if is_skip_marker(&line) {
                skipped_out.store(true, AtomicOrdering::SeqCst);
            }
            let _ = tx_out.send(UpdateEvent::Progress { message: line }).await;
        }
    });
    let tx_err = tx.clone();
    let skipped_err = Arc::clone(&skipped);
    let stderr_handle = tokio::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            tracing::info!(target: "update_script", "{line}");
            if is_skip_marker(&line) {
                skipped_err.store(true, AtomicOrdering::SeqCst);
            }
            let _ = tx_err.send(UpdateEvent::Progress { message: line }).await;
        }
    });

    let status = child.wait().await.map_err(|e| format!("update.sh wait failed: {e}"))?;
    let _ = stdout_handle.await;
    let _ = stderr_handle.await;
    if !status.success() {
        return Err(format!("update.sh exited with {status}"));
    }
    Ok(skipped.load(AtomicOrdering::SeqCst))
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
    fn skip_marker_ignores_ordinary_progress() {
        assert!(!is_skip_marker("Extracting..."));
        assert!(!is_skip_marker("Installing from https://example.com/x.tar.gz"));
    }

    /// Regression: companion-snv reported "Update complete. Now running v4.3.4"
    /// while v5.0.2 was the latest stable and nothing had been installed — the
    /// stale companion-pi picker printed "No matching stable build was found!",
    /// update.sh printed "Skipping update" and exited 0.
    #[test]
    fn outcome_failed_when_skipped_and_newer_version_available() {
        let outcome = classify_outcome("4.3.4", "4.3.4", "v5.0.2", true);
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
            classify_outcome("4.3.4", "4.3.4", "v5.0.2", false),
            Outcome::Failed(_)
        ));
    }

    #[test]
    fn outcome_applied_when_version_changed() {
        assert_eq!(
            classify_outcome("4.3.4", "5.0.2", "v5.0.2", false),
            Outcome::Applied
        );
    }

    #[test]
    fn outcome_applied_even_if_a_skip_marker_appeared_but_version_moved() {
        assert_eq!(
            classify_outcome("4.3.4", "5.0.2", "v5.0.2", true),
            Outcome::Applied
        );
    }

    #[test]
    fn outcome_already_latest_when_unchanged_and_current_is_newest() {
        assert_eq!(
            classify_outcome("5.0.2+9300", "5.0.2+9300", "v5.0.2", true),
            Outcome::AlreadyLatest
        );
    }

    #[test]
    fn outcome_already_latest_when_installed_is_ahead_of_latest() {
        assert_eq!(
            classify_outcome("5.1.0", "5.1.0", "v5.0.2", false),
            Outcome::AlreadyLatest
        );
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
