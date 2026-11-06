export interface SidecarConfig {
  port: number;
  projectRoot: string;

  authToken: string;
}

export interface WorkspaceRef {
  root: string;
  name: string;
}

export interface StorageInfo {
  dataDir: string;
  source: "default" | "custom" | "env";
  sessionsDir: string;
  providersFile: string;
  agentsFile: string;
}

export interface PreloadHarness {
  onConfig: (cb: (cfg: SidecarConfig) => void) => void;
  selectWorkspace: () => Promise<{ projectRoot: string } | null>;
  recentWorkspaces: () => Promise<WorkspaceRef[]>;
  forgetWorkspace: (root: string) => Promise<boolean>;
  storageInfo: () => Promise<StorageInfo>;
  chooseDataDir: () => Promise<string | null>;
  resetDataDir: () => Promise<boolean>;
  openPath: (target: string) => Promise<boolean>;
  restartApp: () => Promise<void>;
}
