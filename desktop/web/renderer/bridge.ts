/* Tauri replacement for the Electron preload bridge.

   The renderer only ever talks to `window.harness`; this module supplies the
   same object on top of Tauri IPC, so the UI code stays untouched. The sidecar
   port and the workspace root come from the Rust side (it owns the process). */
import type {
  PreloadHarness,
  SidecarConfig,
  StorageInfo,
  WorkspaceRef,
} from "../shared/preload-types";

interface TauriInvoke {
  core: {
    invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T>;
  };
}

declare global {
  interface Window {
    __TAURI__?: TauriInvoke;
  }
}

function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const tauri = window.__TAURI__;
  if (!tauri) return Promise.reject(new Error("tauri runtime unavailable"));
  return tauri.core.invoke<T>(cmd, args);
}

let onConfigHandler: ((cfg: SidecarConfig) => void) | null = null;
let configValue: SidecarConfig | null = null;

const harness: PreloadHarness = {
  onConfig(cb) {
    onConfigHandler = cb;
    if (configValue) cb(configValue);
  },
  selectWorkspace: () =>
    invoke<{ projectRoot: string } | null>("select_workspace"),
  recentWorkspaces: () => invoke<WorkspaceRef[]>("recent_workspaces"),
  forgetWorkspace: (root) =>
    invoke<boolean>("forget_workspace", { root }).catch(() => false),
  storageInfo: () => invoke<StorageInfo>("get_storage"),
  chooseDataDir: () =>
    invoke<string | null>("choose_data_dir").catch(() => null),
  resetDataDir: () => invoke<boolean>("reset_data_dir").catch(() => false),
  openPath: (target) =>
    invoke<boolean>("open_path", { target }).catch(() => false),
  restartApp: () => invoke<void>("restart_app").catch(() => undefined),
};

window.harness = harness;

void invoke<SidecarConfig>("sidecar_config")
  .then((cfg) => {
    configValue = cfg;
    onConfigHandler?.(cfg);
  })
  .catch(() => {
    /* the renderer falls back to its default port and boots anyway */
  });

/* The window is frameless-less but the topbar acts as a title bar. Tauri's
   drag-region attribute handles the empty areas; this covers the rest so a
   press anywhere on the bar that is not a control moves the window. */
const WINDOW_API = "plugin:window|start_dragging";

document.addEventListener("mousedown", (event) => {
  if (event.button !== 0 || event.detail > 1) return;
  const target = event.target as HTMLElement | null;
  if (!target) return;
  if (!target.closest("#topbar")) return;
  if (target.closest("button, a, input, textarea, select, .no-drag")) return;
  if (target.hasAttribute("data-tauri-drag-region")) return;
  void invoke(WINDOW_API).catch(() => undefined);
});
