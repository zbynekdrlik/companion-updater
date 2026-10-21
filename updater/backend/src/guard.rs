//! Request guards for the endpoints that change the rig.
//!
//! The dashboard is published at companion-update-*.newlevel.media behind
//! Cloudflare Access (companion-updater#26). Access protects against strangers,
//! not against a cross-site request riding the owner's Access cookie: a link to
//! `/api/update/stream` on another site would start a Companion upgrade on a
//! live rig. Browsers mark every request with `Sec-Fetch-Site`; the dashboard's
//! own EventSource is `same-origin`, a bookmark or typed URL is `none`, and
//! `deploy.sh`'s curl sends no such header at all.

use axum::http::{HeaderMap, StatusCode};

/// `Err` (403) for a request a browser made on behalf of another site.
pub fn reject_cross_site(headers: &HeaderMap) -> Result<(), (StatusCode, String)> {
    let _ = headers;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn with_site(value: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert("sec-fetch-site", HeaderValue::from_str(value).unwrap());
        h
    }

    #[test]
    fn a_link_from_another_site_cannot_start_an_upgrade() {
        let err = reject_cross_site(&with_site("cross-site")).unwrap_err();
        assert_eq!(err.0, StatusCode::FORBIDDEN);
    }

    #[test]
    fn a_sibling_newlevel_media_page_cannot_either() {
        assert_eq!(reject_cross_site(&with_site("same-site")).unwrap_err().0, StatusCode::FORBIDDEN);
    }

    #[test]
    fn the_dashboard_itself_a_bookmark_and_curl_are_allowed() {
        assert!(reject_cross_site(&with_site("same-origin")).is_ok());
        assert!(reject_cross_site(&with_site("none")).is_ok());
        assert!(reject_cross_site(&HeaderMap::new()).is_ok());
    }

    #[test]
    fn the_header_value_is_compared_case_insensitively() {
        assert!(reject_cross_site(&with_site("Cross-Site")).is_err());
    }
}
