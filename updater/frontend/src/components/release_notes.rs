use leptos::prelude::*;
use serde::Deserialize;

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct ReleaseNote {
    pub version: String,
    pub published: String,
    pub url: String,
    /// Rendered by the backend; raw HTML from GitHub is already escaped there.
    pub html: String,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct ReleaseNotes {
    pub releases: Vec<ReleaseNote>,
    #[serde(default)]
    pub error: Option<String>,
}

/// Release notes of every version an update would bring, or of the installed
/// version when Companion is up to date.
#[component]
pub fn ReleaseNotesCard() -> impl IntoView {
    let (notes, set_notes) = signal::<Option<ReleaseNotes>>(None);

    Effect::new(move |_| {
        wasm_bindgen_futures::spawn_local(async move {
            let result = gloo_net::http::Request::get("/api/release-notes")
                .send()
                .await;
            let parsed = match result {
                Ok(resp) => match resp.json::<ReleaseNotes>().await {
                    Ok(n) => n,
                    Err(e) => ReleaseNotes {
                        releases: vec![],
                        error: Some(format!("Cannot read release notes: {e}")),
                    },
                },
                Err(e) => ReleaseNotes {
                    releases: vec![],
                    error: Some(format!("Cannot load release notes: {e}")),
                },
            };
            set_notes.set(Some(parsed));
        });
    });

    view! {
        <div class="card release-notes" id="release-notes">
            <h2>"Release notes"</h2>
            {move || match notes.get() {
                None => view! { <p class="loading">"Loading release notes..."</p> }.into_any(),
                Some(n) => view! {
                    {n.error.clone().map(|e| view! { <div class="error-banner">{e}</div> })}
                    {(n.error.is_none() && n.releases.is_empty())
                        .then(|| view! { <p class="loading">"No release notes found."</p> })}
                    {n.releases.into_iter().map(|r| view! {
                        <section class="release">
                            <div class="release-head">
                                <span class="release-version">{r.version.clone()}</span>
                                <span class="release-date">{r.published.clone()}</span>
                                <a class="release-link" href={r.url.clone()} target="_blank" rel="noopener noreferrer">"GitHub"</a>
                            </div>
                            <div class="release-body" inner_html={r.html.clone()}></div>
                        </section>
                    }).collect_view()}
                }.into_any(),
            }}
        </div>
    }
}
