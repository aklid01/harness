

use std::ffi::OsStr;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub struct Sidecar {
    child: Child,
}

impl Sidecar {
    pub fn try_wait(&mut self) -> std::io::Result<Option<String>> {
        self.child
            .try_wait()
            .map(|status| status.map(|s| s.to_string()))
    }

    pub fn stop(mut self) {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;

            let _ = Command::new("taskkill")
                .args(["/PID", &self.child.id().to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(0x0800_0000)
                .status();
        }
        #[cfg(not(windows))]
        {
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
    }
}

pub fn pick_port(preferred: u16) -> u16 {
    if TcpListener::bind(("127.0.0.1", preferred)).is_ok() {
        return preferred;
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr())
        .map(|addr| addr.port())
        .unwrap_or(preferred)
}

pub fn start(
    port: u16,
    repo: &Path,
    project_root: &Path,
    data_dir: &Path,
    log_path: &Path,
    auth_token: &str,
) -> std::io::Result<Sidecar> {
    let log = open_log(log_path);
    let args = backend_args(port, project_root, data_dir, auth_token);
    let venv = crate::paths::venv_python(repo);
    log_line(
        &log,
        &format!(
            "starting sidecar on port {port} (project={}, data={})",
            project_root.display(),
            data_dir.display()
        ),
    );

    if uv_available() {
        let prefix = ["run".to_string(), "python".to_string()];
        let mut child = spawn("uv", &prefix, &args, repo, &log)?;
        if watch_early_exit(&mut child, Duration::from_secs(5)).is_none() {
            return Ok(Sidecar { child });
        }
        log_line(
            &log,
            "uv stopped during startup, falling back to the virtualenv interpreter",
        );
        let _ = child.wait();
        if !venv.is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("neither uv nor {} could start the backend", venv.display()),
            ));
        }
    }

    let child = spawn(&venv, &[], &args, repo, &log)?;
    Ok(Sidecar { child })
}

fn backend_args(port: u16, project_root: &Path, data_dir: &Path, auth_token: &str) -> Vec<String> {
    vec![
        "-m".to_string(),
        "harness.server".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--project-root".to_string(),
        project_root.to_string_lossy().to_string(),
        "--app-data-dir".to_string(),
        data_dir.to_string_lossy().to_string(),
        "--auth-token".to_string(),
        auth_token.to_string(),
    ]
}




pub fn new_token() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("OS entropy for the sidecar auth token");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn uv_available() -> bool {
    Command::new("uv")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn spawn<P: AsRef<OsStr>>(
    program: P,
    prefix: &[String],
    args: &[String],
    cwd: &Path,
    log: &Arc<Mutex<File>>,
) -> std::io::Result<Child> {
    let mut command = Command::new(program);
    command
        .args(prefix)
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }

    let mut child = command.spawn()?;
    if let Some(stdout) = child.stdout.take() {
        pump(stdout, Arc::clone(log), "out");
    }
    if let Some(stderr) = child.stderr.take() {
        pump(stderr, Arc::clone(log), "err");
    }
    Ok(child)
}

fn pump<R: Read + Send + 'static>(stream: R, log: Arc<Mutex<File>>, tag: &'static str) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            log_line(&log, &format!("[{tag}] {line}"));
        }
    });
}

fn watch_early_exit(child: &mut Child, window: Duration) -> Option<String> {
    let deadline = Instant::now() + window;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status.to_string()),
            Ok(None) => {}
            Err(err) => return Some(err.to_string()),
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(Duration::from_millis(120));
    }
}

fn open_log(path: &Path) -> Arc<Mutex<File>> {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .or_else(|_| {
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(log_fallback())
        })
        .unwrap_or_else(|_| {
            File::create(std::env::temp_dir().join("harness-sidecar.log")).expect("log file")
        });
    Arc::new(Mutex::new(file))
}

fn log_fallback() -> PathBuf {
    std::env::temp_dir().join("harness-sidecar.log")
}

fn log_line(log: &Arc<Mutex<File>>, line: &str) {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut file) = log.lock() {
        let _ = writeln!(file, "[{stamp}] {line}");
    }
}
