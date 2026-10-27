import { app, BrowserWindow, Menu, dialog, ipcMain } from "electron";
import { spawn, spawnSync, ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

// Dev-mode: always load the latest renderer assets instead of Chromium's cache,
// so a restart reflects freshly-built styles.css / renderer.js.
app.commandLine.appendSwitch("disable-http-cache");

let win: BrowserWindow | null = null;
let backend: ChildProcess | null = null;
let activePort = 8765;
let activeRoot = "";

function log(tag: string, message: string): void {
  const stamp = new Date().toISOString().slice(11, 23);
  console.log(`[${stamp}][${tag}] ${message}`);
}

function repoRoot(): string {
  return path.resolve(__dirname, "..", "..", "..");
}

function venvPython(): string {
  return process.platform === "win32"
    ? path.join(repoRoot(), ".venv", "Scripts", "python.exe")
    : path.join(repoRoot(), ".venv", "bin", "python");
}

function preferredPort(): number {
  return Number(process.env.HARNESS_PORT ?? "8765");
}

function workspaceFile(): string {
  return path.join(app.getPath("userData"), "harness", "last-workspace.json");
}

function loadLastWorkspace(): string | null {
  try {
    const raw = fs.readFileSync(workspaceFile(), "utf-8");
    const root = (JSON.parse(raw) as { projectRoot?: string }).projectRoot;
    if (root && fs.existsSync(root) && fs.statSync(root).isDirectory())
      return root;
  } catch {
    /* no saved workspace yet */
  }
  return null;
}

function saveLastWorkspace(root: string): void {
  try {
    fs.mkdirSync(path.dirname(workspaceFile()), { recursive: true });
    fs.writeFileSync(workspaceFile(), JSON.stringify({ projectRoot: root }));
  } catch (err) {
    log("main", `could not save workspace: ${String(err)}`);
  }
}

function recentsFile(): string {
  return path.join(
    app.getPath("userData"),
    "harness",
    "recent-workspaces.json",
  );
}

function loadRecentsRoots(): string[] {
  try {
    const arr = JSON.parse(fs.readFileSync(recentsFile(), "utf-8"));
    if (Array.isArray(arr))
      return arr.filter((x): x is string => typeof x === "string");
  } catch {
    /* no recents file yet */
  }
  return [];
}

function saveRecentsRoots(list: string[]): void {
  try {
    fs.mkdirSync(path.dirname(recentsFile()), { recursive: true });
    fs.writeFileSync(recentsFile(), JSON.stringify(list.slice(0, 24)));
  } catch (err) {
    log("main", `could not save recents: ${String(err)}`);
  }
}

function pushRecent(root: string): void {
  if (!root) return;
  const seen = new Set<string>([root]);
  const list: string[] = [root];
  for (const r of loadRecentsRoots()) {
    if (list.length >= 24) break;
    if (seen.has(r)) continue;
    try {
      if (!fs.existsSync(r) || !fs.statSync(r).isDirectory()) continue;
    } catch {
      continue;
    }
    seen.add(r);
    list.push(r);
  }
  saveRecentsRoots(list);
}

function isDir(p: string): boolean {
  try {
    return fs.existsSync(p) && fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function sidecarPaths(): { projectRoot: string; appDataDir: string } {
  const projectRoot =
    process.env.HARNESS_PROJECT_ROOT ?? loadLastWorkspace() ?? repoRoot();
  const appDataDir =
    process.env.HARNESS_APP_DATA ??
    path.join(app.getPath("userData"), "harness");
  return { projectRoot, appDataDir };
}

function probePort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once("error", () => resolve(false));
    probe.once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port, "127.0.0.1");
  });
}

async function pickPort(): Promise<number> {
  const preferred = preferredPort();
  if (await probePort(preferred)) return preferred;
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function spawnProc(port: number, projectRoot: string): ChildProcess {
  const { appDataDir } = sidecarPaths();
  const root = repoRoot();
  const tail = [
    "-m",
    "harness.server",
    "--port",
    String(port),
    "--project-root",
    projectRoot,
    "--app-data-dir",
    appDataDir,
  ];
  // uv is the documented entry point, but a machine without it still has the
  // synced venv: fall back to its interpreter instead of failing to start.
  const useUv =
    spawnSync("uv", ["--version"], { stdio: "ignore" }).error == null;
  const cmd = useUv ? "uv" : venvPython();
  const args = useUv ? ["run", "python", ...tail] : tail;
  log("main", `spawning backend: ${cmd} ${args.join(" ")} (cwd=${root})`);
  try {
    return spawn(cmd, args, { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  } catch (err) {
    log("main", `spawn threw synchronously: ${String(err)}`);
    dialog.showErrorBox(
      "Harness backend failed",
      `Could not spawn ${cmd}: ${String(err)}`,
    );
    throw err;
  }
}

function wireProc(proc: ChildProcess): void {
  proc.stdout?.on("data", (chunk) =>
    process.stdout.write(`[backend] ${chunk}`),
  );
  proc.stderr?.on("data", (chunk) =>
    process.stderr.write(`[backend] ${chunk}`),
  );
  proc.on("error", (err) =>
    log("main", `backend spawn error: ${String(err)} (is uv on PATH?)`),
  );
  proc.on("exit", (code, signal) =>
    log(
      "main",
      `backend exited pid=${proc.pid} code=${code} signal=${signal ?? "none"}`,
    ),
  );
}

function startBackend(port: number, projectRoot: string): void {
  activeRoot = projectRoot;
  activePort = port;
  backend = spawnProc(port, projectRoot);
  wireProc(backend);
  backend.on("exit", () => {
    backend = null;
  });
}

function killBackend(): void {
  try {
    backend?.kill("SIGKILL");
  } catch {
    /* ignore */
  }
  backend = null;
}

function blockDevtoolsShortcuts(target: BrowserWindow): void {
  target.webContents.on("before-input-event", (event, input) => {
    const key = input.key.toLowerCase();
    if (input.key === "F12") event.preventDefault();
    else if (input.key === "F5") event.preventDefault();
    else if (
      (input.control || input.meta) &&
      input.shift &&
      ["i", "j", "c"].includes(key)
    )
      event.preventDefault();
    else if (
      (input.control || input.meta) &&
      !input.shift &&
      ["r"].includes(key)
    )
      event.preventDefault();
  });
  target.webContents.on("devtools-opened", () =>
    target.webContents.closeDevTools(),
  );
}

function createWindow(port: number, projectRoot: string): void {
  win = new BrowserWindow({
    width: 1180,
    height: 760,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      devTools: false,
    },
  });
  win.setMenu(null);
  blockDevtoolsShortcuts(win);
  const target = path.join(__dirname, "..", "renderer", "index.html");
  const stamp = Date.now();
  const bootFile = writeBootHtml(target, stamp);
  log(
    "main",
    `loading renderer: ${bootFile} with query port=${port} projectRoot=${projectRoot}`,
  );
  win.webContents.on("did-fail-load", (_e, code, desc) =>
    log("main", `renderer did-fail-load code=${code} desc=${desc}`),
  );
  win.webContents.on("did-finish-load", () => {
    log("main", `renderer finished load, sending sidecar-config port=${port}`);
    win?.webContents.send("sidecar-config", { port, projectRoot });
  });
  win.loadFile(bootFile, {
    query: { port: String(port), projectRoot, _v: String(stamp) },
  });
  win.on("closed", () => {
    win = null;
  });
}

/* Read index.html, version-bust the local css/js so Chromium always fetches the
   freshly built files (file:// sub-resources otherwise stick in the disk cache
   across relaunches), write a uniquely-named boot copy, and clean old ones. */
function writeBootHtml(indexPath: string, stamp: number): string {
  try {
    const dir = path.dirname(indexPath);
    let html = fs.readFileSync(indexPath, "utf-8");
    html = html.replace(
      /(["'])(\.\/styles\.css)(\?[^"']*)?(\1)/g,
      `$1$2?v=${stamp}$4`,
    );
    html = html.replace(
      /(["'])(\.\/renderer\.js)(\?[^"']*)?(\1)/g,
      `$1$2?v=${stamp}$4`,
    );
    const bootFile = path.join(dir, `.boot-${stamp}.html`);
    fs.writeFileSync(bootFile, html);
    try {
      for (const f of fs.readdirSync(dir)) {
        if (/^\.boot-\d+\.html$/.test(f) && f !== path.basename(bootFile)) {
          fs.unlinkSync(path.join(dir, f));
        }
      }
    } catch {
      /* cleanup best-effort */
    }
    return bootFile;
  } catch (err) {
    log("main", `writeBootHtml fallback to index.html: ${String(err)}`);
    return indexPath;
  }
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  log("main", `app ready, preferred port=${preferredPort()}`);
  try {
    activePort = await pickPort();
  } catch (err) {
    log("main", `pickPort failed: ${String(err)}`);
    dialog.showErrorBox(
      "Harness failed",
      `Could not find a free port: ${String(err)}`,
    );
    return;
  }
  const { projectRoot } = sidecarPaths();
  pushRecent(projectRoot);
  startBackend(activePort, projectRoot);
  createWindow(activePort, projectRoot);

  ipcMain.handle("select-workspace", async () => {
    if (!win) return null;
    const picked = await dialog.showOpenDialog(win, {
      properties: ["openDirectory"],
    });
    if (picked.canceled || !picked.filePaths.length) return null;
    const root = picked.filePaths[0];
    pushRecent(root);
    saveLastWorkspace(root);
    return { projectRoot: root };
  });

  ipcMain.handle("get-recent-workspaces", async () =>
    loadRecentsRoots()
      .filter((r) => isDir(r))
      .map((r) => ({ root: r, name: path.basename(r) || r })),
  );

  ipcMain.handle("forget-workspace", async (_event, root: string) => {
    if (typeof root !== "string" || !root) return false;
    // Forgetting is not deleting: the folder stays on disk, only the app's
    // memory of it goes. If it was the last folder opened, the remembered
    // workspace goes with it so the next boot does not resurrect it.
    const kept = loadRecentsRoots().filter((r) => r !== root);
    saveRecentsRoots(kept);
    const last = workspaceFile();
    try {
      if (
        fs.existsSync(last) &&
        JSON.parse(fs.readFileSync(last, "utf-8")).projectRoot === root
      ) {
        fs.unlinkSync(last);
      }
    } catch (err) {
      log("main", `could not clear last workspace: ${String(err)}`);
    }
    return true;
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0)
      createWindow(activePort, activeRoot);
  });
});

app.on("window-all-closed", () => {
  killBackend();
  if (process.platform !== "darwin") app.quit();
});

app.on("quit", () => killBackend());
