export type Mode = "build" | "ask" | "plan";

export type WorkflowPhase =
  | "planning"
  | "replanning"
  | "researching"
  | "validating"
  | "handing_off"
  | "building"
  | "reviewing"
  | "completed"
  | "blocked"
  | "failed"
  | null;

export interface PhaseEventPayload {
  phase: WorkflowPhase;
}

export type PlanCompletion = "complete" | "partial" | "unusable" | "cancelled";
export interface PlanRecord {
  session_id?: string;
  task?: string;
  plan?: string;
  completion?: PlanCompletion;
  phase?: string;
  attempts?: number;
}

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

export interface ModelEntry {
  id: string;
  name: string;
  model: string;
  reasoning_effort?: string | null;
  temperature?: number | null;
  context_window?: number | null;
  max_output_tokens?: number | null;
  supports_tools?: boolean;
  supports_reasoning?: boolean;
  supports_images?: boolean;
  supports_text_tools?: boolean;
  extra_params?: Record<string, string | number | boolean | null>;
  disabled?: boolean;
}

export interface ProviderGroup {
  id: string;
  name: string;
  base_url: string;
  headers?: Record<string, string>;
  disabled?: boolean;
  models: ModelEntry[];
}

export interface ProviderEntry extends ModelEntry {
  base_url: string;
  headers?: Record<string, string>;
  disabled?: boolean;
  provider_id: string;
  provider_name: string;
  key?: string;
  active?: boolean;
}

export interface ProviderState {
  groups: ProviderGroup[];
  providers: ProviderEntry[];
  active_provider_id: string | null;
  active_model_id: string | null;
  statuses: Record<string, ProviderEntry>;
  migration: string[];
}

export interface RequestFailure {
  kind: string;
  headline: string;
  detail: string;
  status?: number | null;
  transient?: boolean;
  exhausted?: boolean;
  retries?: number | null;
  hint?: string;
  model?: string | null;
  model_id?: string | null;
}

export interface RetryProgress {
  attempt: number;
  retries_total?: number | null;
  wait: number;
  elapsed: number;
  window: number;
  model?: string | null;
  model_id?: string | null;
  kind?: string;
  headline?: string;
  detail?: string;
  status?: number | null;
  retry_after?: number | null;
  transient?: boolean;
  exhausted?: boolean;
  hint?: string;
}

export interface RetryInfo {
  count: number;
  text: string;
  model?: string | null;
}

export interface SessionListItem {
  id: string;
  title: string;
}

export interface AgentRole {
  enabled: boolean;
  model_id: string;
  instructions: string;

  approve?: boolean;

  auto?: boolean;

  sensitivity?: string;
}
export interface AgentOversight extends AgentRole {
  intensity: string;
  max_rounds: number;
}

export interface ReviewerConfig {
  model_id: string;
  label: string;
  enabled: boolean;

  focus: string;

  state?: string;
  usable?: boolean;
  reason?: string;
}

export interface ReviewConfig {
  enabled: boolean;

  auto_run: string;

  mode: string;
  reviewers: ReviewerConfig[];
  max_reviewers: number;
  max_files: number;
  max_diff_bytes: number;
  max_file_bytes: number;
  max_tokens: number;
  severity_threshold: string;
  full_context: boolean;
  exclusions: string[];
  prompt: string;
}

export interface AgentConfig {
  plan: AgentRole;
  build: AgentRole;
  oversight: AgentOversight;
  review: ReviewConfig;
}

export type ReviewSeverity = "blocker" | "high" | "medium" | "low" | "nit";

export interface ReviewFinding {
  id: string;
  category: string;
  category_label: string;
  severity: ReviewSeverity;
  file: string;
  line: number;
  end_line: number;
  summary: string;
  why: string;
  fix: string;
  confidence: string;

  models: string[];

  conflict_with: string[];
}

export interface ReviewModelStatus {
  model_id: string;
  label: string;
  status: "ran" | "skipped" | "failed" | "cancelled";
  error: string;
  note: string;
  finding_count: number;
  rejected_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  seconds: number;
}

export interface ReviewScope {
  mode: string;
  base: string;
  is_repo: boolean;
  total_files: number;
  files: Array<{
    path: string;
    status: string;
    added: number;
    removed: number;
  }>;
  skipped: Array<{ path: string; reason: string }>;
}

export interface ReviewReport {
  review_id: string;
  created_at: number;
  verdict: "clean" | "suggestions" | "changes_requested" | "failed" | "nothing";
  verdict_label: string;
  findings: ReviewFinding[];
  suppressed_count: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
  models: ReviewModelStatus[];
  threshold: string;
  redacted: number;
  seconds: number;
  message_index: number;
  cache_key: string;
  triggered_by: string;
  stale: boolean;
  dismissed: string[];
  scope: ReviewScope;
  cached?: boolean;
  error?: string;
  action?: string;
  message?: string;
}

export type ReviewStage =
  | "offer"
  | "started"
  | "running"
  | "progress"
  | "finding"
  | "results"
  | "completed"
  | "failed"
  | "nothing"
  | "cancelled"
  | "stale"
  | "updated";

export interface ReviewEstimate {
  tokens_per_model: number;
  tokens_total: number;
  limit: number;
  over_limit: boolean;
}

export interface ReviewEventPayload {
  stage: ReviewStage;
  review_id?: string;
  message?: string;
  error?: string;
  action?: string;
  cached?: boolean;
  redacted?: number;
  files?: string[];
  reviewers?: Array<{ model_id: string; label: string }>;
  skipped?: Array<{ model_id: string; reason: string }>;
  model_id?: string;
  label?: string;
  state?: string;
  findings?: number;
  rejected?: number;
  seconds?: number;
  tokens?: number;
  limit?: number;
  finding?: ReviewFinding;
  report?: ReviewReport;
  estimate?: ReviewEstimate;
}

export interface PlanEventPayload {
  status: "generating" | "ready" | "failed" | "skipped";
  request?: string;
  plan?: string;
  model?: string;
  reason?: string;

  inline?: boolean;

  auto?: boolean;
}

export interface IntentEventPayload {
  intent: Mode;
  source:
    | "command"
    | "sticky"
    | "default"
    | "user"
    | "model"
    | "auto"
    | "fallback"
    | "approved"
    | "skip";
  sticky?: boolean;
}

export interface AutoPlanEventPayload {
  reason: string;
}

export interface DecisionEventPayload {
  status:
    | "reviewing"
    | "approved"
    | "revised"
    | "clarify"
    | "escalated"
    | "error"
    | "skipped";
  tool?: string;
  round?: number;
  verdict?: string;
  reasoning?: string;
  risks?: string[];
  correction?: string;
  question?: string;
}

export interface TabRef {
  session_id: string | null;
  title: string;
  dirty?: boolean;
}

export interface TodoItem {
  content: string;
  status: string;
  activeForm?: string;
}

export type PendingKind = "steer" | "queue";
export interface PendingItem {
  id: string;
  kind: PendingKind;
  text: string;
  attachments?: AttachmentMeta[];
}

export interface AttachmentMeta {
  id: string;
  name: string;
  type: string;
  size: number;
  path?: string;
  error?: string;
  missing?: boolean;
}

export interface AttachmentDraft extends AttachmentMeta {
  status: "uploading" | "ready" | "error";
  progress: number;
  detail?: string;

  uploadId?: string;
}

export interface SessionOp {
  session: string;
  root?: string;
  req_id?: number;
  submission_id?: string;
}

export type OpPayload =
  | {
      op: "submit";
      text: string;
      submission_id?: string;
      attachments?: AttachmentMeta[];
    }
  | {
      op: "enqueue";
      text: string;
      kind: PendingKind;
      submission_id?: string;
      attachments?: AttachmentMeta[];
    }
  | { op: "pending_edit"; id: string; text: string }
  | { op: "pending_remove"; id: string }
  | { op: "pending_remove_attachment"; id: string; attachment_id: string }
  | { op: "attach_begin"; name: string; type: string; size: number }
  | { op: "attach_part"; id: string; data: string }
  | { op: "attach_finish"; id: string }
  | { op: "attach_discard"; id: string }
  | { op: "open"; id: string }
  | { op: "stop" }
  | { op: "set_mode"; mode: Mode }
  | { op: "plan_action"; action: "approve" | "skip" | "cancel" }
  | { op: "rewind_to"; count: number }
  | { op: "rewind_preview"; count: number }
  | {
      op: "resend";
      count: number;
      text: string;
      attachments?: AttachmentMeta[];
    }
  | { op: "git_status" }
  | { op: "git_create_branch"; name: string }
  | { op: "list_sessions" }
  | { op: "get_transcript"; id: string }
  | { op: "rename_session"; root?: string; id: string; title: string }
  | { op: "delete_session"; root?: string; id: string }
  | { op: "list_providers" }
  | { op: "set_active"; id: string }
  | { op: "set_effort"; id: string; effort: string | null }
  | { op: "set_permissions"; mode: string }
  | { op: "get_agents" }
  | { op: "set_agents"; config: AgentConfig }
  | { op: "get_plan"; id?: string }
  | { op: "save_provider"; entry: ProviderEntry; key?: string }
  | { op: "delete_provider"; id: string; scope?: "model" | "provider" }
  | { op: "test_provider"; id: string }
  | {
      op: "fetch_model_info";
      model: string;
      provider_id?: string;
      base_url?: string;
      key?: string;
    }
  | { op: "migrate_keys" }
  | { op: "get_context" }
  | { op: "ws_sessions"; root: string }
  | {
      op: "approval_reply";
      id: string;
      allow: boolean;

      choice?: "skip";
    }
  | { op: "pick_reply"; id: string; index: number | null }
  | { op: "retry"; count: number; text: string }
  | {
      op: "review_start";
      args?: Record<string, unknown>;
      models?: string[];
      focus?: string;
      severity?: string;
      force?: boolean;
    }
  | { op: "review_cancel" }
  | {
      op: "review_action";
      action: "dismiss" | "send";
      review_id: string;
      finding_id?: string;
      finding_ids?: string[];

      apply?: boolean;
    }
  | { op: "review_report" };

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

  info?: { found: boolean; applied: string[]; note?: string };
  ready?: { project: string; project_root: string };
}
