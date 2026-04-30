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
            pages_with_content: self
                .pages_with_content
                .saturating_sub(post.pages_with_content),
            buttons: self.buttons.saturating_sub(post.buttons),
            triggers: self.triggers.saturating_sub(post.triggers),
        }
    }
}

/// Parse a Companion `.companionconfig` byte stream and compute `Counts`.
///
/// `.companionconfig` files (and the bytes returned by `/int/export/full`) are
/// gzip-compressed JSON; raw JSON is also accepted for tests and for callers
/// that have already decompressed. The format is auto-detected from the
/// gzip magic bytes (`1f 8b`).
///
/// Schema (Companion v4.3, observed in real exports):
///   { "instances": { ... },                 // map of connection id -> spec
///     "pages":     [ ... ] | { ... },       // list or map of page entries
///     "triggers":  { ... } }                // map of trigger id -> spec
///
/// Each page entry contains a `controls` map (row -> col -> bank id). A page
/// "has content" if `controls` has at least one row with at least one column.
pub fn count_from_json(json: &[u8]) -> Result<Counts, String> {
    let owned;
    let bytes: &[u8] = if json.len() >= 2 && json[0] == 0x1f && json[1] == 0x8b {
        use std::io::Read as _;
        let mut decoder = flate2::read::GzDecoder::new(json);
        let mut out = Vec::with_capacity(json.len() * 4);
        decoder
            .read_to_end(&mut out)
            .map_err(|e| format!("decompress companionconfig: {e}"))?;
        owned = out;
        &owned
    } else {
        json
    };

    let v: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|e| format!("parse companionconfig: {e}"))?;

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

/// Local URL of the running Companion admin server.
const COMPANION_BASE: &str = "http://127.0.0.1:8000";

/// Companion tRPC WebSocket endpoint (same host/port as the HTTP admin server,
/// just upgraded to WS at the `/trpc` path).
const COMPANION_TRPC_WS: &str = "ws://127.0.0.1:8000/trpc";

/// Maximum size of a single base64 upload chunk (raw bytes, before encoding).
/// 64 KiB matches what the official UI uses and stays well under any tRPC
/// message-size limits.
const IMPORT_CHUNK_BYTES: usize = 64 * 1024;

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
        return Err(format!("export endpoint returned {}", resp.status()));
    }
    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| format!("read export body: {e}"))
}

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
                continue;
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

    // 3. complete
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
    let id = next_id;
    let _ = next_id; // suppress unused after final increment
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
        match client
            .get(&url)
            .timeout(std::time::Duration::from_secs(3))
            .send()
            .await
        {
            Ok(r) if r.status().is_success() => return Ok(()),
            _ => {
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        }
    }
    Err(format!("Companion not healthy within {:?}", timeout))
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
    fn counts_from_gzipped_companionconfig() {
        // `/int/export/full` returns gzip-compressed JSON without setting
        // `Content-Encoding: gzip`, so `count_from_json` must transparently
        // decompress when it sees the gzip magic bytes.
        use flate2::{write::GzEncoder, Compression};
        use std::io::Write as _;
        let raw = br#"{"instances":{"a":{},"b":{}},"triggers":{"t":{}},"pages":[{"controls":{"0":{"0":"x","1":"y"}}}]}"#;
        let mut enc = GzEncoder::new(Vec::new(), Compression::fast());
        enc.write_all(raw).unwrap();
        let gzipped = enc.finish().unwrap();
        // Sanity: gzip magic present.
        assert_eq!(&gzipped[..2], &[0x1f, 0x8b]);
        let c = count_from_json(&gzipped).unwrap();
        assert_eq!(c.connections, 2);
        assert_eq!(c.triggers, 1);
        assert_eq!(c.buttons, 2);
        assert_eq!(c.pages_with_content, 1);
    }

    #[test]
    fn any_decreased_detects_drop() {
        let pre = Counts {
            connections: 41,
            pages_with_content: 20,
            buttons: 200,
            triggers: 47,
        };
        let post_same = pre;
        let post_more = Counts {
            connections: 42,
            ..pre
        };
        let post_less_buttons = Counts {
            buttons: 199,
            ..pre
        };
        assert!(!pre.any_decreased(&post_same));
        assert!(!pre.any_decreased(&post_more));
        assert!(pre.any_decreased(&post_less_buttons));
    }

    #[test]
    fn lost_clamps_gains_to_zero() {
        let pre = Counts {
            connections: 10,
            pages_with_content: 5,
            buttons: 50,
            triggers: 8,
        };
        let post = Counts {
            connections: 12,
            pages_with_content: 5,
            buttons: 48,
            triggers: 8,
        };
        let lost = pre.lost(&post);
        assert_eq!(lost.connections, 0);
        assert_eq!(lost.buttons, 2);
    }
}
