use crate::app::Status;
use leptos::prelude::*;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{EventSource, MessageEvent};

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
                let val: serde_json::Value = match serde_json::from_str(&data) {
                    Ok(v) => v,
                    Err(_) => return,
                };
                let kind = val
                    .get("type")
                    .and_then(|t| t.as_str())
                    .unwrap_or("")
                    .to_string();
                let message = if let Some(m) = val.get("message").and_then(|m| m.as_str()) {
                    m.to_string()
                } else if kind == "safety_pre" || kind == "safety_post" {
                    // Format counts inline.
                    let c = val.get("counts").cloned().unwrap_or_default();
                    let connections =
                        c.get("connections").and_then(|x| x.as_u64()).unwrap_or(0);
                    let pages =
                        c.get("pages_with_content").and_then(|x| x.as_u64()).unwrap_or(0);
                    let buttons = c.get("buttons").and_then(|x| x.as_u64()).unwrap_or(0);
                    let triggers = c.get("triggers").and_then(|x| x.as_u64()).unwrap_or(0);
                    let label = if kind == "safety_pre" {
                        "Pre-upgrade snapshot"
                    } else {
                        "Post-upgrade snapshot"
                    };
                    format!(
                        "{label}: {connections} connections, {pages} pages, {buttons} buttons, {triggers} triggers"
                    )
                } else {
                    "(no message)".to_string()
                };

                set_progress_lines.update(|v| {
                    v.push((kind.clone(), message));
                });

                let terminal = matches!(kind.as_str(), "complete" | "error" | "safety_rollback");
                if terminal {
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
