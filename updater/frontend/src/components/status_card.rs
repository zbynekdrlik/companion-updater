use crate::app::Status;
use leptos::prelude::*;

#[component]
pub fn StatusCard(status: ReadSignal<Option<Status>>) -> impl IntoView {
    view! {
        <Show
            when=move || status.get().is_some()
            fallback=|| view! { <p class="loading">"Loading status..."</p> }
        >
            {move || {
                let s = status.get().unwrap_or_default();
                let latest_class = if s.update_available { "version-number latest" } else { "version-number up-to-date" };
                let svc_class = if s.service_active { "status-badge badge-running" } else { "status-badge badge-stopped" };
                let svc_text = if s.service_active { "Running" } else { "Stopped" };
                let upd_class = if s.update_available { "status-badge badge-update" } else { "status-badge badge-current" };
                let upd_text = if s.update_available { "Update Available" } else { "Up to Date" };
                view! {
                    <div class="version-grid">
                        <div class="version-box">
                            <div class="version-label">"Current"</div>
                            <div class="version-number current">{s.current_version.clone()}</div>
                        </div>
                        <div class="version-box">
                            <div class="version-label">"Latest"</div>
                            <div class={latest_class}>{s.latest_version.clone()}</div>
                        </div>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Service"</span>
                        <span class={svc_class}>{svc_text}</span>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Status"</span>
                        <span class={upd_class}>{upd_text}</span>
                    </div>
                    <div class="status-row">
                        <span class="status-label">"Last checked"</span>
                        <span class="status-value">{s.last_checked.clone()}</span>
                    </div>
                    {s.error.clone().map(|e| view! {
                        <div class="error-banner">{e}</div>
                    })}
                }
            }}
        </Show>
    }
}
