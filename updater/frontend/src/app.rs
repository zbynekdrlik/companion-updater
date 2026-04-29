use leptos::prelude::*;
use serde::Deserialize;

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Status {
    pub current_version: String,
    pub latest_version: String,
    pub update_available: bool,
    pub service_active: bool,
    pub service_enabled: bool,
    pub can_update: bool,
    pub cooldown_remaining: u64,
    pub last_checked: String,
    #[serde(default)]
    pub error: Option<String>,
}

#[component]
pub fn App() -> impl IntoView {
    let (status, set_status) = signal::<Option<Status>>(None);
    let (progress_lines, set_progress_lines) = signal::<Vec<(String, String)>>(vec![]);
    let (updating, set_updating) = signal(false);

    // Initial fetch + 30s polling
    Effect::new(move |_| {
        let set_status = set_status;
        wasm_bindgen_futures::spawn_local(async move {
            fetch_status(set_status).await;
        });
    });

    let refresh = move |_| {
        wasm_bindgen_futures::spawn_local(async move {
            fetch_status(set_status).await;
        });
    };

    view! {
        <div class="container">
            <header>
                <h1>"Companion Update Dashboard"</h1>
                <p class="subtitle">"Bitfocus Companion (companion-pi)"</p>
            </header>

            <div class="card">
                <crate::components::status_card::StatusCard status=status />
            </div>

            <div class="card">
                <crate::components::update_button::UpdateButton
                    status=status
                    updating=updating
                    set_updating=set_updating
                    set_progress_lines=set_progress_lines
                    set_status=set_status
                />
                <crate::components::progress_log::ProgressLog
                    lines=progress_lines
                />
            </div>

            <div class="actions">
                <button class="refresh-btn" on:click=refresh>"Refresh"</button>
            </div>
        </div>
    }
}

async fn fetch_status(set_status: WriteSignal<Option<Status>>) {
    let result = gloo_net::http::Request::get("/api/status")
        .send()
        .await;
    match result {
        Ok(resp) => {
            if let Ok(s) = resp.json::<Status>().await {
                set_status.set(Some(s));
            }
        }
        Err(e) => {
            web_sys::console::error_1(&format!("status fetch failed: {e}").into());
        }
    }
}
