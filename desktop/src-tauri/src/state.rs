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
                // Store FIRST, then re-check the shutdown flag under the same
                // lock: a quit landing between spawn and store used to find an
                // empty slot, skip the kill, and orphan the engine on exactly
                // the path built to prevent that.
                let mut stopped = false;
                if let Ok(mut slot) = state.sidecar.lock() {
                    *slot = Some(child);
                    if state.is_shutting_down() {
                        if let Some(child) = slot.take() {
                            child.stop();
                            stopped = true;
                        }
                    }
                }
                if !stopped {
                    std::thread::spawn(move || monitor_sidecar(app));
                }
            }
            Err(err) => {
                eprintln!("harness: sidecar failed to start: {err}");
                backend_error_dialog(&app, &err.to_string());
            }
        }
    });
}

fn backend_error_dialog(app: &AppHandle, detail: &str) {
    let log = app.state::<AppState>().log_path.display().to_string();
    app.dialog()
        .message(format!(
            "The Python backend could not be started.\n\n{detail}\n\nLog: {log}"
        ))
        .title("Harness backend")
        .kind(MessageDialogKind::Error)
        .show(|_| {});
}

/* A sidecar that dies after a successful start is not silent anymore: it is
   respawned (bounded), and only when the budget is spent does the user hear
   about it. Without this, a crash at minute ten left the renderer retrying a
   dead port forever with nothing on screen. */
fn monitor_sidecar(app: AppHandle) {
    const MAX_RESPAWNS: u32 = 3;
    let mut respawns = 0u32;
    loop {
        std::thread::sleep(std::time::Duration::from_millis(500));
        let state = app.state::<AppState>();
        if state.is_shutting_down() {
            return;
        }
        let exited = match state.sidecar.lock() {
            Ok(mut slot) => match slot.as_mut() {
                Some(child) => match child.try_wait() {
                    Ok(Some(status)) => {
                        let dead = slot.take(); // drop the dead handle
                        drop(dead);
                        Some(status)
                    }
                    Ok(None) => None,
                    Err(_) => None,
                },
                None => continue, // still starting up
            },
            Err(_) => continue,
        };
        let Some(_status) = exited else {
            continue;
        };
        if state.is_shutting_down() {
            return;
        }
        respawns += 1;
        if respawns > MAX_RESPAWNS {
            backend_error_dialog(
                &app,
                "The backend stopped repeatedly and was not restarted. \
                 Restart the app; the log has the details.",
            );
            return;
        }
        eprintln!("harness: sidecar exited, respawning (attempt {respawns})");
        std::thread::sleep(std::time::Duration::from_millis(500 * respawns as u64));
        let state = app.state::<AppState>();
        let (data_dir, _) = paths::current_data_dir(&state.data_root);
        match sidecar::start(
            state.port,
            &state.repo,
            &state.project_root,
            &data_dir,
            &state.log_path,
        ) {
            Ok(child) => {
                if let Ok(mut slot) = state.sidecar.lock() {
                    *slot = Some(child);
                }
            }
            Err(err) => {
                backend_error_dialog(&app, &err.to_string());
                return;
            }
        }
    }
}
