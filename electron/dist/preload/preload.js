"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
electron_1.contextBridge.exposeInMainWorld("harness", {
    onConfig: (cb) => {
        electron_1.ipcRenderer.on("sidecar-config", (_event, cfg) => cb(cfg));
    },
    selectWorkspace: () => electron_1.ipcRenderer.invoke("select-workspace"),
    recentWorkspaces: () => electron_1.ipcRenderer.invoke("get-recent-workspaces"),
    forgetWorkspace: (root) => electron_1.ipcRenderer.invoke("forget-workspace", root),
    storageInfo: () => electron_1.ipcRenderer.invoke("get-storage"),
    chooseDataDir: () => electron_1.ipcRenderer.invoke("choose-data-dir"),
    resetDataDir: () => electron_1.ipcRenderer.invoke("reset-data-dir"),
    openPath: (target) => electron_1.ipcRenderer.invoke("open-path", target),
    restartApp: () => electron_1.ipcRenderer.invoke("restart-app"),
});
electron_1.contextBridge.exposeInMainWorld("harnessInfo", {
    versions: process.versions,
});
//# sourceMappingURL=preload.js.map