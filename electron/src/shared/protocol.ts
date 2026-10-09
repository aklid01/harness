export type Mode = "build" | "plan";

export interface GitInfo {
  has_git: boolean;
  branch: string;
  default_branch: string;
  is_protected: boolean;
}

export interface ContextInfo extends GitInfo {
  project: string;
  project_root: string;
}

export interface ProviderEntry {
  id: string;
  name: string;
  base_url: string;
  model: string;
  headers?: Record<string, string>;
  disabled?: boolean;
  reasoning_effort?: string | null;
  temperature?: string | number | null;
}

export interface SessionListItem {
  id: string;
  title: string;
}

export interface TodoItem {
  content: string;
  status: string;
  activeForm?: string;
}

export interface SessionOp {
  session: string;
  root?: string;
  req_id?: number;
}

export type OpPayload =
  | { op: "submit"; text: string; mode: Mode }
  | { op: "open"; id: string; mode: Mode }
  | { op: "stop" }
  | { op: "rewind_to"; count: number }
  | { op: "rewind_preview"; count: number }
  | { op: "resend"; count: number; text: string; mode: Mode }
  | { op: "git_status" }
  | { op: "git_create_branch"; name: string }
  | { op: "list_sessions" }
  | { op: "get_transcript"; id: string }
  | { op: "rename_session"; id: string; title: string }
  | { op: "delete_session"; id: string }
  | { op: "list_providers" }
  | { op: "set_active"; id: string }
  | { op: "set_effort"; id: string; effort: string | null }
  | { op: "set_permissions"; mode: string }
  | { op: "save_provider"; entry: ProviderEntry; key?: string }
  | { op: "delete_provider"; id: string }
  | { op: "test_provider"; id: string }
    | { op: "migrate_keys" }
    | { op: "get_context" }
    | { op: "ws_sessions"; root: string }
  | { op: "approval_reply"; id: string; allow: boolean }
  | { op: "pick_reply"; id: string; index: number | null };

export type ClientOp = SessionOp & OpPayload;

export interface ServerEnvelope {
  session?: string;
  event?: string;
  payload?: Record<string, unknown>;
  request?: "approval" | "pick";
  id?: string;
  reason?: string;
  tool?: string;
  command?: string;
  target?: string;
  why?: string;
  risk?: string;
  risk_reason?: string;
  title?: string;
  rows?: string[];
  reply?: string;
  req_id?: number;
  ok?: boolean;
  data?: unknown;
  error?: string;
  ready?: { project: string; project_root: string };
}
