mod commands;
mod paths;
mod sidecar;
mod state;

use tauri::Manager;

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let app_state = state::bootstrap().map_err(|err| {
                std::io::Error::other(format!("failed to create auth token: {err}"))
            })?;
            app.manage(app_state);
            state::spawn_sidecar(handle);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::sidecar_config,
            commands::recent_workspaces,
            commands::forget_workspace,
            commands::select_workspace,
            commands::get_storage,
            commands::choose_data_dir,
            commands::reset_data_dir,
            commands::open_path,
            commands::restart_app,
        ])
        .build(tauri::generate_context!())
        .map_err(|err| std::io::Error::other(format!("failed to build the Harness window: {err}")))?
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<state::AppState>() {
                    state.stop_sidecar();
                }
            }
        });
    Ok(())
}
