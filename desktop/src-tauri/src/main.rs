#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if let Err(err) = harness_desktop_lib::run() {
        eprintln!("harness: {err}");
        std::process::exit(1);
    }
}
