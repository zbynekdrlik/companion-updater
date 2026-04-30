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
