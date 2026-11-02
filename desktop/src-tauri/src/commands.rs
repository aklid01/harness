/* Commands the renderer calls through `window.harness` (see web/renderer/bridge.ts). */
use std::path::Path;

use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::DialogExt;

use crate::paths;
use crate::state::AppState;

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SidecarConfig {
    pub port: u16,
    pub project_root: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PickedWorkspace {
    pub project_root: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkspaceRef {
    pub root: String,
    pub name: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StorageInfo {
    pub data_dir: String,
    pub source: String,
    pub sessions_dir: String,
    pub providers_file: String,
    pub agents_file: String,
}

#[tauri::command]
pub fn sidecar_config(state: State<'_, AppState>) -> SidecarConfig {
    SidecarConfig {
        port: state.port,
        project_root: state.project_root.to_string_lossy().to_string(),
    }
}

#[tauri::command]
pub fn recent_workspaces(state: State<'_, AppState>) -> Vec<WorkspaceRef> {
    paths::load_recents(&state.data_root)
        .into_iter()
        .filter(|root| paths::is_dir(Path::new(root)))
        .map(|root| WorkspaceRef {
            name: paths::file_name(Path::new(&root)),
            root,
        })
        .collect()
}

#[tauri::command]
pub fn forget_workspace(state: State<'_, AppState>, root: String) -> bool {
    if root.is_empty() {
        return false;
    }
    paths::forget_recent(&state.data_root, &root);
    true
}

#[tauri::command]
pub async fn select_workspace(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Option<PickedWorkspace>, String> {
    let picked = app.dialog().file().blocking_pick_folder();
    let Some(picked) = picked else {
        return Ok(None);
    };
    let path = picked.into_path().map_err(|err| err.to_string())?;
    let root = path.to_string_lossy().to_string();
    paths::push_recent(&state.data_root, &root);
    paths::save_last_workspace(&state.data_root, &root);
    Ok(Some(PickedWorkspace { project_root: root }))
}

#[tauri::command]
pub fn get_storage(state: State<'_, AppState>) -> StorageInfo {
    let (data_dir, source) = paths::current_data_dir(&state.data_root);
    StorageInfo {
        sessions_dir: data_dir.join("sessions").to_string_lossy().to_string(),
        providers_file: data_dir
            .join("providers.json")
            .to_string_lossy()
            .to_string(),
        agents_file: data_dir.join("agents.json").to_string_lossy().to_string(),
        data_dir: data_dir.to_string_lossy().to_string(),
        source: source.to_string(),
    }
}

#[tauri::command]
pub async fn choose_data_dir(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Option<String>, String> {
    let picked = app.dialog().file().blocking_pick_folder();
    let Some(picked) = picked else {
        return Ok(None);
    };
    let path = picked.into_path().map_err(|err| err.to_string())?;
    let dir = path.to_string_lossy().to_string();
    paths::save_custom_data_dir(&state.data_root, &dir);
    Ok(Some(dir))
}

#[tauri::command]
pub fn reset_data_dir(state: State<'_, AppState>) -> bool {
    paths::reset_custom_data_dir(&state.data_root);
    true
}

#[tauri::command]
pub fn open_path(target: String) -> bool {
    if target.is_empty() {
        return false;
    }
    let path = Path::new(&target);
    if !path.exists() {
        return false;
    }
    #[cfg(windows)]
    let opened = std::process::Command::new("explorer").arg(path).spawn();
    #[cfg(target_os = "macos")]
    let opened = std::process::Command::new("open").arg(path).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let opened = std::process::Command::new("xdg-open").arg(path).spawn();
    opened.is_ok()
}

#[tauri::command]
pub fn restart_app(app: AppHandle) {
    if let Some(state) = app.try_state::<AppState>() {
        state.stop_sidecar();
    }
    app.restart();
}
