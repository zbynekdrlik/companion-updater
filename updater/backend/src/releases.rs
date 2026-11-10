//! Companion release notes from GitHub.
//!
//! Endpoint: `https://api.github.com/repos/bitfocus/companion/releases?per_page=30`
//! Each release has `tag_name` (`"v5.1.2"`), `name`, markdown `body`,
//! `published_at`, `html_url`, `draft` and `prerelease`.
//!
//! The dashboard shows what an update would bring: every stable release newer
//! than the installed version and not newer than the latest stable Linux build.
//! When Companion is up to date it shows the installed version's own notes.
//! GitHub allows 60 unauthenticated requests per hour per IP, so the list is
//! cached (see `ReleaseCache`).

use std::cmp::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use pulldown_cmark::{html, Event, Options, Parser};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

const API_URL: &str = "https://api.github.com/repos/bitfocus/companion/releases?per_page=30";
/// GitHub refuses requests without a User-Agent.
const USER_AGENT: &str = "companion-updater";
pub const CACHE_TTL: Duration = Duration::from_secs(600);

#[derive(Debug, Clone, Deserialize)]
pub struct GithubRelease {
    pub tag_name: String,
    #[serde(default)]
    pub body: Option<String>,
    #[serde(default)]
    pub published_at: Option<String>,
    #[serde(default)]
    pub html_url: String,
    #[serde(default)]
    pub draft: bool,
    #[serde(default)]
    pub prerelease: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ReleaseNote {
    pub version: String,
    /// `YYYY-MM-DD`, or empty when GitHub gives no date.
    pub published: String,
    pub url: String,
    /// The notes rendered to HTML. Raw HTML from the notes is escaped.
    pub html: String,
}

/// When the list was fetched, and the list.
type Cached = Option<(Instant, Vec<GithubRelease>)>;

/// The releases list fetched from GitHub, kept for `CACHE_TTL`.
#[derive(Clone, Default)]
pub struct ReleaseCache {
    inner: Arc<Mutex<Cached>>,
}

impl ReleaseCache {
    /// The cached list while it is fresh, otherwise a new fetch. A failed
    /// fetch falls back to a stale list when there is one (logged), so a
    /// GitHub hiccup or rate limit does not blank the notes.
    pub async fn get(&self, client: &reqwest::Client) -> Result<Vec<GithubRelease>, String> {
        let mut guard = self.inner.lock().await;
        if let Some((at, list)) = guard.as_ref() {
            if at.elapsed() < CACHE_TTL {
                return Ok(list.clone());
            }
        }
        match fetch(client).await {
            Ok(list) => {
                tracing::info!(releases = list.len(), "release notes fetched from GitHub");
                *guard = Some((Instant::now(), list.clone()));
                Ok(list)
            }
            Err(e) => match guard.as_ref() {
                Some((at, list)) => {
                    tracing::warn!(error = %e, age_s = at.elapsed().as_secs(), "release notes fetch failed; serving the cached list");
                    Ok(list.clone())
                }
                None => {
                    tracing::warn!(error = %e, "release notes fetch failed; nothing cached");
                    Err(e)
                }
            },
        }
    }
}

async fn fetch(client: &reqwest::Client) -> Result<Vec<GithubRelease>, String> {
    let resp = client
        .get(API_URL)
        .header("User-Agent", USER_AGENT)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("GitHub request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("GitHub returned {}", resp.status()));
    }
    resp.json()
        .await
        .map_err(|e| format!("Failed to parse GitHub releases: {e}"))
}

/// Stable releases newer than `current` and not newer than `latest`, newest
/// first. When there are none, the release matching `current` (if GitHub has it).
pub fn select(releases: &[GithubRelease], current: &str, latest: &str) -> Vec<ReleaseNote> {
    let stable: Vec<&GithubRelease> = releases
        .iter()
        .filter(|r| !r.draft && !r.prerelease)
        .collect();
    let mut newer: Vec<&GithubRelease> = stable
        .iter()
        .copied()
        .filter(|r| {
            crate::version::compare(&r.tag_name, current) == Ordering::Greater
                && crate::version::compare(&r.tag_name, latest) != Ordering::Greater
        })
        .collect();
    if newer.is_empty() {
        newer = stable
            .iter()
            .copied()
            .filter(|r| crate::version::compare(&r.tag_name, current) == Ordering::Equal)
            .take(1)
            .collect();
    }
    newer.sort_by(|a, b| crate::version::compare(&b.tag_name, &a.tag_name));
    newer.into_iter().map(to_note).collect()
}

fn to_note(r: &GithubRelease) -> ReleaseNote {
    ReleaseNote {
        version: crate::version::format(&r.tag_name),
        published: r
            .published_at
            .as_deref()
            .map(|d| d.chars().take(10).collect())
            .unwrap_or_default(),
        url: r.html_url.clone(),
        html: render(r.body.as_deref().unwrap_or("")),
    }
}

/// Drop the "Downloads / Donate" preamble: keep the text from the first line
/// that names the release notes. Notes without that heading are kept whole.
pub fn trim_preamble(body: &str) -> &str {
    let mut offset = 0;
    for line in body.split_inclusive('\n') {
        if line.to_ascii_lowercase().contains("release notes") {
            return &body[offset..];
        }
        offset += line.len();
    }
    body
}

/// Markdown to HTML. Raw HTML in the notes becomes text, so a release body can
/// never inject markup into the dashboard.
pub fn render(body: &str) -> String {
    let parser = Parser::new_ext(
        trim_preamble(body),
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH,
    )
    .map(|event| match event {
        Event::Html(raw) | Event::InlineHtml(raw) => Event::Text(raw),
        other => other,
    });
    let mut out = String::new();
    html::push_html(&mut out, parser);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(tag: &str, body: &str) -> GithubRelease {
        GithubRelease {
            tag_name: tag.into(),
            body: Some(body.into()),
            published_at: Some("2026-11-04T18:00:49Z".into()),
            html_url: format!("https://github.com/bitfocus/companion/releases/tag/{tag}"),
            draft: false,
            prerelease: false,
        }
    }

    fn versions(notes: &[ReleaseNote]) -> Vec<String> {
        notes.iter().map(|n| n.version.clone()).collect()
    }

    #[test]
    fn shows_every_stable_release_an_update_would_bring_newest_first() {
        let mut beta = rel("v5.2.0-beta.1", "");
        beta.prerelease = true;
        let mut draft = rel("v5.1.3", "");
        draft.draft = true;
        let releases = vec![
            beta,
            draft,
            rel("v5.0.7", ""),
            rel("v5.1.2", ""),
            rel("v5.1.0", ""),
            rel("v5.1.1", ""),
        ];
        let notes = select(&releases, "5.0.7+8957", "v5.1.2");
        assert_eq!(versions(&notes), vec!["v5.1.2", "v5.1.1", "v5.1.0"]);
    }

    #[test]
    fn a_release_newer_than_the_latest_linux_build_is_not_shown() {
        let releases = vec![rel("v5.1.3", ""), rel("v5.1.2", ""), rel("v5.1.1", "")];
        let notes = select(&releases, "5.1.1", "v5.1.2");
        assert_eq!(versions(&notes), vec!["v5.1.2"]);
    }

    #[test]
    fn up_to_date_shows_the_installed_versions_own_notes() {
        let releases = vec![rel("v5.1.2", "x"), rel("v5.1.1", "y")];
        let notes = select(&releases, "5.1.2+10019", "v5.1.2");
        assert_eq!(versions(&notes), vec!["v5.1.2"]);
        assert_eq!(notes[0].published, "2026-11-04");
        assert_eq!(
            notes[0].url,
            "https://github.com/bitfocus/companion/releases/tag/v5.1.2"
        );
    }

    #[test]
    fn nothing_known_gives_an_empty_list() {
        assert!(select(&[rel("v4.3.1", "")], "5.1.2", "v5.1.2").is_empty());
    }

    #[test]
    fn the_downloads_and_donate_preamble_is_dropped() {
        let body = "## 📦  Downloads available at\n* https://user.bitfocus.io/download\n\n## Companion v5.1.2 - Release Notes\n\n### 🐞 BUG FIXES\n\n- optimise button drawing\n";
        let html = render(body);
        assert!(!html.contains("Downloads"), "{html}");
        assert!(
            html.contains("<h2>Companion v5.1.2 - Release Notes</h2>"),
            "{html}"
        );
        assert!(html.contains("<li>optimise button drawing</li>"), "{html}");
    }

    #[test]
    fn notes_without_the_heading_are_kept_whole() {
        assert_eq!(
            trim_preamble("- a fix\n- another\n"),
            "- a fix\n- another\n"
        );
    }

    #[test]
    fn raw_html_in_the_notes_is_shown_as_text_not_markup() {
        let html = render("Release notes\n\n<script>alert(1)</script>\n\ntext with <img src=x onerror=alert(1)> inline\n");
        assert!(!html.contains("<script>"), "{html}");
        assert!(!html.contains("<img"), "{html}");
        assert!(html.contains("&lt;script&gt;"), "{html}");
    }

    #[test]
    fn parses_the_real_github_shape() {
        let json = r#"[{"tag_name":"v5.1.2","name":"Bitfocus Companion v5.1.2","body":"b","published_at":"2026-11-04T18:00:49Z","html_url":"https://github.com/bitfocus/companion/releases/tag/v5.1.2","draft":false,"prerelease":false,"assets":[]}]"#;
        let parsed: Vec<GithubRelease> = serde_json::from_str(json).unwrap();
        assert_eq!(parsed[0].tag_name, "v5.1.2");
        assert!(!parsed[0].prerelease);
    }
}
