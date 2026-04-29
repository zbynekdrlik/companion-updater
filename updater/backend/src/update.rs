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
#[serde(tag = "type", rename_all = "lowercase")]
pub enum UpdateEvent {
    Progress { message: String },
    Complete { message: String },
    Error { message: String },
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
        })
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_progress_serializes() {
        let e = UpdateEvent::Progress {
            message: "hello".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"progress","message":"hello"}"#);
    }

    #[test]
    fn event_complete_serializes() {
        let e = UpdateEvent::Complete {
            message: "done".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"complete","message":"done"}"#);
    }

    #[test]
    fn event_error_serializes() {
        let e = UpdateEvent::Error {
            message: "boom".into(),
        };
        let s = serde_json::to_string(&e).unwrap();
        assert_eq!(s, r#"{"type":"error","message":"boom"}"#);
    }
}
