use crate::app::Status;
use leptos::prelude::*;
use serde::Deserialize;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{EventSource, MessageEvent};

#[derive(Deserialize)]
struct UpdateEvent {
    #[serde(rename = "type")]
    kind: String,
    message: String,
}

#[component]
pub fn UpdateButton(
    status: ReadSignal<Option<Status>>,
    updating: ReadSignal<bool>,
    set_updating: WriteSignal<bool>,
    set_progress_lines: WriteSignal<Vec<(String, String)>>,
    set_status: WriteSignal<Option<Status>>,
) -> impl IntoView {
    let label = move || {
        if updating.get() {
            return "Updating...".to_string();
        }
        let s = match status.get() {
            Some(s) => s,
            None => return "Checking...".to_string(),
        };
        if !s.update_available {
            "Up to Date".to_string()
        } else if !s.can_update && s.cooldown_remaining > 0 {
            format!("Cooldown: {}s", s.cooldown_remaining)
        } else {
            "Update Now".to_string()
        }
    };

    let class = move || {
        if updating.get() {
            "update-btn in-progress"
        } else {
            match status.get() {
                Some(s) if s.update_available && s.can_update => "update-btn available",
                _ => "update-btn disabled",
            }
        }
    };

    let disabled = move || {
        if updating.get() {
            return true;
        }
        match status.get() {
            Some(s) => !(s.update_available && s.can_update),
            None => true,
        }
    };

    let on_click = move |_| {
        if updating.get_untracked() {
            return;
        }
        set_updating.set(true);
        set_progress_lines.set(vec![]);

        let es = match EventSource::new("/api/update/stream") {
            Ok(es) => es,
            Err(e) => {
                web_sys::console::error_1(&format!("EventSource error: {e:?}").into());
                set_updating.set(false);
                return;
            }
        };

        let es_for_msg = es.clone();
        let on_message = Closure::<dyn FnMut(MessageEvent)>::new(
            move |event: MessageEvent| {
                let data = event.data().as_string().unwrap_or_default();
                let parsed: UpdateEvent = match serde_json::from_str(&data) {
                    Ok(p) => p,
                    Err(_) => return,
                };
                set_progress_lines.update(|v| {
                    v.push((parsed.kind.clone(), parsed.message.clone()));
                });
                if parsed.kind == "complete" || parsed.kind == "error" {
                    es_for_msg.close();
                    set_updating.set(false);
                    // Refresh status after a short delay
                    let set_status = set_status;
                    wasm_bindgen_futures::spawn_local(async move {
                        gloo_timers::future::TimeoutFuture::new(1500).await;
                        if let Ok(resp) = gloo_net::http::Request::get("/api/status").send().await {
                            if let Ok(s) = resp.json::<Status>().await {
                                set_status.set(Some(s));
                            }
                        }
                    });
                }
            },
        );
        es.set_onmessage(Some(on_message.as_ref().unchecked_ref()));
        on_message.forget();

        let es_for_err = es.clone();
        let on_error = Closure::<dyn FnMut(_)>::new(move |_event: web_sys::Event| {
            es_for_err.close();
            set_updating.set(false);
            set_progress_lines.update(|v| {
                v.push((
                    "error".into(),
                    "Connection lost. Please refresh.".into(),
                ));
            });
        });
        es.set_onerror(Some(on_error.as_ref().unchecked_ref()));
        on_error.forget();
    };

    view! {
        <button class={class} disabled={disabled} on:click=on_click>
            {label}
        </button>
    }
}
