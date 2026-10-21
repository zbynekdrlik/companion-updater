//! Request guards for the endpoints that change the rig.
//!
//! The dashboard is published at companion-update-*.newlevel.media behind
//! Cloudflare Access (companion-updater#26). Access protects against strangers,
//! not against a request riding the owner's Access cookie: a link on another
//! site, or one opened from mail or chat, would navigate to
//! `/api/update/stream` and start a Companion upgrade on a live rig.
//!
//! Browsers send Fetch Metadata headers to secure origins (the HTTPS public
//! URLs). The dashboard's own EventSource is `Sec-Fetch-Site: same-origin`,
//! `Sec-Fetch-Mode: cors`; deploy.sh's curl sends no such headers. Everything a
//! browser does by navigating (links, bookmarks, typed URLs) is refused.
//!
//! Over plain `http://<rig>:8081` on the LAN browsers send none of these
//! headers, so this guard does nothing there. That LAN path has never had
//! authentication; it is reachable only inside the church networks.

use axum::http::{HeaderMap, StatusCode};

/// `Err` (403) for a browser request that did not come from the dashboard page:
/// cross-site / same-site, or any top-level navigation.
pub fn reject_cross_site(headers: &HeaderMap) -> Result<(), (StatusCode, String)> {
    let site = headers
        .get("sec-fetch-site")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim().to_ascii_lowercase());
    let mode = headers
        .get("sec-fetch-mode")
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim().to_ascii_lowercase());
    let cross_site = matches!(site.as_deref(), Some("cross-site" | "same-site"));
    let navigation = mode.as_deref() == Some("navigate");
    if cross_site || navigation {
        tracing::warn!(
            sec_fetch_site = site.as_deref().unwrap_or("-"),
            sec_fetch_mode = mode.as_deref().unwrap_or("-"),
            "refused an upgrade request that did not come from the dashboard"
        );
        return Err((
            StatusCode::FORBIDDEN,
            "Updates can only be started from the dashboard itself.".into(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn with(site: Option<&str>, mode: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        if let Some(v) = site {
            h.insert("sec-fetch-site", HeaderValue::from_str(v).unwrap());
        }
        if let Some(v) = mode {
            h.insert("sec-fetch-mode", HeaderValue::from_str(v).unwrap());
        }
        h
    }

    #[test]
    fn a_link_from_another_site_cannot_start_an_upgrade() {
        let err = reject_cross_site(&with(Some("cross-site"), Some("navigate"))).unwrap_err();
        assert_eq!(err.0, StatusCode::FORBIDDEN);
    }

    #[test]
    fn a_sibling_newlevel_media_page_cannot_either() {
        assert_eq!(
            reject_cross_site(&with(Some("same-site"), Some("cors"))).unwrap_err().0,
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn a_link_from_mail_or_chat_or_a_bookmark_cannot_either() {
        // An external app's link, a bookmark and a typed URL all arrive as a
        // top-level navigation with Sec-Fetch-Site: none.
        assert_eq!(
            reject_cross_site(&with(Some("none"), Some("navigate"))).unwrap_err().0,
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn the_dashboard_eventsource_and_curl_are_allowed() {
        assert!(reject_cross_site(&with(Some("same-origin"), Some("cors"))).is_ok());
        assert!(reject_cross_site(&HeaderMap::new()).is_ok());
    }

    #[test]
    fn the_header_values_are_compared_case_insensitively() {
        assert!(reject_cross_site(&with(Some("Cross-Site"), None)).is_err());
        assert!(reject_cross_site(&with(Some("none"), Some("Navigate"))).is_err());
    }
}
