//! Run the companion-pi update script and stream its output.
//!
//! Spawns `sudo bash /usr/local/src/companionpi/update.sh stable` and yields
//! each line of combined stdout/stderr. After the child exits with success,
//! restarts the companion service.

use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;

const UPDATE_SCRIPT: &str = "/usr/local/src/companionpi/update.sh";

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
    #[allow(dead_code)]
    SafetyPre {
        counts: crate::safety::Counts,
    },
    #[allow(dead_code)]
    SafetyPost {
        counts: crate::safety::Counts,
    },
    #[allow(dead_code)]
    SafetyRollback {
        message: String,
        lost: crate::safety::Counts,
    },
}

/// Spawn the update process and stream lines through `tx`.
/// Closes `tx` when finished.
pub async fn run_update(tx: mpsc::Sender<UpdateEvent>) {
    let _ = tx
        .send(UpdateEvent::Progress {
            message: "Starting update (stable channel)...".into(),
        })
        .await;

    let mut child = match Command::new("sudo")
        .args(["bash", UPDATE_SCRIPT, "stable"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("Failed to spawn update.sh: {e}"),
                })
                .await;
            return;
        }
    };

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

    let status = match child.wait().await {
        Ok(s) => s,
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("update.sh wait failed: {e}"),
                })
                .await;
            return;
        }
    };

    let _ = stdout_handle.await;
    let _ = stderr_handle.await;

    if !status.success() {
        let _ = tx
            .send(UpdateEvent::Error {
                message: format!("update.sh exited with {status}"),
            })
            .await;
        return;
    }

    let _ = tx
        .send(UpdateEvent::Progress {
            message: "Restarting companion service...".into(),
        })
        .await;

    let restart = Command::new("sudo")
        .args(["systemctl", "restart", "companion"])
        .status()
        .await;

    match restart {
        Ok(s) if s.success() => {}
        Ok(s) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("systemctl restart exited with {s}"),
                })
                .await;
            return;
        }
        Err(e) => {
            let _ = tx
                .send(UpdateEvent::Error {
                    message: format!("systemctl restart failed: {e}"),
                })
                .await;
            return;
        }
    }

    // Allow Companion a moment to come up, then read the new version.
    tokio::time::sleep(Duration::from_secs(2)).await;

    let new_version = crate::companion::read_installed_version()
        .await
        .unwrap_or_else(|_| "unknown".to_string());

    let _ = tx
        .send(UpdateEvent::Complete {
            message: format!(
                "Update complete. Now running {}",
                crate::version::format(&new_version)
            ),
            diff: None,
        })
        .await;
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
