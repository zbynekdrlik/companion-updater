use leptos::prelude::*;

fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(|| view! { <h1>"Companion Update Dashboard"</h1> });
}
