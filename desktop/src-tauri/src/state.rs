/* Shell state: where the app lives on disk, which port the sidecar uses, and
the child process handle. */
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

use crate::paths;
use crate::sidecar::{self, Sidecar};

pub struct AppState {
    pub port: u16,
    pub repo: PathBuf,
    pub project_root: PathBuf,
    pub data_root: PathBuf,
    pub log_path: PathBuf,
    pub sidecar: Mutex<Option<Sidecar>>,
    shutting_down: AtomicBool,
}

impl AppState {
    pub fn stop_sidecar(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        let child = self.sidecar.lock().ok().and_then(|mut slot| slot.take());
        if let Some(child) = child {
            child.stop();
        }
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }
}

pub fn bootstrap() -> AppState {
    let data_root = paths::data_root();
    paths::adopt_legacy_data_root(&data_root);

    let repo = paths::repo_root();
    let project_root = std::env::var_os("HARNESS_PROJECT_ROOT")
        .map(PathBuf::from)
        .or_else(|| paths::load_last_workspace(&data_root).map(PathBuf::from))
        .unwrap_or_else(|| repo.clone());

    let port = sidecar::pick_port(paths::preferred_port());
    paths::push_recent(&data_root, &project_root.to_string_lossy());

    let logs = paths::logs_dir(&data_root);
    let _ = std::fs::create_dir_all(&logs);

    AppState {
        port,
        repo,
        project_root,
        data_root,
        log_path: logs.join("sidecar.log"),
        sidecar: Mutex::new(None),
        shutting_down: AtomicBool::new(false),
    }
}

/* Runs off the main thread: `uv` may sync dependencies on a cold start and the
window should not wait for that. The renderer retries the socket until the
backend answers. */
pub fn spawn_sidecar(app: AppHandle) {
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let (data_dir, _) = paths::current_data_dir(&state.data_root);
        let started = sidecar::start(
            state.port,
            &state.repo,
            &state.project_root,
            &data_dir,
            &state.log_path,
        );

        match started {
            Ok(child) => {
                if state.is_shutting_down() {
                    child.stop();
                    return;
                }
                if let Ok(mut slot) = state.sidecar.lock() {
                    *slot = Some(child);
                }
            }
            Err(err) => {
                eprintln!("harness: sidecar failed to start: {err}");
                app.dialog()
                    .message(format!(
                        "The Python backend could not be started.\n\n{err}\n\nLog: {}",
                        state.log_path.display()
                    ))
                    .title("Harness backend")
                    .kind(MessageDialogKind::Error)
                    .show(|_| {});
            }
        }
    });
}
