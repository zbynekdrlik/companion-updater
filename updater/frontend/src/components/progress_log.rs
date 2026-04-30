use leptos::prelude::*;

#[component]
pub fn ProgressLog(lines: ReadSignal<Vec<(String, String)>>) -> impl IntoView {
    let visible = move || !lines.get().is_empty();
    let each_fn = move || lines.get().into_iter().enumerate().collect::<Vec<_>>();
    let key_fn = |(i, _): &(usize, (String, String))| *i;
    let children_fn = move |(_, (kind, msg)): (usize, (String, String))| {
        let cls = match kind.as_str() {
            "error" => "line error",
            "complete" => "line success",
            "safety_pre" => "line safety-pre",
            "safety_post" => "line safety-post",
            "safety_rollback" => "line safety-rollback",
            _ => "line",
        };
        view! { <div class={cls}>{msg}</div> }
    };

    view! {
        <Show when=visible fallback=|| view! { <span></span> }>
            <div class="progress-container active">
                <div class="progress-log">
                    <For
                        each=each_fn
                        key=key_fn
                        children=children_fn
                    />
                </div>
            </div>
        </Show>
    }
}
