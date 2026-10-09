import { contextBridge, ipcRenderer } from "electron";
import type { SidecarConfig, WorkspaceRef } from "../shared/preload-types";

contextBridge.exposeInMainWorld("harness", {
  onConfig: (cb: (cfg: SidecarConfig) => void): void => {
    ipcRenderer.on("sidecar-config", (_event, cfg) => cb(cfg as SidecarConfig));
  },
  selectWorkspace: (): Promise<{ projectRoot: string } | null> =>
    ipcRenderer.invoke("select-workspace") as Promise<{ projectRoot: string } | null>,
  recentWorkspaces: (): Promise<WorkspaceRef[]> =>
    ipcRenderer.invoke("get-recent-workspaces") as Promise<WorkspaceRef[]>,
});

contextBridge.exposeInMainWorld("harnessInfo", {
  versions: process.versions,
});
