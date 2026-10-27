import { contextBridge, ipcRenderer } from "electron";
import type {
  SidecarConfig,
  StorageInfo,
  WorkspaceRef,
} from "../shared/preload-types";

contextBridge.exposeInMainWorld("harness", {
  onConfig: (cb: (cfg: SidecarConfig) => void): void => {
    ipcRenderer.on("sidecar-config", (_event, cfg) => cb(cfg as SidecarConfig));
  },
  selectWorkspace: (): Promise<{ projectRoot: string } | null> =>
    ipcRenderer.invoke("select-workspace") as Promise<{
      projectRoot: string;
    } | null>,
  recentWorkspaces: (): Promise<WorkspaceRef[]> =>
    ipcRenderer.invoke("get-recent-workspaces") as Promise<WorkspaceRef[]>,
  forgetWorkspace: (root: string): Promise<boolean> =>
    ipcRenderer.invoke("forget-workspace", root) as Promise<boolean>,
  storageInfo: (): Promise<StorageInfo> =>
    ipcRenderer.invoke("get-storage") as Promise<StorageInfo>,
  chooseDataDir: (): Promise<string | null> =>
    ipcRenderer.invoke("choose-data-dir") as Promise<string | null>,
  resetDataDir: (): Promise<boolean> =>
    ipcRenderer.invoke("reset-data-dir") as Promise<boolean>,
  openPath: (target: string): Promise<boolean> =>
    ipcRenderer.invoke("open-path", target) as Promise<boolean>,
  restartApp: (): Promise<void> =>
    ipcRenderer.invoke("restart-app") as Promise<void>,
});

contextBridge.exposeInMainWorld("harnessInfo", {
  versions: process.versions,
});
