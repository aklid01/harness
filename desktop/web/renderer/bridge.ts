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

const readConfig = async (tries: number): Promise<void> => {
  try {
    const cfg = await invoke<SidecarConfig>("sidecar_config");
    configValue = cfg;
    onConfigHandler?.(cfg);
  } catch {
    if (tries > 0) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await readConfig(tries - 1);
      return;
    }

    document.dispatchEvent(new Event("harness:config-failed"));
  }
};
void readConfig(5);

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
