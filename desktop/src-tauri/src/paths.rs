use std::fs;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;

const RECENTS_LIMIT: usize = 24;

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct LastWorkspace {
    project_root: String,
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct DataDirOverride {
    data_dir: String,
}

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(windows)]
fn roaming_dir() -> PathBuf {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join("AppData").join("Roaming"))
}

#[cfg(not(windows))]
fn roaming_dir() -> PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".local").join("share"))
}

pub fn data_root() -> PathBuf {
    if let Some(dir) = std::env::var_os("HARNESS_DATA_ROOT") {
        return PathBuf::from(dir);
    }
    roaming_dir().join("Harness")
}

pub fn adopt_legacy_data_root(root: &Path) {
    if root.exists() {
        return;
    }
    let legacy = roaming_dir().join("harness-electron").join("harness");
    if !legacy.is_dir() {
        return;
    }
    if let Some(parent) = root.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::rename(&legacy, root);
}

pub fn last_workspace_file(root: &Path) -> PathBuf {
    root.join("last-workspace.json")
}

pub fn recents_file(root: &Path) -> PathBuf {
    root.join("recent-workspaces.json")
}

pub fn data_dir_file(root: &Path) -> PathBuf {
    root.join("data-dir.json")
}

pub fn logs_dir(root: &Path) -> PathBuf {
    root.join("logs")
}

pub fn read_json<T: DeserializeOwned>(path: &Path) -> Option<T> {
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn write_json<T: serde::Serialize>(path: &Path, value: &T) {
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Ok(raw) = serde_json::to_string(value) {
        let _ = fs::write(path, raw);
    }
}

pub fn is_dir(path: &Path) -> bool {
    fs::metadata(path).map(|m| m.is_dir()).unwrap_or(false)
}

pub fn load_last_workspace(root: &Path) -> Option<String> {
    let saved: LastWorkspace = read_json(&last_workspace_file(root))?;
    if is_dir(Path::new(&saved.project_root)) {
        Some(saved.project_root)
    } else {
        None
    }
}

pub fn save_last_workspace(root: &Path, project_root: &str) {
    write_json(
        &last_workspace_file(root),
        &LastWorkspace {
            project_root: project_root.to_string(),
        },
    );
}

pub fn clear_last_workspace(root: &Path, project_root: &str) {
    let path = last_workspace_file(root);
    let matches = read_json::<LastWorkspace>(&path)
        .map(|saved| saved.project_root == project_root)
        .unwrap_or(false);
    if matches {
        let _ = fs::remove_file(path);
    }
}

pub fn load_recents(root: &Path) -> Vec<String> {
    read_json::<Vec<String>>(&recents_file(root)).unwrap_or_default()
}

pub fn save_recents(root: &Path, list: &[String]) {
    write_json(&recents_file(root), &list);
}

pub fn push_recent(root: &Path, project_root: &str) {
    if project_root.is_empty() {
        return;
    }
    let mut list: Vec<String> = vec![project_root.to_string()];
    for entry in load_recents(root) {
        if list.len() >= RECENTS_LIMIT {
            break;
        }
        if list.iter().any(|seen| seen == &entry) {
            continue;
        }
        if !is_dir(Path::new(&entry)) {
            continue;
        }
        list.push(entry);
    }
    save_recents(root, &list);
}

pub fn forget_recent(root: &Path, project_root: &str) {
    let kept: Vec<String> = load_recents(root)
        .into_iter()
        .filter(|entry| entry != project_root)
        .collect();
    save_recents(root, &kept);
    clear_last_workspace(root, project_root);
}

pub fn custom_data_dir(root: &Path) -> Option<String> {
    let saved: DataDirOverride = read_json(&data_dir_file(root))?;
    if saved.data_dir.is_empty() {
        None
    } else {
        Some(saved.data_dir)
    }
}

pub fn save_custom_data_dir(root: &Path, dir: &str) {
    write_json(
        &data_dir_file(root),
        &DataDirOverride {
            data_dir: dir.to_string(),
        },
    );
}

pub fn reset_custom_data_dir(root: &Path) {
    let _ = fs::remove_file(data_dir_file(root));
}

pub fn current_data_dir(root: &Path) -> (PathBuf, &'static str) {
    if let Some(dir) = std::env::var_os("HARNESS_APP_DATA") {
        return (PathBuf::from(dir), "env");
    }
    if let Some(dir) = custom_data_dir(root) {
        return (PathBuf::from(dir), "custom");
    }
    (root.to_path_buf(), "default")
}

pub fn repo_root() -> PathBuf {
    // Where the APP CODE lives - never HARNESS_PROJECT_ROOT: that env var is
    // the agent's workspace (read by bootstrap for project_root), and using

    if let Ok(exe) = std::env::current_exe() {
        for dir in exe.ancestors().skip(1).take(8) {
            if dir.join("harness").join("server.py").is_file() {
                return dir.to_path_buf();
            }
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

pub fn preferred_port() -> u16 {
    std::env::var("HARNESS_PORT")
        .ok()
        .and_then(|raw| raw.parse::<u16>().ok())
        .unwrap_or(8765)
}

pub fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string_lossy().to_string())
}
