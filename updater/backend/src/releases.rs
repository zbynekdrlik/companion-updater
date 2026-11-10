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

use pulldown_cmark::{html, CowStr, Event, Options, Parser, Tag};
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

/// After a failed fetch, GitHub is not asked again for this long (rate limit:
/// a 403 must not be retried on every page load).
pub const RETRY_AFTER_FAILURE: Duration = Duration::from_secs(60);

#[derive(Default)]
struct CacheState {
    /// When the list was fetched, and the list.
    list: Option<(Instant, Vec<GithubRelease>)>,
    /// When the last fetch failed (cleared by a successful one).
    failed_at: Option<Instant>,
}

/// The releases list fetched from GitHub, kept for `CACHE_TTL`.
#[derive(Clone)]
pub struct ReleaseCache {
    url: String,
    ttl: Duration,
    retry: Duration,
    inner: Arc<Mutex<CacheState>>,
}

impl Default for ReleaseCache {
    fn default() -> Self {
        Self::new(API_URL, CACHE_TTL, RETRY_AFTER_FAILURE)
    }
}

impl ReleaseCache {
    pub fn new(url: &str, ttl: Duration, retry: Duration) -> Self {
        Self {
            url: url.to_string(),
            ttl,
            retry,
            inner: Arc::new(Mutex::new(CacheState::default())),
        }
    }

    /// The cached list while it is fresh, otherwise a new fetch. A failed
    /// fetch falls back to a stale list when there is one (logged), so a
    /// GitHub hiccup or rate limit does not blank the notes, and GitHub is
    /// not asked again for `retry`. The lock is not held during the fetch.
    pub async fn get(&self, client: &reqwest::Client) -> Result<Vec<GithubRelease>, String> {
        {
            let state = self.inner.lock().await;
            if let Some((at, list)) = state.list.as_ref() {
                if at.elapsed() < self.ttl {
                    return Ok(list.clone());
                }
            }
            if let Some(failed) = state.failed_at {
                if failed.elapsed() < self.retry {
                    return match state.list.as_ref() {
                        Some((_, list)) => Ok(list.clone()),
                        None => Err(format!(
                            "GitHub release list unavailable; next try in {} s",
                            self.retry.saturating_sub(failed.elapsed()).as_secs()
                        )),
                    };
                }
            }
        }
        let fetched = fetch(client, &self.url).await;
        let mut state = self.inner.lock().await;
        match fetched {
            Ok(list) => {
                tracing::info!(releases = list.len(), "release notes fetched from GitHub");
                state.list = Some((Instant::now(), list.clone()));
                state.failed_at = None;
                Ok(list)
            }
            Err(e) => {
                state.failed_at = Some(Instant::now());
                match state.list.as_ref() {
                    Some((at, list)) => {
                        tracing::warn!(error = %e, age_s = at.elapsed().as_secs(), "release notes fetch failed; serving the cached list");
                        Ok(list.clone())
                    }
                    None => {
                        tracing::warn!(error = %e, "release notes fetch failed; nothing cached");
                        Err(e)
                    }
                }
            }
        }
    }
}

async fn fetch(client: &reqwest::Client, url: &str) -> Result<Vec<GithubRelease>, String> {
    let resp = client
        .get(url)
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
        // A `-rc.1` style tag is a prerelease even when GitHub does not flag it:
        // version::parse would read its numbers as a higher version.
        // A `-rc.1` style tag is a prerelease even when GitHub does not flag it.
        .filter(|r| !r.draft && !r.prerelease && !r.tag_name.contains('-'))
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

/// Drop the "Downloads / Donate" preamble and the "Companion vX - Release Notes"
/// heading (the card shows the version already): keep the text after the first
/// HEADING that names the release notes. Notes without such a heading are kept whole.
pub fn trim_preamble(body: &str) -> &str {
    let mut offset = 0;
    for line in body.split_inclusive('\n') {
        offset += line.len();
        let t = line.trim_start();
        if t.starts_with('#') && t.to_ascii_lowercase().contains("release notes") {
            return &body[offset..];
        }
    }
    body
}

/// Only web and mail links survive; anything else (`javascript:`, `data:`,
/// relative paths into the dashboard) becomes `#`.
fn safe_url(url: CowStr<'_>) -> CowStr<'_> {
    let lower = url.trim().to_ascii_lowercase();
    if ["https://", "http://", "mailto:"]
        .iter()
        .any(|p| lower.starts_with(p))
    {
        url
    } else {
        CowStr::Borrowed("#")
    }
}

/// Markdown to HTML. Raw HTML in the notes becomes text and links keep only
/// web/mail schemes, so a release body can never inject script into the dashboard.
pub fn render(body: &str) -> String {
    let parser = Parser::new_ext(
        trim_preamble(body),
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH,
    )
    .map(|event| match event {
        Event::Html(raw) | Event::InlineHtml(raw) => Event::Text(raw),
        Event::Start(Tag::Link {
            link_type,
            dest_url,
            title,
            id,
        }) => Event::Start(Tag::Link {
            link_type,
            dest_url: safe_url(dest_url),
            title,
            id,
        }),
        Event::Start(Tag::Image {
            link_type,
            dest_url,
            title,
            id,
        }) => Event::Start(Tag::Image {
            link_type,
            dest_url: safe_url(dest_url),
            title,
            id,
        }),
        other => other,
    });
    let mut out = String::new();
    html::push_html(&mut out, parser);
    // Raw HTML is escaped above, so every `<a href=` here is a markdown link:
    // open it in a new tab instead of navigating the dashboard away.
    out.replace(
        "<a href=\"",
        "<a target=\"_blank\" rel=\"noopener noreferrer\" href=\"",
    )
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
            !html.contains("Release Notes"),
            "the version heading is dropped too: {html}"
        );
        assert!(html.contains("<h3>🐞 BUG FIXES</h3>"), "{html}");
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
    fn a_plain_line_mentioning_release_notes_is_not_the_heading() {
        let plain = "**Breaking:** read this first.\nSee the release notes below.\n- x\n";
        assert_eq!(trim_preamble(plain), plain);
        let body = "intro\nsee release notes\n## Companion v6.0.0 - Release Notes\n- x\n";
        assert_eq!(trim_preamble(body), "- x\n");
    }

    #[test]
    fn an_unflagged_rc_tag_is_not_a_stable_release() {
        // version::parse reads "v5.2.0-rc.1" as [5, 2, 1]: without the tag check it
        // would pass as a release between the installed and the latest version.
        let releases = vec![rel("v5.2.0-rc.1", ""), rel("v5.2.1", ""), rel("v5.1.1", "")];
        assert_eq!(
            versions(&select(&releases, "5.1.1", "v5.2.1")),
            vec!["v5.2.1"]
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
    fn only_web_and_mail_links_survive() {
        let html = render("Release notes\n\n[a](javascript:alert(1)) [b](JavaScript:x) [v](vbscript:msgbox) ![c](data:image/png;base64,AA) [d](https://github.com/bitfocus/companion/pull/4574) [e](/api/update/stream)\n");
        assert!(!html.to_ascii_lowercase().contains("javascript:"), "{html}");
        assert!(!html.contains("data:"), "{html}");
        assert!(!html.contains("/api/update/stream"), "{html}");
        assert!(!html.to_ascii_lowercase().contains("vbscript:"), "{html}");
        assert!(html.contains(r#"<a target="_blank" rel="noopener noreferrer" href="https://github.com/bitfocus/companion/pull/4574">d</a>"#), "{html}");
        assert_eq!(html.matches(r##"href="#""##).count(), 4, "{html}");
    }

    #[test]
    fn parses_the_real_github_shape() {
        let json = r#"[{"tag_name":"v5.1.2","name":"Bitfocus Companion v5.1.2","body":"b","published_at":"2026-11-04T18:00:49Z","html_url":"https://github.com/bitfocus/companion/releases/tag/v5.1.2","draft":false,"prerelease":false,"assets":[]}]"#;
        let parsed: Vec<GithubRelease> = serde_json::from_str(json).unwrap();
        assert_eq!(parsed[0].tag_name, "v5.1.2");
        assert!(!parsed[0].prerelease);
    }

    mod cache {
        use super::super::*;
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering as AO};

        struct Fake {
            hits: AtomicUsize,
            fail: AtomicBool,
        }

        /// A local stand-in for the GitHub releases endpoint.
        async fn fake_github() -> (String, Arc<Fake>) {
            let fake = Arc::new(Fake {
                hits: AtomicUsize::new(0),
                fail: AtomicBool::new(false),
            });
            let f = fake.clone();
            let app = axum::Router::new().route(
                "/releases",
                axum::routing::get(move || {
                    let f = f.clone();
                    async move {
                        f.hits.fetch_add(1, AO::SeqCst);
                        if f.fail.load(AO::SeqCst) {
                            (
                                axum::http::StatusCode::FORBIDDEN,
                                String::from("rate limited"),
                            )
                        } else {
                            (
                                axum::http::StatusCode::OK,
                                String::from(
                                    r#"[{"tag_name":"v5.1.2","body":"b","html_url":"u"}]"#,
                                ),
                            )
                        }
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
            (format!("http://{addr}/releases"), fake)
        }

        #[tokio::test]
        async fn a_fresh_list_is_served_from_the_cache() {
            let (url, fake) = fake_github().await;
            let cache = ReleaseCache::new(&url, Duration::from_secs(600), Duration::from_secs(60));
            let client = reqwest::Client::new();
            assert_eq!(cache.get(&client).await.unwrap()[0].tag_name, "v5.1.2");
            cache.get(&client).await.unwrap();
            assert_eq!(fake.hits.load(AO::SeqCst), 1);
        }

        #[tokio::test]
        async fn an_expired_list_is_fetched_again() {
            let (url, fake) = fake_github().await;
            let cache = ReleaseCache::new(&url, Duration::ZERO, Duration::from_secs(60));
            let client = reqwest::Client::new();
            cache.get(&client).await.unwrap();
            cache.get(&client).await.unwrap();
            assert_eq!(fake.hits.load(AO::SeqCst), 2);
        }

        #[tokio::test]
        async fn a_failed_fetch_serves_the_stale_list_and_waits_before_asking_again() {
            let (url, fake) = fake_github().await;
            let cache = ReleaseCache::new(&url, Duration::ZERO, Duration::from_secs(60));
            let client = reqwest::Client::new();
            cache.get(&client).await.unwrap();
            fake.fail.store(true, AO::SeqCst);
            assert_eq!(
                cache.get(&client).await.unwrap()[0].tag_name,
                "v5.1.2",
                "stale list, not an error"
            );
            assert_eq!(cache.get(&client).await.unwrap()[0].tag_name, "v5.1.2");
            assert_eq!(
                fake.hits.load(AO::SeqCst),
                2,
                "no new request inside the retry window"
            );
        }

        #[tokio::test]
        async fn a_failure_with_nothing_cached_is_an_error_and_is_not_retried_at_once() {
            let (url, fake) = fake_github().await;
            fake.fail.store(true, AO::SeqCst);
            let cache = ReleaseCache::new(&url, Duration::from_secs(600), Duration::from_secs(60));
            let client = reqwest::Client::new();
            let first = cache.get(&client).await.unwrap_err();
            assert!(first.contains("403"), "{first}");
            let second = cache.get(&client).await.unwrap_err();
            assert!(second.contains("next try in"), "{second}");
            assert_eq!(fake.hits.load(AO::SeqCst), 1);
        }

        #[tokio::test]
        async fn after_the_retry_window_github_is_asked_again() {
            let (url, fake) = fake_github().await;
            fake.fail.store(true, AO::SeqCst);
            let cache = ReleaseCache::new(&url, Duration::from_secs(600), Duration::ZERO);
            let client = reqwest::Client::new();
            cache.get(&client).await.unwrap_err();
            fake.fail.store(false, AO::SeqCst);
            assert_eq!(cache.get(&client).await.unwrap()[0].tag_name, "v5.1.2");
            assert_eq!(fake.hits.load(AO::SeqCst), 2);
        }
    }
}
