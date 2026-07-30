mod bitfocus;
mod companion;
mod safety;
mod static_files;
mod update;
mod version;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::{
    extract::State,
    http::StatusCode,
    response::sse::{Event, KeepAlive, Sse},
    response::Json,
    routing::get,
    Router,
};
use chrono::Local;
use futures::stream::Stream;
use serde::Serialize;
use tokio::sync::{mpsc, Mutex};
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::StreamExt;

const COOLDOWN_SECS: u64 = 300;

#[derive(Clone)]
struct AppState {
    http: reqwest::Client,
    last_update: Arc<Mutex<Option<Instant>>>,
    update_running: Arc<Mutex<bool>>,
}

#[derive(Serialize)]
struct StatusResponse {
    current_version: String,
    latest_version: String,
    update_available: bool,
    service_active: bool,
    service_enabled: bool,
    can_update: bool,
    cooldown_remaining: u64,
    last_checked: String,
    error: Option<String>,
    /// Version of this updater itself, so a deploy can be verified from the UI.
    updater_version: String,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    // If a previous run was killed while Companion was stopped for an upgrade,
    // bring Companion back before serving anything.
    update::reconcile_companion_state();

    let state = AppState {
        http: reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .build()
            .expect("reqwest client"),
        last_update: Arc::new(Mutex::new(None)),
        update_running: Arc::new(Mutex::new(false)),
    };

    let app = Router::new()
        .route("/", get(static_files::index))
        .route("/api/status", get(status_handler))
        .route("/api/update/stream", get(update_stream_handler))
        .route("/healthz", get(|| async { "ok" }))
        .fallback(get(static_files::asset))
        .with_state(state);

    let addr: SocketAddr = "0.0.0.0:8081".parse().unwrap();
    tracing::info!("companion-updater listening on {}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn status_handler(State(state): State<AppState>) -> Json<StatusResponse> {
    let current_raw = companion::read_installed_version().await;
    let latest_raw = bitfocus::fetch_latest_stable_linux(&state.http).await;
    let service_active = companion::service_is_active().await;
    let service_enabled = companion::service_is_enabled().await;

    let cooldown_remaining = {
        let last = state.last_update.lock().await;
        match *last {
            Some(t) => COOLDOWN_SECS.saturating_sub(t.elapsed().as_secs()),
            None => 0,
        }
    };

    let update_running = *state.update_running.lock().await;
    let can_update = cooldown_remaining == 0 && !update_running;

    let (current_version, latest_version, update_available, error) =
        match (current_raw, latest_raw) {
            (Ok(c), Ok(l)) => {
                let avail = version::is_update_available(&c, &l);
                (version::format(&c), version::format(&l), avail, None)
            }
            (Err(e), _) => (
                "unknown".into(),
                "unknown".into(),
                false,
                Some(format!("Cannot read installed version: {e}")),
            ),
            (Ok(c), Err(e)) => (
                version::format(&c),
                "unknown".into(),
                false,
                Some(format!("Cannot fetch latest version: {e}")),
            ),
        };

    Json(StatusResponse {
        current_version,
        latest_version,
        update_available,
        service_active,
        service_enabled,
        can_update,
        cooldown_remaining,
        last_checked: Local::now().format("%H:%M:%S").to_string(),
        error,
        updater_version: format!("v{}", env!("CARGO_PKG_VERSION")),
    })
}

async fn update_stream_handler(
    State(state): State<AppState>,
) -> Result<
    Sse<impl Stream<Item = Result<Event, std::convert::Infallible>>>,
    (StatusCode, String),
> {
    {
        let mut running = state.update_running.lock().await;
        if *running {
            return Err((
                StatusCode::CONFLICT,
                "Update already in progress".into(),
            ));
        }
        let last = state.last_update.lock().await;
        if let Some(t) = *last {
            let elapsed = t.elapsed().as_secs();
            if elapsed < COOLDOWN_SECS {
                return Err((
                    StatusCode::TOO_MANY_REQUESTS,
                    format!("Cooldown active. Wait {} seconds.", COOLDOWN_SECS - elapsed),
                ));
            }
        }
        *running = true;
    }

    let (tx, rx) = mpsc::channel::<update::UpdateEvent>(64);
    let state_clone = state.clone();

    tokio::spawn(async move {
        let succeeded = update::run_update(tx).await;
        let mut running = state_clone.update_running.lock().await;
        *running = false;
        // Only a successful run starts the cooldown. A failed one must be
        // retryable straight away — waiting 5 minutes to retry an upgrade that
        // did nothing helps nobody.
        if succeeded {
            let mut last = state_clone.last_update.lock().await;
            *last = Some(Instant::now());
        }
    });

    let stream = ReceiverStream::new(rx).map(|event| {
        let json = serde_json::to_string(&event).unwrap_or_else(|_| "{}".into());
        Ok(Event::default().data(json))
    });

    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}
