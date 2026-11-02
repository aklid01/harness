"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const node_child_process_1 = require("node:child_process");
const node_fs_1 = __importDefault(require("node:fs"));
const node_net_1 = __importDefault(require("node:net"));
const node_path_1 = __importDefault(require("node:path"));
// Dev-mode: always load the latest renderer assets instead of Chromium's cache,
// so a restart reflects freshly-built styles.css / renderer.js.
electron_1.app.commandLine.appendSwitch("disable-http-cache");
let win = null;
let backend = null;
let activePort = 8765;
let activeRoot = "";
function log(tag, message) {
    const stamp = new Date().toISOString().slice(11, 23);
    console.log(`[${stamp}][${tag}] ${message}`);
}
function repoRoot() {
    return node_path_1.default.resolve(__dirname, "..", "..", "..");
}
function venvPython() {
    return process.platform === "win32"
        ? node_path_1.default.join(repoRoot(), ".venv", "Scripts", "python.exe")
        : node_path_1.default.join(repoRoot(), ".venv", "bin", "python");
}
function preferredPort() {
    return Number(process.env.HARNESS_PORT ?? "8765");
}
function workspaceFile() {
    return node_path_1.default.join(electron_1.app.getPath("userData"), "harness", "last-workspace.json");
}
function loadLastWorkspace() {
    try {
        const raw = node_fs_1.default.readFileSync(workspaceFile(), "utf-8");
        const root = JSON.parse(raw).projectRoot;
        if (root && node_fs_1.default.existsSync(root) && node_fs_1.default.statSync(root).isDirectory())
            return root;
    }
    catch {
        /* no saved workspace yet */
    }
    return null;
}
function saveLastWorkspace(root) {
    try {
        node_fs_1.default.mkdirSync(node_path_1.default.dirname(workspaceFile()), { recursive: true });
        node_fs_1.default.writeFileSync(workspaceFile(), JSON.stringify({ projectRoot: root }));
    }
    catch (err) {
        log("main", `could not save workspace: ${String(err)}`);
    }
}
function recentsFile() {
    return node_path_1.default.join(electron_1.app.getPath("userData"), "harness", "recent-workspaces.json");
}
function loadRecentsRoots() {
    try {
        const arr = JSON.parse(node_fs_1.default.readFileSync(recentsFile(), "utf-8"));
        if (Array.isArray(arr))
            return arr.filter((x) => typeof x === "string");
    }
    catch {
        /* no recents file yet */
    }
    return [];
}
function saveRecentsRoots(list) {
    try {
        node_fs_1.default.mkdirSync(node_path_1.default.dirname(recentsFile()), { recursive: true });
        node_fs_1.default.writeFileSync(recentsFile(), JSON.stringify(list.slice(0, 24)));
    }
    catch (err) {
        log("main", `could not save recents: ${String(err)}`);
    }
}
function pushRecent(root) {
    if (!root)
        return;
    const seen = new Set([root]);
    const list = [root];
    for (const r of loadRecentsRoots()) {
        if (list.length >= 24)
            break;
        if (seen.has(r))
            continue;
        try {
            if (!node_fs_1.default.existsSync(r) || !node_fs_1.default.statSync(r).isDirectory())
                continue;
        }
        catch {
            continue;
        }
        seen.add(r);
        list.push(r);
    }
    saveRecentsRoots(list);
}
function isDir(p) {
    try {
        return node_fs_1.default.existsSync(p) && node_fs_1.default.statSync(p).isDirectory();
    }
    catch {
        return false;
    }
}
function defaultDataDir() {
    return node_path_1.default.join(electron_1.app.getPath("userData"), "harness");
}
function dataDirFile() {
    return node_path_1.default.join(electron_1.app.getPath("userData"), "harness", "data-dir.json");
}
function loadCustomDataDir() {
    try {
        const raw = JSON.parse(node_fs_1.default.readFileSync(dataDirFile(), "utf-8"));
        const dir = raw.dataDir;
        if (typeof dir === "string" && dir)
            return dir;
    }
    catch {
        /* no override yet */
    }
    return null;
}
function currentDataDir() {
    if (process.env.HARNESS_APP_DATA)
        return process.env.HARNESS_APP_DATA;
    return loadCustomDataDir() ?? defaultDataDir();
}
function sidecarPaths() {
    const projectRoot = process.env.HARNESS_PROJECT_ROOT ?? loadLastWorkspace() ?? repoRoot();
    return { projectRoot, appDataDir: currentDataDir() };
}
function probePort(port) {
    return new Promise((resolve) => {
        const probe = node_net_1.default.createServer();
        probe.once("error", () => resolve(false));
        probe.once("listening", () => probe.close(() => resolve(true)));
        probe.listen(port, "127.0.0.1");
    });
}
async function pickPort() {
    const preferred = preferredPort();
    if (await probePort(preferred))
        return preferred;
    return new Promise((resolve, reject) => {
        const server = node_net_1.default.createServer();
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
            const address = server.address();
            const port = typeof address === "object" && address ? address.port : 0;
            server.close(() => resolve(port));
        });
    });
}
function spawnProc(port, projectRoot) {
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
    const useUv = (0, node_child_process_1.spawnSync)("uv", ["--version"], { stdio: "ignore" }).error == null;
    const cmd = useUv ? "uv" : venvPython();
    const args = useUv ? ["run", "python", ...tail] : tail;
    log("main", `spawning backend: ${cmd} ${args.join(" ")} (cwd=${root})`);
    try {
        return (0, node_child_process_1.spawn)(cmd, args, { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
    }
    catch (err) {
        log("main", `spawn threw synchronously: ${String(err)}`);
        electron_1.dialog.showErrorBox("Harness backend failed", `Could not spawn ${cmd}: ${String(err)}`);
        throw err;
    }
}
function wireProc(proc) {
    proc.stdout?.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`));
    proc.stderr?.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`));
    proc.on("error", (err) => log("main", `backend spawn error: ${String(err)} (is uv on PATH?)`));
    proc.on("exit", (code, signal) => log("main", `backend exited pid=${proc.pid} code=${code} signal=${signal ?? "none"}`));
}
function startBackend(port, projectRoot) {
    activeRoot = projectRoot;
    activePort = port;
    backend = spawnProc(port, projectRoot);
    wireProc(backend);
    backend.on("exit", () => {
        backend = null;
    });
}
function killBackend() {
    try {
        backend?.kill("SIGKILL");
    }
    catch {
        /* ignore */
    }
    backend = null;
}
function blockDevtoolsShortcuts(target) {
    target.webContents.on("before-input-event", (event, input) => {
        const key = input.key.toLowerCase();
        if (input.key === "F12")
            event.preventDefault();
        else if (input.key === "F5")
            event.preventDefault();
        else if ((input.control || input.meta) &&
            input.shift &&
            ["i", "j", "c"].includes(key))
            event.preventDefault();
        else if ((input.control || input.meta) &&
            !input.shift &&
            ["r"].includes(key))
            event.preventDefault();
    });
    target.webContents.on("devtools-opened", () => target.webContents.closeDevTools());
}
function createWindow(port, projectRoot) {
    win = new electron_1.BrowserWindow({
        width: 1180,
        height: 760,
        autoHideMenuBar: true,
        webPreferences: {
            preload: node_path_1.default.join(__dirname, "..", "preload", "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
            devTools: false,
        },
    });
    win.setMenu(null);
    blockDevtoolsShortcuts(win);
    const target = node_path_1.default.join(__dirname, "..", "renderer", "index.html");
    const stamp = Date.now();
    const bootFile = writeBootHtml(target, stamp);
    log("main", `loading renderer: ${bootFile} with query port=${port} projectRoot=${projectRoot}`);
    win.webContents.on("did-fail-load", (_e, code, desc) => log("main", `renderer did-fail-load code=${code} desc=${desc}`));
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
function writeBootHtml(indexPath, stamp) {
    try {
        const dir = node_path_1.default.dirname(indexPath);
        let html = node_fs_1.default.readFileSync(indexPath, "utf-8");
        html = html.replace(/(["'])(\.\/styles\.css)(\?[^"']*)?(\1)/g, `$1$2?v=${stamp}$4`);
        html = html.replace(/(["'])(\.\/renderer\.js)(\?[^"']*)?(\1)/g, `$1$2?v=${stamp}$4`);
        const bootFile = node_path_1.default.join(dir, `.boot-${stamp}.html`);
        node_fs_1.default.writeFileSync(bootFile, html);
        try {
            for (const f of node_fs_1.default.readdirSync(dir)) {
                if (/^\.boot-\d+\.html$/.test(f) && f !== node_path_1.default.basename(bootFile)) {
                    node_fs_1.default.unlinkSync(node_path_1.default.join(dir, f));
                }
            }
        }
        catch {
            /* cleanup best-effort */
        }
        return bootFile;
    }
    catch (err) {
        log("main", `writeBootHtml fallback to index.html: ${String(err)}`);
        return indexPath;
    }
}
electron_1.app.whenReady().then(async () => {
    electron_1.Menu.setApplicationMenu(null);
    log("main", `app ready, preferred port=${preferredPort()}`);
    try {
        activePort = await pickPort();
    }
    catch (err) {
        log("main", `pickPort failed: ${String(err)}`);
        electron_1.dialog.showErrorBox("Harness failed", `Could not find a free port: ${String(err)}`);
        return;
    }
    const { projectRoot } = sidecarPaths();
    pushRecent(projectRoot);
    startBackend(activePort, projectRoot);
    createWindow(activePort, projectRoot);
    electron_1.ipcMain.handle("select-workspace", async () => {
        if (!win)
            return null;
        const picked = await electron_1.dialog.showOpenDialog(win, {
            properties: ["openDirectory"],
        });
        if (picked.canceled || !picked.filePaths.length)
            return null;
        const root = picked.filePaths[0];
        pushRecent(root);
        saveLastWorkspace(root);
        return { projectRoot: root };
    });
    electron_1.ipcMain.handle("get-recent-workspaces", async () => loadRecentsRoots()
        .filter((r) => isDir(r))
        .map((r) => ({ root: r, name: node_path_1.default.basename(r) || r })));
    electron_1.ipcMain.handle("forget-workspace", async (_event, root) => {
        if (typeof root !== "string" || !root)
            return false;
        // Forgetting is not deleting: the folder stays on disk, only the app's
        // memory of it goes. If it was the last folder opened, the remembered
        // workspace goes with it so the next boot does not resurrect it.
        const kept = loadRecentsRoots().filter((r) => r !== root);
        saveRecentsRoots(kept);
        const last = workspaceFile();
        try {
            if (node_fs_1.default.existsSync(last) &&
                JSON.parse(node_fs_1.default.readFileSync(last, "utf-8")).projectRoot === root) {
                node_fs_1.default.unlinkSync(last);
            }
        }
        catch (err) {
            log("main", `could not clear last workspace: ${String(err)}`);
        }
        return true;
    });
    electron_1.ipcMain.handle("get-storage", async () => {
        const dataDir = currentDataDir();
        const source = process.env.HARNESS_APP_DATA
            ? "env"
            : loadCustomDataDir()
                ? "custom"
                : "default";
        return {
            dataDir,
            source,
            sessionsDir: node_path_1.default.join(dataDir, "sessions"),
            providersFile: node_path_1.default.join(dataDir, "providers.json"),
            agentsFile: node_path_1.default.join(dataDir, "agents.json"),
        };
    });
    electron_1.ipcMain.handle("choose-data-dir", async () => {
        if (!win)
            return null;
        const picked = await electron_1.dialog.showOpenDialog(win, {
            properties: ["openDirectory", "createDirectory"],
        });
        if (picked.canceled || !picked.filePaths.length)
            return null;
        const dir = picked.filePaths[0];
        try {
            node_fs_1.default.mkdirSync(node_path_1.default.dirname(dataDirFile()), { recursive: true });
            node_fs_1.default.writeFileSync(dataDirFile(), JSON.stringify({ dataDir: dir }));
        }
        catch (err) {
            log("main", `could not save data dir: ${String(err)}`);
            return null;
        }
        return dir;
    });
    electron_1.ipcMain.handle("reset-data-dir", async () => {
        try {
            node_fs_1.default.unlinkSync(dataDirFile());
        }
        catch {
            /* already gone */
        }
        return true;
    });
    electron_1.ipcMain.handle("open-path", async (_event, target) => {
        if (typeof target !== "string" || !target)
            return false;
        const failure = await electron_1.shell.openPath(target);
        if (failure)
            log("main", `could not open ${target}: ${failure}`);
        return !failure;
    });
    electron_1.ipcMain.handle("restart-app", async () => {
        killBackend();
        electron_1.app.relaunch();
        electron_1.app.exit(0);
    });
    electron_1.app.on("activate", () => {
        if (electron_1.BrowserWindow.getAllWindows().length === 0)
            createWindow(activePort, activeRoot);
    });
});
electron_1.app.on("window-all-closed", () => {
    killBackend();
    if (process.platform !== "darwin")
        electron_1.app.quit();
});
electron_1.app.on("quit", () => killBackend());
//# sourceMappingURL=main.js.map