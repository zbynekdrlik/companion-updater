//! Serve the frontend bundle that trunk produces into `frontend/dist/`.
//! The whole directory is embedded at compile time via `include_dir!`.

use axum::{
    body::Body,
    extract::Path,
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use include_dir::{include_dir, Dir};

static DIST: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist");

pub async fn index() -> Response {
    serve("index.html").await
}

pub async fn asset(Path(path): Path<String>) -> Response {
    serve(&path).await
}

async fn serve(path: &str) -> Response {
    let file = match DIST.get_file(path) {
        Some(f) => f,
        None => {
            return (StatusCode::NOT_FOUND, "not found").into_response();
        }
    };
    let mime = mime_guess::from_path(path)
        .first_or_octet_stream()
        .essence_str()
        .to_string();
    let mut resp = Response::new(Body::from(file.contents()));
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(&mime).unwrap_or(HeaderValue::from_static("application/octet-stream")),
    );
    resp
}
