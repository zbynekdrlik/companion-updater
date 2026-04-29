#[allow(dead_code)]
mod version;

#[allow(dead_code)]
mod bitfocus;

#[allow(dead_code)]
mod companion;

#[allow(dead_code)]
mod update;

use axum::{routing::get, Router};
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let app = Router::new().route("/healthz", get(|| async { "ok" }));

    let addr: SocketAddr = "0.0.0.0:8081".parse().unwrap();
    tracing::info!("listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
