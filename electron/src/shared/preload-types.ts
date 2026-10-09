export interface SidecarConfig {
  port: number;
  projectRoot: string;
}

export interface WorkspaceRef {
  root: string;
  name: string;
}

export interface PreloadHarness {
  onConfig: (cb: (cfg: SidecarConfig) => void) => void;
  selectWorkspace: () => Promise<{ projectRoot: string } | null>;
  recentWorkspaces: () => Promise<WorkspaceRef[]>;
}
