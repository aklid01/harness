import type { ClientOp, ContextInfo, OpPayload, ProviderEntry, ProviderGroup, ProviderState, RetryInfo, ServerEnvelope, SessionListItem, TodoItem } from "../shared/protocol";
import type { PreloadHarness, WorkspaceRef } from "../shared/preload-types";

declare global {
  interface Window {
    harness?: PreloadHarness;
  }
}

/* ---------------- icon set ---------------- */
const stroke = (body: string): string =>
  `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICONS = {
  chat: stroke('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  check: stroke('<polyline points="20 6 9 17 4 12"/>'),
  copy: stroke('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  edit: stroke('<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'),
  rewind: stroke('<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'),
  trash: stroke('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  chev: stroke('<polyline points="9 18 15 12 9 6"/>'),
  info: stroke('<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>'),
  pending: stroke('<circle cx="12" cy="12" r="9"/>'),
  doing: stroke('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none"/>'),
  done: stroke('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'),
  folder: stroke('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
  arrow: stroke('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),
  layers: stroke('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'),
  palette: stroke('<circle cx="8" cy="8" r="5.5"/><rect x="14" y="14" width="5.5" height="5.5" rx="1.2"/>'),
  sliders: stroke('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>'),
  layout: stroke('<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/>'),
  close: stroke('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  branch: stroke('<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>'),
};

/* ---------------- preferences (global, localStorage) ---------------- */
interface Prefs {
  theme: string;
  fontScale: "sm" | "base" | "lg";
  density: "comfortable" | "compact";
  reduceMotion: boolean;
  confirmProtected: boolean;
  defaultMode: "build" | "plan";
  effortGlobal: string;
  effortByWs: Record<string, string>;
  providerByWs: Record<string, string>;
  lastSessionByWs: Record<string, string>;
}
const PREFS_KEY = "harness:prefs:v1";
const DEFAULT_PREFS: Prefs = {
  theme: "neon", fontScale: "base", density: "comfortable", reduceMotion: false,
  confirmProtected: true, defaultMode: "build", effortGlobal: "default",
  effortByWs: {}, providerByWs: {}, lastSessionByWs: {},
};
function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const saved = JSON.parse(raw) as Partial<Prefs>;
    return {
      ...DEFAULT_PREFS, ...saved,
      effortByWs: saved.effortByWs ?? {}, providerByWs: saved.providerByWs ?? {}, lastSessionByWs: saved.lastSessionByWs ?? {},
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}
function savePrefs(): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* storage unavailable */
  }
}

/* ---------------- boot params + logging ---------------- */
const logLines: string[] = [];
function dlog(tag: string, ...parts: unknown[]): void {
  const stamp = new Date().toISOString().slice(11, 23);
  const line = `[${stamp}][${tag}] ${parts.map(String).join(" ")}`;
  logLines.push(line);
  if (logLines.length > 500) logLines.splice(0, logLines.length - 500);
  console.log(line);
  const drawer = document.getElementById("logDrawer");
  if (drawer && !drawer.classList.contains("hidden")) {
    drawer.textContent = logLines.join("\n");
    drawer.scrollTop = drawer.scrollHeight;
  }
}
window.addEventListener("error", (e) => dlog("boot", "WINDOW-ERROR:", e.message, e.filename, e.lineno));

let prefs = loadPrefs();
function applyAppearance(): void {
  const d = document.documentElement;
  d.dataset["theme"] = prefs.theme;
  if (prefs.fontScale === "base") delete d.dataset["font"];
  else d.dataset["font"] = prefs.fontScale;
  d.dataset["density"] = prefs.density;
  if (prefs.reduceMotion) d.dataset["motion"] = "reduce";
  else delete d.dataset["motion"];
}
applyAppearance();

/* ---------------- DOM refs ---------------- */
const el = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const chat = el<HTMLDivElement>("chat");
const emptyState = el<HTMLDivElement>("emptyState");
const statusBar = el<HTMLDivElement>("activity");
const activityText = el<HTMLSpanElement>("activityText");
const statusMode = el<HTMLSpanElement>("statusMode");
const entry = el<HTMLTextAreaElement>("entry");
const promptZone = el<HTMLDivElement>("promptZone");
const editBar = el<HTMLDivElement>("editBar");
const expandBtn = el<HTMLButtonElement>("expandBtn");
const shell = el<HTMLDivElement>("shell");
const treeBox = el<HTMLDivElement>("wsTree");
const wsCount = el<HTMLSpanElement>("wsCount");
const tabsBox = el<HTMLDivElement>("tabs");
const planBox = el<HTMLDivElement>("plan");
const planProgress = el<HTMLSpanElement>("planProgress");
const usageBox = el<HTMLDivElement>("usage");
const contextBox = el<HTMLDivElement>("context");
const connBox = el<HTMLSpanElement>("conn");
const connText = el<HTMLSpanElement>("connText");
const modeBtn = el<HTMLButtonElement>("modeBtn");
const modeLabel = el<HTMLSpanElement>("modeLabel");
const modeMenu = el<HTMLDivElement>("modeMenu");
const providerBtn = el<HTMLButtonElement>("providerBtn");
const providerLabel = el<HTMLSpanElement>("providerLabel");
const providerMenu = el<HTMLDivElement>("providerMenu");
const effortBtn = el<HTMLButtonElement>("effortBtn");
const effortLabel = el<HTMLSpanElement>("effortLabel");
const effortMenu = el<HTMLDivElement>("effortMenu");
const permsBtn = el<HTMLButtonElement>("permsBtn");
const permsLabel = el<HTMLSpanElement>("permsLabel");
const permsMenu = el<HTMLDivElement>("permsMenu");
const sendstop = el<HTMLButtonElement>("sendstop");
const modal = el<HTMLDivElement>("modal");
const modalBox = el<HTMLDivElement>("modalBox");
const contextMenu = el<HTMLDivElement>("contextMenu");
const usageChip = el<HTMLButtonElement>("usageChip");
const usageChipText = el<HTMLSpanElement>("usageChipText");
const usagePop = el<HTMLDivElement>("usagePop");
const landing = el<HTMLDivElement>("landing");

const REQUIRED_IDS = [
  "chat", "emptyState", "activity", "activityText", "statusMode", "entry", "promptZone", "editBar", "expandBtn", "shell",
  "wsTree", "wsCount", "tabs", "plan", "planProgress", "usage", "context", "conn", "connText",
  "modeBtn", "modeLabel", "modeMenu", "providerBtn", "providerLabel", "providerMenu",
  "effortBtn", "effortLabel", "effortMenu", "permsBtn", "permsLabel", "permsMenu",
  "sendstop", "modal", "modalBox", "contextMenu", "usageChip", "usageChipText", "usagePop",
  "toggleLog", "logDrawer", "btnNew", "btnOpenFolder", "btnSettings", "toggleLeft", "toggleRight", "side", "right", "editCancel", "newTab", "landing", "landingNew",
];
const missingIds = REQUIRED_IDS.filter((id) => !document.getElementById(id));
if (missingIds.length > 0) {
  const msg = `UI version mismatch (stale build?): missing ${missingIds.join(", ")} - rebuild with npm run build`;
  dlog("boot", "FATAL:", msg, "href=", location.href);
  const c = document.getElementById("connText");
  if (c) c.textContent = "ui mismatch";
  throw new Error(msg);
}

/* ---------------- per-tab context ---------------- */
interface Ctx {
  id: string;
  root: string;
  project: string;
  hadReady: boolean;
  sessionId: string | null;
  sessionTitle: string;
  openIntent: "new" | "session" | "resume";
  openId: string | null;
  busy: boolean;
  draft: boolean;
  totals: Record<string, number>;
  planItems: TodoItem[];
  modeVal: "build" | "plan";
  providerId: string | null;
  effortVal: string;
  permsVal: string;
  gitInfo: { has_git: boolean; branch: string; default_branch: string; is_protected: boolean };
  lastBranch: string;
  branchAck: string;
  pendingText: string | null;
  pendingRewind: number | null;
  pendingEffort: string | null;
  pendingProviderId: string | null;
  pendingOpenSessionId: string | null;
  pendingResume: boolean;
  failure: { retry: RetryInfo | null; headline: string; model: string | null } | null;
  retryCard: HTMLDivElement | null;
  streamPre: HTMLDivElement | null;
  streamWrap: HTMLElement | null;
  streamText: string[];
  thinking: HTMLDivElement | null;
  toolCards: Map<string, HTMLDivElement>;
  chatBody: HTMLElement;
  entryDraft: string;
}
let tabs: Ctx[] = [];
let activeCtx: Ctx | null = null;
const ctxById = new Map<string, Ctx>();
let tabSeq = 0;
let reqSeq = 0;

/* single shared backend connection (one WebSocket for every tab) */
let sharedWs: WebSocket | null = null;
let wsGen = 0;
let connected = false;
let bootPort = location.search ? (new URLSearchParams(location.search).get("port") ?? "8765") : "8765";
let bootRoot = new URLSearchParams(location.search).get("projectRoot") ?? "";

/* shared (app-wide) */
let recents: WorkspaceRef[] = [];
let sessionsByRoot: Record<string, SessionListItem[]> = {};
let collapsedWs = new Set<string>();
let providerGroups: ProviderGroup[] = [];
let providerModels: ProviderEntry[] = [];
let providerStatuses: Record<string, ProviderEntry> = {};
let providerOptions: Array<{ id: string; label: string; sub: string }> = [];
let providerActiveModelId: string | null = null;
let providerActiveGroupId: string | null = null;
let providerMigration: string[] = [];
const providerById = new Map<string, ProviderEntry>();
let settingsRequested = false;
let settingsLastTab = "models";
let activeApproval: { ctx: Ctx; id: string } | null = null;
let pendingRewindCount: number | null = null;
let popHideTimer: number | null = null;

const EFFORTS = ["default", "low", "medium", "high", "xhigh"];
const PERMS: Array<{ label: string; value: string }> = [
  { label: "Default", value: "default" },
  { label: "Strict", value: "strict" },
  { label: "Allow All", value: "allow-all" },
  { label: "Autopilot", value: "autopilot" },
];
const THEMES = [
  { id: "neon", name: "Neon", desc: "Black + neon green", swatch: ["#060706", "#131614", "#3ee08a"] },
  { id: "cobalt", name: "Cobalt", desc: "Deep navy + azure", swatch: ["#06080d", "#121722", "#4c8dff"] },
  { id: "ember", name: "Ember", desc: "Graphite + warm amber", swatch: ["#0a0806", "#191410", "#f0883e"] },
  { id: "graphite", name: "Graphite", desc: "Neutral, low-distraction", swatch: ["#09090a", "#161618", "#c6cad3"] },
];
const PLAN_ICON: Record<string, string> = { pending: ICONS.pending, in_progress: ICONS.doing, completed: ICONS.done };

function makeCtx(root: string, project: string, intent: Ctx["openIntent"], openId: string | null): Ctx {
  const ctx: Ctx = {
    id: `t${++tabSeq}`, root, project,
    hadReady: false,
    sessionId: null, sessionTitle: "New Session", openIntent: intent, openId: openId ?? null,
    busy: false, draft: false, totals: {}, planItems: [], modeVal: prefs.defaultMode,
    providerId: null, effortVal: prefs.effortGlobal, permsVal: "default",
    gitInfo: { has_git: false, branch: "", default_branch: "", is_protected: false }, lastBranch: "", branchAck: "",
    pendingText: null, pendingRewind: null, pendingEffort: null, pendingProviderId: null, pendingOpenSessionId: null, pendingResume: false,
    failure: null, retryCard: null,
    streamPre: null, streamWrap: null, streamText: [], thinking: null, toolCards: new Map(),
    chatBody: document.createElement("div"), entryDraft: "",
  };
  ctx.chatBody.className = "chat-body";
  ctxById.set(ctx.id, ctx);
  return ctx;
}

/* A non-tab context used for app-level ops (providers, settings) so they work
   even when no session tab is open (e.g. from the landing state). */
let appCtx: Ctx | null = null;
function ensureAppCtx(): Ctx {
  const root = activeCtx?.root ?? bootRoot ?? "";
  if (!appCtx) {
    appCtx = makeCtx(root, baseName(root || "Workspace"), "resume", null);
    appCtx.draft = true;
  } else if (appCtx.root !== root) {
    appCtx.root = root;
  }
  return appCtx;
}

/* ---------------- generic helpers ---------------- */
function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function baseName(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : p;
}
function firstUserTitle(messages: Array<Record<string, unknown>>): string {
  for (const m of messages) {
    if (m["role"] === "user") {
      const text = String(m["content"] ?? "").split(/\s+/).join(" ").trim();
      if (text && !text.startsWith("<")) return text.slice(0, 60);
    }
  }
  return "New Session";
}
function displayTitle(raw: string): string {
  return !raw || raw === "(empty)" ? "New Session" : raw;
}
function relTime(id: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(id);
  if (!m) return "";
  const when = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]));
  const diff = Date.now() - when.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d`;
  return when.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function renderMarkdown(text: string): string {
  const fenced: string[] = [];
  let out = escapeHtml(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    const label = lang || "code";
    fenced.push(`<div class="code-block"><div class="code-head"><span>${escapeHtml(label)}</span></div><pre>${code.replace(/\n$/, "")}</pre></div>`);
    return `\u0000${fenced.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  out = out
    .replace(/^####\s+(.*)$/gm, "<h4>$1</h4>").replace(/^###\s+(.*)$/gm, "<h3>$1</h3>")
    .replace(/^##\s+(.*)$/gm, "<h2>$1</h2>").replace(/^#\s+(.*)$/gm, "<h2>$1</h2>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/((?:^[ \t]*[-*][ \t]+.*\n?)+)/gm, (b) => `<ul>${b.trim().split(/\n/).map((l) => `<li>${l.replace(/^[ \t]*[-*][ \t]+/, "")}</li>`).join("")}</ul>`);
  out = out.replace(/((?:^[ \t]*\d+\.[ \t]+.*\n?)+)/gm, (b) => `<ol>${b.trim().split(/\n/).map((l) => `<li>${l.replace(/^[ \t]*\d+\.[ \t]+/, "")}</li>`).join("")}</ol>`);
  return out.replace(/\u0000(\d+)\u0000/g, (_m, i) => fenced[Number(i)]);
}
const MARQUEE = new WeakMap<HTMLElement, { scroll: Animation | null; back: Animation | null }>();
function attachMarquee(viewport: HTMLElement | null): void {
  if (!viewport) return;
  const inner = viewport.firstElementChild as HTMLElement | null;
  if (!inner) return;
  const state = { scroll: null, back: null } as { scroll: Animation | null; back: Animation | null };
  MARQUEE.set(viewport, state);
  const reset = (): void => {
    if (state.scroll) { try { state.scroll.cancel(); } catch { /* ignore */ } state.scroll = null; }
    if (state.back) { try { state.back.cancel(); } catch { /* ignore */ } state.back = null; }
    inner.style.transform = "";
  };
  viewport.addEventListener("mouseenter", () => {
    if (document.documentElement.dataset["motion"] === "reduce") return;
    const dist = inner.scrollWidth - viewport.clientWidth;
    if (dist <= 2) return;
    reset();
    state.scroll = inner.animate([{ transform: "translateX(0px)" }, { transform: `translateX(${-dist}px)` }], { duration: Math.max(2600, dist * 26), delay: 350, iterations: Infinity, direction: "alternate", easing: "ease-in-out" });
  });
  viewport.addEventListener("mouseleave", () => {
    if (!state.scroll) return;
    try {
      const current = getComputedStyle(inner).transform;
      state.scroll.cancel(); state.scroll = null;
      state.back = inner.animate([{ transform: current || "none" }, { transform: "translateX(0px)" }], { duration: 360, easing: "ease", fill: "forwards" });
      state.back.onfinish = () => { try { state.back?.cancel(); } catch { /* ignore */ } state.back = null; inner.style.transform = ""; };
    } catch { /* ignore */ }
  });
}

/* ---------------- shared DOM state (active tab) ---------------- */
function updateEmptyState(): void {
  if (!activeCtx) return;
  const has = activeCtx.chatBody.querySelector(".msg, .tool, .note, .error, .retry, .thinking") !== null;
  emptyState.classList.toggle("hidden", has);
}
function scrollBottom(ctx: Ctx): void {
  if (ctx === activeCtx) chat.scrollTop = chat.scrollHeight;
}
function setConn(text: string, state: "online" | "connecting" | "offline"): void {
  connText.textContent = text;
  connBox.className = `conn-pill ${state}`;
}
function setStatusState(state: "ready" | "working" | "error"): void {
  statusBar.classList.toggle("working", state === "working");
  statusBar.classList.toggle("error", state === "error");
}
function setActivity(text: string): void {
  activityText.textContent = text;
}
function setBusy(ctx: Ctx, value: boolean): void {
  ctx.busy = value;
  if (ctx === activeCtx) {
    updateSendState();
    setStatusState(value ? "working" : "ready");
    renderTabs();
  }
}
function updateSendState(): void {
  if (!activeCtx) return;
  sendstop.classList.toggle("busy", activeCtx.busy);
  sendstop.disabled = activeCtx.busy ? false : entry.value.trim().length === 0;
  sendstop.title = activeCtx.busy ? "Stop" : "Send";
}
function send(ctx: Ctx, op: OpPayload): number {
  if (!sharedWs || sharedWs.readyState !== WebSocket.OPEN) {
    dlog(ctx.id, "SEND-BLOCKED", op.op, "readyState=", sharedWs?.readyState ?? "null");
    if (ctx === activeCtx) setActivity("not connected to backend - waiting for reconnect…");
    return 0;
  }
  reqSeq += 1;
  const payload: ClientOp = { session: ctx.id, root: ctx.root, ...op, req_id: reqSeq } as ClientOp;
  sharedWs.send(JSON.stringify(payload));
  return reqSeq;
}

/* ---------------- chat rendering (per-tab) ---------------- */
function turnWrap(kind: "user" | "assistant"): { wrap: HTMLElement; body: HTMLDivElement } {
  const wrap = document.createElement("div");
  wrap.className = `msg ${kind}`;
  const body = document.createElement("div");
  body.className = kind;
  wrap.appendChild(body);
  return { wrap, body };
}
function addUser(ctx: Ctx, text: string): void {
  const { wrap, body } = turnWrap("user");
  body.textContent = text;
  ctx.chatBody.appendChild(wrap);
  updateEmptyStateOnly(ctx);
  scrollBottom(ctx);
}
function updateEmptyStateOnly(ctx: Ctx): void {
  if (ctx === activeCtx) updateEmptyState();
}
function beginStream(ctx: Ctx): void {
  ctx.streamText = [];
  const { wrap, body } = turnWrap("assistant");
  ctx.streamWrap = wrap;
  ctx.streamPre = body;
  ctx.chatBody.appendChild(wrap);
  updateEmptyStateOnly(ctx);
}
function appendDelta(ctx: Ctx, text: string): void {
  hideThinking(ctx);
  if (!ctx.streamPre) beginStream(ctx);
  ctx.streamText.push(text);
  if (ctx.streamPre) ctx.streamPre.textContent = ctx.streamText.join("");
  scrollBottom(ctx);
}
function endStream(ctx: Ctx, fullText: string): void {
  hideThinking(ctx);
  if (!fullText.trim()) { ctx.streamWrap?.remove(); ctx.streamWrap = null; ctx.streamPre = null; ctx.streamText = []; return; }
  if (ctx.streamPre) ctx.streamPre.innerHTML = renderMarkdown(fullText);
  ctx.streamPre = null; ctx.streamWrap = null; ctx.streamText = [];
  scrollBottom(ctx);
}
function showThinking(ctx: Ctx): void {
  if (ctx.thinking || ctx.streamPre) return;
  ctx.thinking = document.createElement("div");
  ctx.thinking.className = "note thinking";
  ctx.thinking.innerHTML = `<span class="spin"></span><span>thinking… [${ctx.modeVal}]</span>`;
  ctx.chatBody.appendChild(ctx.thinking);
  scrollBottom(ctx);
}
function hideThinking(ctx: Ctx): void {
  ctx.thinking?.remove();
  ctx.thinking = null;
}
function addNote(ctx: Ctx, text: string): void {
  const div = document.createElement("div");
  div.className = "note";
  div.innerHTML = `<span class="n-ico">${ICONS.info}</span><span>${escapeHtml(text)}</span>`;
  ctx.chatBody.appendChild(div);
  updateEmptyStateOnly(ctx);
  scrollBottom(ctx);
}
function addError(ctx: Ctx, text: string, meta?: Record<string, unknown>): void {
  hideThinking(ctx);
  // A stream that broke halfway has already delivered its answer: keep it
  // rather than throwing away what the model managed to say.
  if (ctx.streamPre && ctx.streamText.join("").trim()) {
    ctx.streamPre.innerHTML = renderMarkdown(ctx.streamText.join(""));
  } else {
    ctx.streamWrap?.remove();
  }
  ctx.streamWrap = null; ctx.streamPre = null; ctx.streamText = [];
  clearRetryCard(ctx);
  if (ctx === activeCtx) setStatusState("error");
  const retry = (meta?.["retry"] as RetryInfo | undefined) ?? null;
  const model = String(meta?.["model"] ?? "") || (retry?.model ?? "");
  ctx.failure = { retry, headline: text, model: model || null };
  const div = document.createElement("div");
  div.className = "error";
  const actions: string[] = [];
  // A permanent failure (bad key, unknown model id) is not worth re-sending.
  if (retry && meta?.["transient"] !== false) actions.push(`<button class="btn small primary" data-retry="1">Retry</button>`);
  if (providerGroups.length) actions.push(`<button class="btn small" data-switch="1">Switch model</button>`);
  div.innerHTML =
    `<b>${escapeHtml((meta?.["headline"] as string) ?? (model ? `${model} failed` : "Error - no response produced"))}</b>` +
    `<div>${escapeHtml(text)}</div>` +
    (actions.length ? `<div class="msg-actions" style="opacity:1;pointer-events:auto;margin-top:8px">${actions.join("")}</div>` : "");
  ctx.chatBody.appendChild(div);
  updateEmptyStateOnly(ctx);
  scrollBottom(ctx);
  div.querySelectorAll<HTMLButtonElement>("[data-retry]").forEach((b) => { b.onclick = (e) => { e.stopPropagation(); retryFailedRequest(ctx); }; });
  div.querySelectorAll<HTMLButtonElement>("[data-switch]").forEach((b) => { b.onclick = (e) => { e.stopPropagation(); openModelPicker(ctx); }; });
}
/* One persistent status area for the whole retry process, so a failing
   provider does not spam a card per attempt. */
function clearRetryCard(ctx: Ctx): void {
  ctx.retryCard?.remove();
  ctx.retryCard = null;
}
function showRetry(ctx: Ctx, p: Record<string, unknown>): void {
  if (ctx.retryCard?.parentElement !== ctx.chatBody) {
    clearRetryCard(ctx);
    const card = document.createElement("div");
    card.className = "retry";
    ctx.chatBody.appendChild(card);
    ctx.retryCard = card;
  }
  const card = ctx.retryCard;
  if (!card) return;
  const model = String(p["model"] ?? ctx.failure?.model ?? "the model");
  const wait = Number(p["wait"] ?? 0);
  const attempt = Number(p["attempt"] ?? 1);
  const elapsed = Number(p["elapsed"] ?? 0);
  const window = Number(p["window"] ?? 30);
  const reason = String(p["headline"] ?? p["kind"] ?? "the request failed");
  card.innerHTML =
    `<span class="r-spin"></span>` +
    `<div class="r-body"><div class="r-title">${escapeHtml(model)}</div>` +
    `<div class="r-detail">${escapeHtml(reason)}. Retrying…</div>` +
    `<div class="r-sub">Retry ${attempt} · waiting ${wait.toFixed(wait < 1 ? 1 : 0)}s · ${elapsed.toFixed(0)}s of ${window.toFixed(0)}s elapsed</div></div>`;
  if (ctx === activeCtx) setActivity(`${model} - ${reason}. Retrying…`);
  scrollBottom(ctx);
}
function retryFailedRequest(ctx: Ctx): void {
  const info = ctx.failure?.retry;
  if (!info) { setActivity("nothing to retry"); return; }
  if (ctx.busy) { setActivity("still busy - press Stop first"); return; }
  clearRetryCard(ctx);
  ctx.failure = null;
  setBusy(ctx, true);
  showThinking(ctx);
  if (!send(ctx, { op: "retry", count: info.count, text: info.text, mode: ctx.modeVal })) {
    setBusy(ctx, false);
    setActivity("retry failed - engine busy");
  }
}
/* Switching models after a failure: the session, its history and any unsaved
   composer text stay exactly where they are. */
function openModelPicker(ctx: Ctx): void {
  const usable = providerGroups.filter((g) => !g.disabled && g.models.some((m) => !m.disabled));
  if (!usable.length) { setActivity("no other model to switch to - open Settings to add one"); return; }
  const groups = usable.map((group) => {
    const rows = group.models.filter((m) => !m.disabled).map((model) => {
      const entry = providerById.get(model.id);
      if (!entry) return "";
      const isCur = entry.id === ctx.providerId;
      return `<button class="pick-item${isCur ? " selected" : ""}" data-model="${escapeHtml(entry.id)}">` +
        `<span class="pm-name">${escapeHtml(entry.name || entry.model)}</span>` +
        `<span class="pm-sub">${escapeHtml(modelSubtitle(entry))}</span></button>`;
    }).join("");
    return `<div class="pm-group"><div class="pm-group-name">${escapeHtml(group.name)}</div>${rows}</div>`;
  }).join("");
  const failed = ctx.failure?.retry;
  const hint = failed
    ? `<p class="perm-why">The request that failed can be retried with the model you pick. Your session, history and draft are untouched.</p>`
    : "";
  showModal(`<h3>Switch model</h3>${hint}<div class="pick-list pm-list">${groups}</div><div class="modal-actions"><button class="btn" id="pmCancel">Cancel</button></div>`);
  modalBox.querySelectorAll<HTMLButtonElement>("[data-model]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset["model"] ?? "";
      const entry = providerById.get(id);
      hideModal();
      const changed = id !== ctx.providerId;
      selectModel(ctx, id, { adoptEffort: true });
      clearRetryCard(ctx);
      if (ctx === activeCtx) setActivity(`switched to ${entry?.name ?? id}${changed ? "" : " (already selected)"}`);
      if (failed) {
        const card = Array.from(ctx.chatBody.querySelectorAll<HTMLElement>(".error")).pop();
        if (card) {
          const note = document.createElement("div");
          note.className = "note";
          note.innerHTML = `<span class="n-ico">${ICONS.info}</span><span>Switched to ${escapeHtml(entry?.name ?? id)}. Press Retry to try the same request with it.</span>`;
          card.after(note);
        }
        scrollBottom(ctx);
      }
    };
  });
  el<HTMLButtonElement>("pmCancel").onclick = hideModal;
}
function addToolLine(ctx: Ctx, callId: string | null, name: string, args: Record<string, unknown>): string {
  hideThinking(ctx);
  const seq = ++toolSeq[ctx.id];
  const key = callId ?? `local-${seq}`;
  const div = document.createElement("div");
  div.className = "tool";
  const summary = String(args?.["command"] ?? args?.["path"] ?? JSON.stringify(args ?? {}));
  const short = summary.length > 90 ? summary.slice(0, 87) + "..." : summary;
  div.innerHTML =
    `<span class="badge">…</span><span class="t-name">${escapeHtml(name)}</span>` +
    `<span class="t-summary">${escapeHtml(short)}</span>` +
    `<button class="t-tog" title="Toggle output" disabled>${ICONS.chev}</button><pre class="hidden"></pre>`;
  const button = div.querySelector("button") as HTMLButtonElement;
  const pre = div.querySelector("pre") as HTMLPreElement;
  button.onclick = () => { const open = pre.classList.toggle("hidden") === false; button.classList.toggle("open", open); };
  ctx.chatBody.appendChild(div);
  ctx.toolCards.set(key, div);
  updateEmptyStateOnly(ctx);
  scrollBottom(ctx);
  return key;
}
const toolSeq: Record<string, number> = {};
function finishToolLine(ctx: Ctx, callId: string | null, result: string): void {
  const div = (callId && ctx.toolCards.get(callId)) ?? [...ctx.toolCards.values()].pop();
  if (!div) return;
  const badge = div.querySelector(".badge") as HTMLSpanElement;
  const pre = div.querySelector("pre") as HTMLPreElement;
  const button = div.querySelector("button") as HTMLButtonElement;
  const lowered = (result || "").toLowerCase();
  if (lowered.startsWith("the user denied") || lowered.startsWith("blocked by policy")) { badge.textContent = "NOT RUN"; badge.className = "badge-deny"; }
  else if (lowered.startsWith("plan mode")) { badge.textContent = "SKIPPED"; badge.className = "badge-skip"; }
  else { badge.textContent = "done"; badge.className = "badge-done"; }
  const lines = (result || "(no output)").trim().split("\n");
  pre.textContent = lines.slice(0, 12).join("\n") + (lines.length > 12 ? `\n... ${lines.length - 12} more lines` : "");
  button.disabled = false;
}
function msgActions(ctx: Ctx, idx: number, kind: "user" | "assistant", text: string): HTMLDivElement {
  const bar = document.createElement("div");
  bar.className = "msg-actions";
  const mk = (label: string, icon: string, title: string, fn: () => void): void => {
    const btn = document.createElement("button");
    btn.innerHTML = `<span class="ma-ico">${icon}</span><span>${escapeHtml(label)}</span>`;
    btn.title = title;
    btn.onclick = (e) => { e.stopPropagation(); fn(); };
    bar.appendChild(btn);
  };
  if (kind === "user") mk("Edit", ICONS.edit, "Edit this message and resend (rewinds first)", () => startEdit(ctx, idx, text));
  else mk("Copy", ICONS.copy, "Copy to clipboard", () => {
    try { void navigator.clipboard?.writeText(text); setActivity("copied"); } catch { setActivity("copy failed"); }
  });
  mk("Rewind", ICONS.rewind, "Rewind and restore files to before this message", () => confirmRewind(ctx, idx));
  return bar;
}
function renderTranscript(ctx: Ctx, messages: Array<Record<string, unknown>>): void {
  ctx.chatBody.replaceChildren();
  ctx.toolCards.clear();
  ctx.streamPre = null; ctx.streamWrap = null;
  const results = new Map<string, string>();
  for (const m of messages) if (m["role"] === "tool" && typeof m["tool_call_id"] === "string") results.set(m["tool_call_id"] as string, String(m["content"] ?? ""));
  messages.forEach((m, idx) => {
    if (m["role"] === "user" && typeof m["content"] === "string") {
      const content = m["content"] as string;
      if (content.includes("<summary>") || content.startsWith("<env>") || content.startsWith("<system-reminder")) return;
      const { wrap, body } = turnWrap("user");
      body.textContent = content;
      wrap.appendChild(msgActions(ctx, idx, "user", content));
      ctx.chatBody.appendChild(wrap);
    } else if (m["role"] === "assistant") {
      const content = String(m["content"] ?? "");
      if (content.trim()) {
        const { wrap, body } = turnWrap("assistant");
        body.innerHTML = renderMarkdown(content);
        wrap.appendChild(msgActions(ctx, idx, "assistant", content));
        ctx.chatBody.appendChild(wrap);
      }
      for (const call of (m["tool_calls"] as Array<Record<string, unknown>>) ?? []) {
        const fn = call["function"] as Record<string, string>;
        let args: Record<string, unknown> = {};
        try { args = JSON.parse(fn["arguments"] ?? "{}"); } catch { args = {}; }
        const key = addToolLine(ctx, String(call["id"] ?? ""), fn["name"], args);
        const card = ctx.toolCards.get(key);
        if (card) { ctx.toolCards.delete(key); ctx.toolCards.set(String(call["id"] ?? key), card); }
        finishToolLine(ctx, String(call["id"] ?? ""), results.get(String(call["id"])) ?? "");
      }
    }
  });
  updateEmptyStateOnly(ctx);
  scrollBottom(ctx);
}

/* ---------------- edit mode ---------------- */
function showEditBar(): void { editBar.classList.remove("hidden"); promptZone.classList.add("editing"); }
function hideEditBar(): void { editBar.classList.add("hidden"); promptZone.classList.remove("editing"); }
function startEdit(ctx: Ctx, idx: number, text: string): void {
  if (ctx.busy) return;
  ctx.pendingRewind = idx;
  entry.value = text;
  autogrow(); updateSendState(); showEditBar(); entry.focus();
  setActivity("editing message - Enter resends (rewinds first), Esc or Cancel edit to cancel");
}
function cancelEdit(ctx: Ctx): void {
  if (ctx.pendingRewind === null) return;
  ctx.pendingRewind = null;
  entry.value = ""; autogrow(); updateSendState();
  if (ctx === activeCtx) hideEditBar();
  entry.focus();
  setActivity("edit cancelled");
}

/* ---------------- plan / usage / context panels ---------------- */
function renderPlan(ctx: Ctx): void {
  planBox.innerHTML = "";
  const done = ctx.planItems.filter((i) => i.status === "completed").length;
  planProgress.textContent = ctx.planItems.length ? `${done}/${ctx.planItems.length}` : "";
  if (!ctx.planItems.length) { planBox.innerHTML = `<div class="panel-empty">No plan yet. The agent uses write_todos to track steps.</div>`; return; }
  for (const item of ctx.planItems) {
    const row = document.createElement("div");
    row.className = `plan-item ${item.status}`;
    row.innerHTML = `<span class="plan-status ${item.status}">${PLAN_ICON[item.status] ?? ICONS.pending}</span><span class="plan-text">${escapeHtml(item.content)}</span>`;
    planBox.appendChild(row);
  }
}
function totalsSummary(ctx: Ctx): { items: Array<{ label: string; val: number }>; detail: string[]; total: number } {
  const num = (k: string): number => Number(ctx.totals[k]) || 0;
  const known = new Set(["prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens"]);
  const items: Array<{ label: string; val: number }> = [];
  if (num("prompt_tokens")) items.push({ label: "prompt", val: num("prompt_tokens") });
  if (num("completion_tokens")) items.push({ label: "completion", val: num("completion_tokens") });
  for (const [k, v] of Object.entries(ctx.totals)) { if (known.has(k) || !(Number(v) > 0)) continue; items.push({ label: k.replace(/_tokens$/, "").replace(/_/g, " "), val: Number(v) }); }
  const detail: string[] = [];
  if (num("reasoning_tokens")) detail.push(`${num("reasoning_tokens").toLocaleString()} reasoning`);
  if (num("cached_tokens")) detail.push(`${num("cached_tokens").toLocaleString()} cached`);
  return { items, detail, total: num("prompt_tokens") + num("completion_tokens") };
}
function compact(n: number): string { return n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }
function renderUsage(ctx: Ctx): void {
  const sum = totalsSummary(ctx);
  if (!sum.total) { usageBox.textContent = "no calls yet"; renderUsageChip(ctx); return; }
  const rows = sum.items.map((it) => `<div class="usage-row"><span class="u-label">${escapeHtml(it.label)}</span><span class="u-value">${it.val.toLocaleString()}</span></div>`);
  if (sum.detail.length) rows.push(`<div class="usage-detail">${sum.detail.join(" · ")}</div>`);
  rows.push(`<div class="usage-row total"><span class="u-label">total</span><span class="u-value">${sum.total.toLocaleString()}</span></div>`);
  usageBox.innerHTML = rows.join("");
  renderUsageChip(ctx);
}
function renderUsageChip(ctx: Ctx): void {
  if (ctx === activeCtx) usageChipText.textContent = compact(totalsSummary(ctx).total);
}
function renderContext(ctx: Ctx): void {
  const d = ctx.gitInfo;
  const branch = d.branch || "";
  let chip: string;
  if (!d.has_git) chip = `<span class="branch-chip nogit"><span class="b-dot"></span>No Git</span>`;
  else if (d.is_protected) chip = `<span class="branch-chip protected" title="Default/protected branch">${ICONS.branch}${escapeHtml(branch || "(detached)")}</span>`;
  else chip = `<span class="branch-chip"><span class="b-dot"></span>${escapeHtml(branch || "(detached)")}</span>`;
  contextBox.innerHTML =
    `<div class="ctx-row"><span class="ctx-label">Workspace</span><span class="ctx-value" title="${escapeHtml(ctx.root)}">${escapeHtml(ctx.project)}</span></div>` +
    `<div class="ctx-row"><span class="ctx-label">Branch</span>${chip}</div>` +
    `<div class="ctx-path" title="${escapeHtml(ctx.root)}">${escapeHtml(ctx.root)}</div>`;
}
function buildUsagePop(): string {
  if (!activeCtx) return "";
  const ctx = activeCtx;
  const sum = totalsSummary(ctx);
  const rows = sum.items.map((it) => `<div class="pop-row"><span>${escapeHtml(it.label)}</span><b>${it.val.toLocaleString()}</b></div>`).join("");
  const detail = sum.detail.length ? `<div class="pop-detail">${sum.detail.join(" · ")}</div>` : "";
  const done = ctx.planItems.filter((i) => i.status === "completed").length;
  const planHtml = ctx.planItems.length
    ? `<div class="pop-plan-head">Plan ${done}/${ctx.planItems.length}</div><ul class="pop-plan">${ctx.planItems.map((i) => `<li class="${i.status}"><span class="pop-ic">${PLAN_ICON[i.status] ?? ""}</span>${escapeHtml(i.content)}</li>`).join("")}</ul>`
    : `<div class="pop-plan-head">Plan</div><div class="pop-empty">No plan yet.</div>`;
  const branch = !ctx.gitInfo.has_git ? "no git" : ctx.gitInfo.is_protected ? `${ctx.gitInfo.branch} · protected` : ctx.gitInfo.branch || "(detached)";
  return `<div class="pop-head">Usage & context</div>` +
    (sum.total ? `${rows}${detail}<div class="pop-row total"><span>total</span><b>${sum.total.toLocaleString()}</b></div>` : `<div class="pop-empty">No calls yet.</div>`) +
    `<div class="pop-sep"></div><div class="pop-ctx">${escapeHtml(ctx.project || "-")} <span class="pop-sep-in">·</span> ${escapeHtml(branch)}</div>` +
    `<div class="pop-sep"></div>${planHtml}`;
}
function showUsagePop(): void {
  if (popHideTimer) { clearTimeout(popHideTimer); popHideTimer = null; }
  usagePop.innerHTML = buildUsagePop();
  usagePop.classList.remove("hidden");
  const r = usageChip.getBoundingClientRect();
  usagePop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 300))}px`;
  usagePop.style.bottom = `${window.innerHeight - r.top + 8}px`;
}
function scheduleHidePop(): void { if (popHideTimer) clearTimeout(popHideTimer); popHideTimer = window.setTimeout(() => usagePop.classList.add("hidden"), 180); }

/* ---------------- tab bar ---------------- */
function renderTabs(): void {
  tabsBox.replaceChildren();
  for (const t of tabs) {
    const el0 = document.createElement("div");
    el0.className = "tab" + (t === activeCtx ? " active" : "");
    el0.setAttribute("role", "tab");
    el0.setAttribute("aria-selected", String(t === activeCtx));
    el0.innerHTML =
      `<span class="tab-glyph">${ICONS.chat}</span>` +
      `<span class="tab-label"><span class="tab-ws">${escapeHtml(t.project)}</span><span class="tab-title"><span class="mq">${escapeHtml(displayTitle(t.sessionTitle))}</span></span></span>` +
      `<button class="tab-close" title="Close tab">${ICONS.close}</button>`;
    attachMarquee(el0.querySelector<HTMLElement>(".tab-title"));
    el0.onclick = (e) => { if ((e.target as HTMLElement).closest(".tab-close")) return; activateTab(t.id); };
    (el0.querySelector(".tab-close") as HTMLButtonElement).onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
    tabsBox.appendChild(el0);
  }
}
function renderActive(): void {
  if (!activeCtx) return;
  const ctx = activeCtx;
  chat.replaceChildren(ctx.chatBody);
  entry.value = ctx.entryDraft;
  autogrow();
  updateEmptyState();
  scrollBottom(ctx);
  renderPlan(ctx); renderUsage(ctx); renderContext(ctx);
  modeLabel.textContent = ctx.modeVal === "build" ? "Build" : "Plan";
  statusMode.textContent = ctx.modeVal === "build" ? "Build" : "Plan";
  effortLabel.textContent = ctx.effortVal;
  permsLabel.textContent = PERMS.find((p) => p.value === ctx.permsVal)?.label ?? ctx.permsVal;
  const cur = providerOptions.find((p) => p.id === ctx.providerId);
  providerLabel.textContent = cur ? cur.label : "No providers";
  providerBtn.classList.toggle("no-provider", !cur);
  renderMenu(modeMenu, [{ label: "Build", value: "build" }, { label: "Plan", value: "plan" }], ctx.modeVal, (v) => pickMode(ctx, v));
  renderMenu(effortMenu, EFFORTS.map((e) => ({ label: e, value: e })), ctx.effortVal, (v) => pickEffort(ctx, v));
  renderMenu(permsMenu, PERMS.map((p) => ({ label: p.label, value: p.value })), ctx.permsVal, (v) => pickPerms(ctx, v));
  refreshProviderMenu();
  setStatusState(ctx.busy ? "working" : "ready");
  if (connected) setConn(`connected · ${ctx.project || baseName(ctx.root)}`, "online");
  else setConn(`connecting… (port ${bootPort})`, "connecting");
  if (ctx.pendingRewind !== null) showEditBar(); else hideEditBar();
  updateSendState();
  renderTabs();
  renderTree();
  renderLanding();
}
function activateTab(id: string): void {
  const target = tabs.find((t) => t.id === id);
  if (!target || target === activeCtx) return;
  if (activeCtx) activeCtx.entryDraft = entry.value;
  activeCtx = target;
  renderActive();
}
function newSessionTab(root?: string): void {
  const target = root ?? activeCtx?.root ?? bootRoot;
  if (!target) return;
  openTab(target, "new", null);
}
function issueOpen(ctx: Ctx): void {
  if (ctx.hadReady) return;
  ctx.hadReady = true;
  const intent = ctx.openIntent;
  const openId = ctx.openId;
  ctx.openIntent = "resume";
  ctx.openId = null;
  applyWorkspacePrefs(ctx);
  if (intent === "new") { ctx.draft = true; primeContext(ctx); return; }
  if (intent === "session") { send(ctx, { op: "open", id: openId ?? "", mode: ctx.modeVal }); return; }
  // resume: use the remembered session, else fall back to the workspace's most
  // recent session once the list arrives (never a fresh blank if history exists).
  const last = prefs.lastSessionByWs[ctx.root];
  if (last) { send(ctx, { op: "open", id: last, mode: ctx.modeVal }); return; }
  ctx.pendingResume = true;
}
function openOrFocus(root: string, sessionId: string): void {
  const existing = tabs.find((t) => t.root === root && t.sessionId === sessionId);
  if (existing) { activateTab(existing.id); collapsedWs.delete(root); renderTree(); return; }
  openTab(root, "session", sessionId);
}
function switchToWorkspace(root: string): void {
  if (activeCtx && activeCtx.root === root) { toggleCollapse(root); return; }
  const existing = tabs.filter((t) => t.root === root);
  if (existing.length) { activateTab(existing[existing.length - 1].id); collapsedWs.delete(root); renderTree(); return; }
  openTab(root, "resume", null);
}
function openTab(root: string, intent: Ctx["openIntent"], openId: string | null): void {
  if (intent === "session" && openId) { const ex = tabs.find((t) => t.root === root && t.sessionId === openId); if (ex) { activateTab(ex.id); return; } }
  if (activeCtx) setActivity(`opening ${baseName(root)}…`);
  const ctx = makeCtx(root, baseName(root), intent, openId);
  tabs.push(ctx);
  collapsedWs.delete(root);
  renderTabs();
  activateTab(ctx.id);
  if (connected) issueOpen(ctx);
  else void fetchRecents();
}
function closeTab(id: string): void {
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  const ctx = tabs[idx];
  if (activeCtx === ctx) activeCtx.entryDraft = entry.value;
  ctxById.delete(id);
  tabs.splice(idx, 1);
  if (tabs.length === 0) { activeCtx = null; renderTabs(); renderLanding(); return; }
  if (activeCtx === ctx) activateTab((tabs[idx] ?? tabs[idx - 1] ?? tabs[0]).id);
  else renderTabs();
}

/* When no tab is open, show a full landing in the center instead of a session. */
function renderLanding(): void {
  const none = !activeCtx;
  landing.classList.toggle("hidden", !none);
  statusBar.classList.toggle("hidden", none);
  promptZone.classList.toggle("hidden", none);
  if (none) {
    chat.replaceChildren();
    emptyState.classList.add("hidden");
    usagePop.classList.add("hidden");
  }
}
async function fetchRecents(): Promise<void> {
  try {
    const r = await window.harness?.recentWorkspaces();
    if (Array.isArray(r)) recents = r.map((x) => ({ root: String(x.root), name: String(x.name) || baseName(String(x.root)) }));
  } catch {
    recents = recents.filter((x) => x.root !== activeCtx?.root);
  }
  renderTree();
  loadAllWorkspaceSessions();
}

/* Load every recent workspace's session list so the sidebar shows them all. */
function loadAllWorkspaceSessions(): void {
  if (!activeCtx || !connected) return;
  for (const ws of recents) {
    if (sessionsByRoot[ws.root]) continue;
    send(activeCtx, { op: "ws_sessions", root: ws.root });
  }
}

/* ---------------- sessions library (left sidebar) ---------------- */
function ctxForRoot(root: string): Ctx | null {
  if (activeCtx && activeCtx.root === root) return activeCtx;
  return tabs.find((t) => t.root === root) ?? null;
}
function sessionRow(s: SessionListItem, root: string): HTMLElement {
  const row = document.createElement("div");
  const isCur = !!activeCtx && activeCtx.root === root && activeCtx.sessionId === s.id;
  row.className = "session-item" + (isCur ? " active" : "");
  const t = relTime(s.id);
  row.innerHTML =
    `<span class="s-glyph">${ICONS.chat}</span>` +
    `<span class="s-title"><span class="mq">${escapeHtml(displayTitle(s.title))}</span></span>` +
    `<span class="s-time">${t}</span>`;
  attachMarquee(row.querySelector<HTMLElement>(".s-title"));
  row.onclick = () => openOrFocus(root, s.id);
  row.oncontextmenu = (e) => { e.preventDefault(); lastMenuX = e.clientX; lastMenuY = e.clientY; showSessionMenu(root, s); };
  return row;
}
function newTreeSessionRow(root: string): HTMLElement {
  const row = document.createElement("div");
  row.className = "tree-new";
  row.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>New session</span>`;
  row.onclick = () => newSessionTab(root);
  return row;
}
function branchMetaHtml(ctx: Ctx): string {
  const d = ctx.gitInfo;
  if (!d.has_git) return `<span class="ws-meta"><span style="width:6px;height:6px;border-radius:50%;background:var(--text-3);display:inline-block"></span>no git</span>`;
  const dot = d.is_protected ? "var(--warn)" : "var(--accent)";
  return `<span class="ws-meta"><span style="width:6px;height:6px;border-radius:50%;background:${dot};display:inline-block"></span>${escapeHtml(d.branch || "(detached)")}</span>`;
}
function toggleCollapse(root: string): void { if (collapsedWs.has(root)) collapsedWs.delete(root); else collapsedWs.add(root); renderTree(); }
function renderTree(): void {
  wsCount.textContent = recents.length ? String(recents.length) : "";
  treeBox.replaceChildren();
  if (!recents.length) { treeBox.innerHTML = `<div class="tree-empty">No workspaces yet.<br>Open a folder to begin.</div>`; return; }
  for (const ws of recents) {
    const isActive = !!activeCtx && activeCtx.root === ws.root;
    const hasTab = tabs.some((t) => t.root === ws.root);
    const list = sessionsByRoot[ws.root] ?? [];
    const open = !collapsedWs.has(ws.root);
    const row = document.createElement("div");
    row.className = "ws-row" + (isActive ? " active" : "") + (open ? " open" : "");
    row.title = ws.root;
    row.innerHTML =
      `<span class="ws-glyph">${ICONS.folder}</span>` +
      `<span class="ws-name"><span class="mq">${escapeHtml(ws.name)}</span></span>` +
      (isActive ? branchMetaHtml(activeCtx as Ctx) : `<span class="ws-meta">${hasTab ? `<span style="width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block"></span>` : ""}${list.length ? `${list.length}` : ""}</span>`) +
      `<span class="ws-act">${ICONS.arrow}</span>`;
    attachMarquee(row.querySelector<HTMLElement>(".ws-name"));
    row.onclick = () => switchToWorkspace(ws.root);
    treeBox.appendChild(row);
    const kids = document.createElement("div");
    kids.className = "ws-kids" + (open ? "" : " hidden");
    for (const s of list) kids.appendChild(sessionRow(s, ws.root));
    kids.appendChild(newTreeSessionRow(ws.root));
    treeBox.appendChild(kids);
  }
}
function showSessionMenu(root: string, item: SessionListItem): void {
  const ctx = ctxForRoot(root);
  contextMenu.innerHTML =
    `<button data-act="rename">${ICONS.edit}<span>Rename</span></button>` +
    `<div class="ctx-sep"></div>` +
    `<button data-act="delete" class="danger">${ICONS.trash}<span>Delete</span></button>`;
  contextMenu.classList.remove("hidden");
  const rect = contextMenu.getBoundingClientRect();
  const px = (contextMenu.style.left = `${Math.max(8, Math.min(lastMenuX, window.innerWidth - rect.width - 8))}px`);
  contextMenu.style.top = `${Math.max(8, Math.min(lastMenuY, window.innerHeight - rect.height - 8))}px`;
  contextMenu.querySelectorAll("button").forEach((b) => {
    (b as HTMLButtonElement).onclick = () => {
      const act = (b as HTMLElement).dataset["act"];
      hideContextMenu();
      if (act === "rename") { if (ctx) openRename(ctx, item); else setActivity("open this workspace to rename its sessions"); }
      else if (act === "delete") { if (ctx) openDelete(ctx, item); else setActivity("open this workspace to edit its sessions"); }
    };
  });
  void px;
}
let lastMenuX = 0;
let lastMenuY = 0;
function hideContextMenu(): void { contextMenu.classList.add("hidden"); }

/* ---------------- session lifecycle helpers ---------------- */
function refreshSessionsCache(ctx: Ctx, list: SessionListItem[]): void {
  sessionsByRoot[ctx.root] = list;
  for (const t of tabs) {
    if (t.root !== ctx.root || !t.sessionId) continue;
    const match = list.find((s) => s.id === t.sessionId);
    if (match && match.title) t.sessionTitle = match.title;
  }
  renderTree();
  renderTabs();
}
function resumeIfNeeded(ctx: Ctx, list: SessionListItem[]): void {
  if (ctx.busy) return;
  const ids = new Set(list.map((s) => s.id));
  if (ctx.pendingOpenSessionId) {
    const openId = ctx.pendingOpenSessionId;
    ctx.pendingOpenSessionId = null;
    if (ids.has(openId)) { collapsedWs.delete(ctx.root); send(ctx, { op: "open", id: openId, mode: ctx.modeVal }); return; }
  }
  if (ctx.pendingResume) {
    ctx.pendingResume = false;
    if (ctx.sessionId) return;
    const newest = list[0]?.id;
    send(ctx, { op: "open", id: newest ?? "", mode: ctx.modeVal });
  }
}
function applyWorkspacePrefs(ctx: Ctx): void {
  ctx.effortVal = prefs.effortByWs[ctx.root] ?? prefs.effortGlobal;
  ctx.pendingEffort = ctx.effortVal;
  ctx.pendingProviderId = prefs.providerByWs[ctx.root] ?? null;
}
function clearWorkspaceUI(ctx: Ctx): void {
  hideThinking(ctx);
  ctx.chatBody.replaceChildren();
  ctx.toolCards.clear();
  ctx.streamPre = null; ctx.streamWrap = null;
  ctx.sessionId = null; ctx.sessionTitle = "New Session";
  ctx.totals = {}; ctx.planItems = [];
  ctx.pendingRewind = null; ctx.pendingText = null; ctx.pendingOpenSessionId = null;
  updateEmptyStateOnly(ctx);
  if (ctx === activeCtx) {
    renderPlan(ctx); renderUsage(ctx);
    hideEditBar(); entry.value = ""; updateSendState();
  }
}

/* ---------------- modal ---------------- */
function showModal(html: string): void {
  modalBox.classList.remove("wide");
  modalBox.classList.remove("settings-modal");
  modalBox.style.width = "";
  modalBox.style.maxWidth = "";
  modalBox.style.maxHeight = "";
  modalBox.style.height = "";
  modalBox.innerHTML = html;
  modal.classList.remove("hidden");
}
function hideModal(): void {
  activeApproval = null;
  modal.classList.add("hidden");
  modalBox.innerHTML = "";
}
function openRename(ctx: Ctx, item: SessionListItem): void {
  showModal(
    `<h3>Rename session</h3><div class="field"><label>Title</label><input id="renameInput" maxlength="60" value="${escapeHtml(item.title)}" /></div>` +
      `<div class="modal-actions"><button class="btn" id="renameCancel">Cancel</button><button class="btn primary" id="renameSave">Save</button></div>`,
  );
  const input = el<HTMLInputElement>("renameInput");
  input.focus(); input.select();
  const save = (): void => { const v = input.value.trim(); if (v) { send(ctx, { op: "rename_session", id: item.id, title: v.slice(0, 60) }); if (ctx === activeCtx) setActivity("renamed"); } hideModal(); };
  el<HTMLButtonElement>("renameSave").onclick = save;
  el<HTMLButtonElement>("renameCancel").onclick = hideModal;
  input.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") save(); };
}
function openDelete(ctx: Ctx, item: SessionListItem): void {
  showModal(
    `<h3>Delete session</h3><p>Delete <b>${escapeHtml(item.title)}</b>? This cannot be undone.</p>` +
      `<div class="modal-actions"><button class="btn" id="delCancel">Cancel</button><button class="btn danger" id="delOk">Delete</button></div>`,
  );
  el<HTMLButtonElement>("delOk").onclick = () => { send(ctx, { op: "delete_session", id: item.id }); hideModal(); };
  el<HTMLButtonElement>("delCancel").onclick = hideModal;
}
function askPick(ctx: Ctx, title: string, rows: string[], id: string): void {
  if (!rows.length) { send(ctx, { op: "pick_reply", id, index: null }); return; }
  showModal(
    `<h3>${escapeHtml(title || "Choose one")}</h3>` +
      `<div class="pick-list">${rows.map((r, i) => `<button class="pick-item" data-i="${i}">${escapeHtml(r)}</button>`).join("")}</div>` +
      `<div class="modal-actions"><button class="btn" id="mCancel">Cancel</button></div>`,
  );
  modalBox.querySelectorAll<HTMLButtonElement>(".pick-item").forEach((btn) => { btn.onclick = () => { send(ctx, { op: "pick_reply", id, index: Number(btn.dataset["i"]) }); hideModal(); }; });
  el<HTMLButtonElement>("mCancel").onclick = () => { send(ctx, { op: "pick_reply", id, index: null }); hideModal(); };
}

/* ---------------- approval (keyboard-first) ---------------- */
const RISK_CLASS: Record<string, string> = { low: "risk-low", medium: "risk-medium", high: "risk-high", critical: "risk-critical" };
function riskBadge(level: string): string { return `<span class="risk-badge ${RISK_CLASS[level] ?? "risk-medium"}">${escapeHtml((level || "?").toUpperCase())}</span>`; }
function askApproval(ctx: Ctx, id: string, reason: string, tool = "", command = "", target = "", why = "", risk = "", riskReason = ""): void {
  const rows: string[] = [];
  if (command) rows.push(`<div class="perm-label">Command</div><pre class="perm-cmd">${escapeHtml(command)}</pre>`);
  else if (target) rows.push(`<div class="perm-label">${escapeHtml(tool || "File")}</div><div class="perm-target">${escapeHtml(target)}</div>`);
  rows.push(`<div class="perm-label">Why</div><div class="perm-why">${escapeHtml(why || reason)}</div>`);
  rows.push(`<div class="perm-risk"><span class="perm-label">Risk</span>${riskBadge(risk)}<span class="perm-risk-reason">${escapeHtml(riskReason || "")}</span></div>`);
  showModal(
    `<h3>Permission required</h3>${rows.join("")}` +
      `<div class="perm-hint">Enter to allow · Esc to deny</div>` +
      `<div class="modal-actions"><button class="btn danger" id="mDeny">Deny</button><button class="btn primary" id="mAllow">Allow</button></div>`,
  );
  const allow = el<HTMLButtonElement>("mAllow");
  allow.onclick = () => resolveApproval(ctx, id, true);
  el<HTMLButtonElement>("mDeny").onclick = () => resolveApproval(ctx, id, false);
  activeApproval = { ctx, id };
  setTimeout(() => allow.focus(), 0);
}
function resolveApproval(ctx: Ctx, id: string, allow: boolean): void {
  activeApproval = null;
  send(ctx, { op: "approval_reply", id, allow });
  hideModal();
}

/* ---------------- rewind (conversation + files) ---------------- */
function confirmRewind(ctx: Ctx, idx: number): void {
  if (ctx.busy) return;
  pendingRewindCount = idx;
  send(ctx, { op: "rewind_preview", count: idx });
}
function openRewindConfirm(count: number, changes: Array<{ path: string; action: string; dirty: boolean }>): void {
  pendingRewindCount = count;
  const dirty = changes.some((c) => c.dirty);
  const list = changes.length
    ? `<ul class="rewind-list">${changes.map((c) => `<li><span class="rw-path" title="${escapeHtml(c.path)}">${escapeHtml(baseName(c.path))}</span><span class="rw-act ${c.action === "delete" ? "del" : "res"}">${c.action === "delete" ? "will be deleted" : "restored to pre-edit"}</span>${c.dirty ? `<span class="rw-dirty">edited since</span>` : ""}</li>`).join("")}</ul>`
    : `<p class="perm-why">This rewinds the conversation only. No project files were changed after this point, so nothing on disk is affected.</p>`;
  showModal(
    `<h3>Rewind to this point?</h3>` +
      `<p class="perm-why">The conversation returns to before this message${changes.length ? ` and <b>${changes.length}</b> file(s) the agent changed are restored` : ""}.</p>${list}` +
      (dirty ? `<div class="rewind-warn">Some of these files were edited after the agent changed them. Restoring will overwrite those manual edits.</div>` : "") +
      `<div class="rewind-note">Rewind restores files edited with write/str_replace; it cannot revert arbitrary shell changes.</div>` +
      `<div class="modal-actions"><button class="btn" id="rwCancel">Cancel</button><button class="btn primary" id="rwOk">Rewind</button></div>`,
  );
  el<HTMLButtonElement>("rwCancel").onclick = () => { pendingRewindCount = null; hideModal(); };
  el<HTMLButtonElement>("rwOk").onclick = () => {
    if (!activeCtx) return;
    const c = pendingRewindCount ?? count; pendingRewindCount = null; hideModal();
    send(activeCtx, { op: "rewind_to", count: c });
  };
}

/* ---------------- branch guard ---------------- */
function askBranchConfirm(ctx: Ctx, text: string): void {
  ctx.pendingText = text;
  const def = ctx.gitInfo.default_branch || "main";
  showModal(
    `<h3>Direct edit on ${escapeHtml(ctx.gitInfo.branch)}?</h3><p>You are on <b>${escapeHtml(ctx.gitInfo.branch)}</b>, the default branch${def !== ctx.gitInfo.branch ? ` (<b>${escapeHtml(def)}</b>)` : ""}. Edits here go straight onto it.</p>` +
      `<div class="modal-actions"><button class="btn" id="bCancel">Cancel</button><button class="btn" id="bBranch">New branch…</button><button class="btn primary" id="bDirect">Edit directly</button></div>`,
  );
  el<HTMLButtonElement>("bDirect").onclick = () => { ctx.branchAck = ctx.gitInfo.branch; const t = ctx.pendingText; ctx.pendingText = null; hideModal(); if (t !== null) doSubmit(ctx, t); };
  el<HTMLButtonElement>("bBranch").onclick = () => askBranchName(ctx);
  el<HTMLButtonElement>("bCancel").onclick = () => { ctx.pendingText = null; hideModal(); setActivity("cancelled - not on a safe branch"); };
}
function askBranchName(ctx: Ctx): void {
  const suggestion = `harness/${new Date().toISOString().slice(0, 10)}`;
  showModal(
    `<h3>Create a new branch</h3><div class="field"><label>Branch name</label><input id="branchName" value="${escapeHtml(suggestion)}" /></div>` +
      `<div class="modal-actions"><button class="btn" id="bnCancel">Cancel</button><button class="btn primary" id="bnCreate">Create &amp; send</button></div>`,
  );
  const input = el<HTMLInputElement>("branchName"); input.focus(); input.select();
  const create = (): void => { const name = input.value.trim(); if (!name) return; hideModal(); send(ctx, { op: "git_create_branch", name }); };
  el<HTMLButtonElement>("bnCreate").onclick = create;
  el<HTMLButtonElement>("bnCancel").onclick = () => { ctx.pendingText = null; hideModal(); };
  input.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter") create(); };
}

/* ---------------- compose + submit ---------------- */
function autogrow(): void {
  entry.style.height = "auto";
  const cap = promptZone.classList.contains("expanded") ? Math.floor(window.innerHeight * 0.6) : 200;
  entry.style.height = `${Math.min(entry.scrollHeight, cap)}px`;
}
function doSubmit(ctx: Ctx, text: string): void {
  entry.value = ""; ctx.pendingRewind = null;
  // A new turn supersedes any retry card or failure card from the last one.
  clearRetryCard(ctx); ctx.failure = null;
  hideEditBar(); autogrow(); updateSendState(); setBusy(ctx, true); showThinking(ctx);
  send(ctx, { op: "submit", text, mode: ctx.modeVal });
}
function submit(ctx: Ctx, text: string): void {
  const trimmed = text.trim();
  if (activeApproval) return;
  if (!trimmed || ctx.busy) return;
  if (!connected) { setActivity("not connected - waiting for reconnect…"); return; }
  if (ctx.pendingRewind !== null) {
    const idx = ctx.pendingRewind; ctx.pendingRewind = null; entry.value = ""; autogrow(); hideEditBar();
    if (!send(ctx, { op: "resend", count: idx, text: trimmed, mode: ctx.modeVal })) setActivity("resend failed - engine busy");
    return;
  }
  if (prefs.confirmProtected && ctx.gitInfo.is_protected && ctx.branchAck !== ctx.gitInfo.branch) { askBranchConfirm(ctx, trimmed); return; }
  doSubmit(ctx, trimmed);
}

/* ---------------- connection (single backend, all tabs share it) ---------------- */
function connect(): void {
  wsGen += 1;
  const gen = wsGen;
  setConn(`connecting… (port ${bootPort})`, "connecting");
  dlog("ws", "WS-CONNECT", `ws://127.0.0.1:${bootPort}`, "gen", gen);
  const sock = new WebSocket(`ws://127.0.0.1:${bootPort}`);
  sharedWs = sock;
  sock.onopen = () => {
    if (gen !== wsGen) return;
    connected = true;
    dlog("ws", "WS-OPEN");
    if (activeCtx) setConn(`connected · ${activeCtx.project || baseName(activeCtx.root)}`, "online");
  };
  sock.onmessage = (e) => {
    try {
      dispatchFrame(JSON.parse(String(e.data)) as ServerEnvelope);
    } catch (err) {
      dlog("ws", "WS-BAD-FRAME:", String(err));
    }
  };
  sock.onclose = () => {
    connected = false;
    if (gen !== wsGen) return;
    dlog("ws", "WS-CLOSE");
    if (activeCtx) { setConn("disconnected - retrying…", "offline"); setBusy(activeCtx, false); }
    setTimeout(() => { if (gen === wsGen) connect(); }, 1500);
  };
  sock.onerror = () => { try { sock.close(); } catch { /* ignore */ } };
}
function dispatchFrame(msg: ServerEnvelope): void {
  if (msg.ready) { handleReady(msg); return; }
  const ctx = msg.session ? ctxById.get(msg.session) : null;
  if (ctx) handleEnvelope(msg, ctx);
}
function handleReady(msg: ServerEnvelope): void {
  connected = true;
  const root = String(msg.ready?.project_root ?? bootRoot);
  if (root) bootRoot = root;
  void fetchRecents();
  if (activeCtx) {
    setConn(`connected · ${activeCtx.project || baseName(activeCtx.root)}`, "online");
    if (!activeCtx.hadReady) issueOpen(activeCtx);
    primeContext(activeCtx);
  }
}

/* Prime a tab's data: sessions list, providers, and workspace/git context. */
function primeContext(ctx: Ctx): void {
  send(ctx, { op: "list_sessions" });
  send(ctx, { op: "list_providers" });
  send(ctx, { op: "get_context" });
}

/* ---------------- envelope routing (to a tab's Session) ---------------- */
function handleEnvelope(msg: ServerEnvelope, ctx: Ctx): void {
  if (msg.request === "approval" && msg.id) { dlog(ctx.id, "RECV approval", msg.id); askApproval(ctx, msg.id, String(msg.reason ?? ""), String(msg.tool ?? ""), String(msg.command ?? ""), String(msg.target ?? ""), String(msg.why ?? ""), String(msg.risk ?? ""), String(msg.risk_reason ?? "")); return; }
  if (msg.request === "pick" && msg.id) { dlog(ctx.id, "RECV pick", msg.id); askPick(ctx, String(msg.title ?? ""), (msg.rows as string[]) ?? [], msg.id); return; }

  if (msg.event) {
    const p = (msg.payload ?? {}) as Record<string, unknown>;
    switch (msg.event) {
      case "message":
        hideThinking(ctx);
        if (p["role"] === "user") addUser(ctx, String(p["content"] ?? ""));
        else endStream(ctx, String(p["content"] ?? ""));
        if (ctx === activeCtx) setActivity("ready");
        break;
      case "message_delta": appendDelta(ctx, String(p["text"] ?? "")); break;
      case "tool_call": {
        const args = (p["args"] as Record<string, unknown>) ?? {};
        if (p["name"] === "write_todos" && Array.isArray(args["todos"])) { ctx.planItems = args["todos"] as TodoItem[]; if (ctx === activeCtx) renderPlan(ctx); }
        else { if (ctx === activeCtx) setActivity(`running ${p["name"]}: ${String(args["command"] ?? args["path"] ?? "")}`.slice(0, 120)); addToolLine(ctx, (p["call_id"] as string) ?? null, String(p["name"]), args); }
        break;
      }
      case "tool_result":
        if (p["name"] !== "write_todos") finishToolLine(ctx, (p["call_id"] as string) ?? null, String(p["result"] ?? ""));
        if (ctx === activeCtx) setActivity("thinking…");
        break;
      case "todos": ctx.planItems = (p["todos"] as TodoItem[]) ?? []; if (ctx === activeCtx) renderPlan(ctx); break;
      case "usage": for (const [k, v] of Object.entries(p)) ctx.totals[k] = (ctx.totals[k] ?? 0) + (Number(v) || 0); if (ctx === activeCtx) renderUsage(ctx); break;
      case "status": {
        const label = p["active"] as string | null;
        if (label) { if (ctx === activeCtx) { setActivity(label); setStatusState("working"); } showThinking(ctx); }
        else { if (ctx === activeCtx && !ctx.busy) setStatusState("ready"); hideThinking(ctx); }
        break;
      }
      case "note": addNote(ctx, String(p["text"] ?? "")); break;
      case "retry": showRetry(ctx, p); break;
      case "error":
        clearRetryCard(ctx);
        addError(ctx, String(p["text"] ?? ""), p);
        if (ctx === activeCtx) setActivity(String(p["model"] ? `${p["model"]} failed` : "error - see above"));
        break;
      case "session": {
        const id = (p["id"] as string) ?? null;
        ctx.sessionId = id;
        if (id) prefs.lastSessionByWs[ctx.root] = id, savePrefs();
        ctx.sessionTitle = "New Session";
        renderTranscript(ctx, (p["messages"] as Array<Record<string, unknown>>) ?? []);
        primeContext(ctx);
        if (ctx === activeCtx) setActivity(String(p["label"] ?? ""));
        break;
      }
      case "session_title": {
        const title = String(p["title"] ?? "");
        if (title) { ctx.sessionTitle = title; if (ctx === activeCtx) renderTabs(); }
        break;
      }
      case "compacted": renderTranscript(ctx, (p["messages"] as Array<Record<string, unknown>>) ?? []); addNote(ctx, `compacted ${p["before"]} to ${((p["messages"] as unknown[]) ?? []).length} messages`); break;
      case "subagent": if (ctx === activeCtx) setActivity("subagent exploring…"); addNote(ctx, "subagent: " + String(p["description"] ?? "")); break;
      case "done": setBusy(ctx, false); if (ctx === activeCtx) setActivity("ready"); send(ctx, { op: "list_sessions" }); send(ctx, { op: "get_context" }); if (ctx.sessionId) { const sid = ctx.sessionId; send(ctx, { op: "get_transcript", id: sid }); } break;
      case "sessions_updated": send(ctx, { op: "list_sessions" }); break;
    }
    return;
  }

  if (msg.reply === "list_sessions" && Array.isArray(msg.data)) {
    const list = msg.data as SessionListItem[];
    refreshSessionsCache(ctx, list);
    resumeIfNeeded(ctx, list);
  } else if (msg.reply === "ws_sessions" && msg.data && typeof msg.data === "object") {
    const d = msg.data as { root: string; sessions: SessionListItem[] };
    if (d.root) { sessionsByRoot[d.root] = d.sessions ?? []; renderTree(); }
  } else if (msg.reply === "list_providers" && msg.data && typeof msg.data === "object") {
    const data = msg.data as ProviderState;
    providerGroups = data.groups ?? [];
    providerModels = data.providers ?? [];
    providerStatuses = data.statuses ?? {};
    providerActiveModelId = data.active_model_id ?? null;
    providerActiveGroupId = data.active_provider_id ?? null;
    providerMigration = data.migration ?? [];
    providerById.clear();
    for (const model of providerModels) providerById.set(model.id, model);
    providerOptions = providerModels
      .filter((p) => !p.disabled)
      .map((p) => ({ id: p.id, label: p.name || p.model, sub: p.provider_name }));
    const pick = (id: string | null): string | null =>
      id && providerOptions.some((p) => p.id === id) ? id : null;
    ctx.providerId = pick(ctx.pendingProviderId) ?? pick(providerActiveModelId) ?? providerOptions[0]?.id ?? null;
    ctx.pendingProviderId = null;
    if (ctx.pendingEffort !== null) { const eff = ctx.pendingEffort; ctx.pendingEffort = null; if (eff !== ctx.effortVal) ctx.effortVal = eff; }
    syncEffortFromModel(ctx);
    if (ctx === activeCtx) {
      const cur = providerOptions.find((p) => p.id === ctx.providerId);
      providerLabel.textContent = cur ? cur.label : "No providers";
      providerLabel.title = cur ? `${cur.label} · ${cur.sub}` : "No providers";
      providerBtn.classList.toggle("no-provider", !cur);
      effortLabel.textContent = ctx.effortVal;
      refreshProviderMenu();
      renderTabs();
    }
    if (settingsRequested) { settingsRequested = false; openSettings(settingsLastTab); }
  } else if (msg.reply === "set_active" || msg.reply === "delete_provider" || msg.reply === "save_provider") {
    if (msg.ok) {
      if (msg.reply === "save_provider" && msg.data && typeof msg.data === "object") {
        const saved = msg.data as ProviderEntry;
        ctx.providerId = saved.id;
        prefs.providerByWs[ctx.root] = saved.id;
        savePrefs();
        syncEffortFromModel(ctx);
      }
      send(ctx, { op: "list_providers" });
    } else if (ctx === activeCtx) setActivity(`provider change failed: ${String(msg.error ?? "unknown")}`);
  } else if (msg.reply === "retry") {
    if (msg.ok) { setBusy(ctx, true); showThinking(ctx); }
    else if (ctx === activeCtx) setActivity("retry failed - engine busy");
  } else if (msg.reply === "git_create_branch") {
    if (msg.ok) { send(ctx, { op: "get_context" }); if (ctx.pendingText !== null) { const t = ctx.pendingText; ctx.pendingText = null; doSubmit(ctx, t); } }
    else { ctx.pendingText = null; if (ctx === activeCtx) setActivity(`branch creation failed: ${String(msg.error ?? "unknown")}`); }
  } else if (msg.reply === "rewind_to") {
    if (!msg.ok) { if (ctx === activeCtx) setActivity(`rewind failed: ${String(msg.error ?? "unknown")}`); }
    else { const changed = Array.isArray(msg.data) ? (msg.data as string[]) : []; if (ctx === activeCtx) setActivity(changed.length ? `rewound · restored ${changed.length} file(s)` : "rewound"); send(ctx, { op: "get_transcript", id: ctx.sessionId ?? "" }); }
  } else if (msg.reply === "rewind_preview") {
    const data = (msg.data ?? {}) as { count?: number; changes?: Array<{ path: string; action: string; dirty: boolean }> };
    openRewindConfirm(data.count ?? 0, data.changes ?? []);
  } else if (msg.reply === "get_transcript" && msg.ok && Array.isArray(msg.data)) {
    renderTranscript(ctx, msg.data as Array<Record<string, unknown>>);
  } else if (msg.reply === "resend") {
    if (msg.ok) { setBusy(ctx, true); showThinking(ctx); } else { setBusy(ctx, false); if (ctx === activeCtx) setActivity("resend failed - engine busy"); }
  } else if (msg.reply === "test_provider") {
    const status = document.getElementById("setStatus");
    if (status) { status.textContent = `${msg.ok ? "PASS: " : "FAIL: "}${String(msg.data ?? msg.error ?? "")}`; status.className = `status-line ${msg.ok ? "ok" : "fail"}`; }
    send(ctx, { op: "list_providers" });
  } else if (msg.reply === "migrate_keys") {
    const status = document.getElementById("setStatus");
    if (status) status.textContent = msg.ok ? `imported ${String(msg.data)} key(s)` : `migration failed: ${String(msg.error ?? "")}`;
    send(ctx, { op: "list_providers" });
  } else if (msg.reply === "get_context" && msg.data && typeof msg.data === "object") {
    const data = msg.data as ContextInfo;
    ctx.gitInfo = { has_git: Boolean(data.has_git), branch: String(data.branch ?? ""), default_branch: String(data.default_branch ?? ""), is_protected: Boolean(data.is_protected) };
    if (ctx.gitInfo.branch !== ctx.lastBranch) { ctx.lastBranch = ctx.gitInfo.branch; ctx.branchAck = ""; }
    if (ctx === activeCtx) { renderContext(ctx); renderTree(); }
  }
}

/* ---------------- menus (operate on active tab) ---------------- */
function closeAllMenus(): void { for (const m of [modeMenu, providerMenu, effortMenu, permsMenu]) m.classList.add("hidden"); }
function renderMenu(menu: HTMLDivElement, options: Array<{ label: string; value: string }>, current: string, onPick: (value: string) => void): void {
  menu.replaceChildren();
  for (const opt of options) {
    const btn = document.createElement("button");
    const isCur = opt.value === current;
    if (isCur) btn.classList.add("current");
    btn.innerHTML = `<span class="m-check">${isCur ? ICONS.check : ""}</span><span class="m-label">${escapeHtml(opt.label)}</span>`;
    btn.onclick = (e) => { e.stopPropagation(); closeAllMenus(); onPick(opt.value); };
    menu.appendChild(btn);
  }
}
function wireMenu(button: HTMLButtonElement, menu: HTMLDivElement): void {
  button.onclick = (e) => { e.stopPropagation(); const was = menu.classList.contains("hidden"); closeAllMenus(); if (was) menu.classList.remove("hidden"); };
}
function applyMode(ctx: Ctx, mode: "build" | "plan"): void {
  ctx.modeVal = mode;
  if (ctx === activeCtx) {
    modeLabel.textContent = mode === "build" ? "Build" : "Plan";
    statusMode.textContent = mode === "build" ? "Build" : "Plan";
    renderMenu(modeMenu, [{ label: "Build", value: "build" }, { label: "Plan", value: "plan" }], ctx.modeVal, (v) => pickMode(ctx, v));
  }
}
function pickMode(ctx: Ctx, value: string): void { applyMode(ctx, value as "build" | "plan"); }
function pickEffort(ctx: Ctx, value: string): void {
  ctx.effortVal = value;
  if (ctx === activeCtx) { effortLabel.textContent = value; renderMenu(effortMenu, EFFORTS.map((e) => ({ label: e, value: e })), ctx.effortVal, (v) => pickEffort(ctx, v)); }
  prefs.effortByWs[ctx.root] = value; savePrefs();
  pushEffort(ctx);
}
function pickPerms(ctx: Ctx, value: string): void {
  ctx.permsVal = value;
  if (ctx === activeCtx) { permsLabel.textContent = PERMS.find((p) => p.value === value)?.label ?? value; renderMenu(permsMenu, PERMS.map((p) => ({ label: p.label, value: p.value })), ctx.permsVal, (v) => pickPerms(ctx, v)); }
  send(ctx, { op: "set_permissions", mode: value });
}
/* ---------------- model selection ---------------- */
function effortForModel(modelId: string | null): string {
  if (!modelId) return "default";
  const entry = providerById.get(modelId);
  return (entry?.reasoning_effort as string) || "default";
}
function modelSubtitle(entry: ProviderEntry): string {
  const bits: string[] = [entry.model || "(no model id)"];
  if (entry.context_window) bits.push(`${Math.round(Number(entry.context_window) / 1000)}k ctx`);
  if (entry.max_output_tokens) bits.push(`${entry.max_output_tokens} out`);
  if (entry.supports_tools === false) bits.push("no tools");
  return bits.join(" · ");
}
function pushEffort(ctx: Ctx): void {
  if (ctx === activeCtx) effortLabel.textContent = ctx.effortVal;
  if (ctx.providerId) {
    send(ctx, { op: "set_effort", id: ctx.providerId, effort: ctx.effortVal === "default" ? null : ctx.effortVal });
  }
}
function syncEffortFromModel(ctx: Ctx): void {
  /* No explicit choice for this workspace yet: the model's own setting wins,
     falling back to the app-wide default. */
  if (prefs.effortByWs[ctx.root] !== undefined) return;
  const own = effortForModel(ctx.providerId);
  ctx.effortVal = own !== "default" ? own : prefs.effortGlobal;
  if (ctx === activeCtx) effortLabel.textContent = ctx.effortVal;
}
function selectModel(ctx: Ctx, modelId: string | null, options: { adoptEffort?: boolean } = {}): void {
  ctx.providerId = modelId;
  prefs.providerByWs[ctx.root] = modelId ?? "";
  savePrefs();
  if (options.adoptEffort) {
    // A different model has different capabilities, so it brings its own.
    ctx.effortVal = effortForModel(modelId);
    prefs.effortByWs[ctx.root] = ctx.effortVal;
    savePrefs();
  }
  send(ctx, { op: "set_active", id: modelId ?? "" });
  pushEffort(ctx);
  if (ctx === activeCtx) {
    const cur = providerOptions.find((p) => p.id === ctx.providerId);
    providerLabel.textContent = cur ? cur.label : "No providers";
    providerLabel.title = cur ? `${cur.label} · ${cur.sub}` : "No providers";
    providerBtn.classList.toggle("no-provider", !cur);
    effortLabel.textContent = ctx.effortVal;
    refreshProviderMenu();
    renderTabs();
  }
}
function refreshProviderMenu(): void {
  if (!activeCtx) return;
  const ctx = activeCtx;
  providerMenu.replaceChildren();
  const usable = providerGroups.filter((g) => g.models.some((m) => !m.disabled && !g.disabled));
  if (!usable.length) {
    providerMenu.innerHTML = `<div class="menu-empty">No models yet. Add one in Settings.</div>`;
    return;
  }
  for (const group of usable) {
    const head = document.createElement("div");
    head.className = "menu-group";
    head.innerHTML = `<span class="mg-name">${escapeHtml(group.name)}</span>` +
      `<span class="mg-count">${group.models.filter((m) => !m.disabled).length}</span>`;
    providerMenu.appendChild(head);
    for (const model of group.models) {
      if (model.disabled || group.disabled) continue;
      const entry = providerById.get(model.id);
      if (!entry) continue;
      const btn = document.createElement("button");
      const isCur = entry.id === ctx.providerId;
      if (isCur) btn.classList.add("current");
      btn.innerHTML = `<span class="m-check">${isCur ? ICONS.check : ""}</span>` +
        `<span class="m-label">${escapeHtml(entry.name || entry.model)}</span>` +
        `<span class="m-sub">${escapeHtml(modelSubtitle(entry))}</span>`;
      btn.title = `${group.name} · ${entry.model}`;
      btn.onclick = (e) => {
        e.stopPropagation();
        closeAllMenus();
        if (entry.id === ctx.providerId) return;
        if (ctx.busy) {
          setActivity("switching model - the request in flight is stopped");
        }
        selectModel(ctx, entry.id, { adoptEffort: true });
        setActivity(`${entry.name || entry.model} selected`);
      };
      providerMenu.appendChild(btn);
    }
  }
  const foot = document.createElement("div");
  foot.className = "menu-foot";
  foot.innerHTML = `<button class="m-manage" id="mManageModels">${ICONS.sliders}<span>Manage models…</span></button>`;
  providerMenu.appendChild(foot);
  el<HTMLButtonElement>("mManageModels").onclick = (e) => {
    e.stopPropagation();
    closeAllMenus();
    settingsRequested = true;
    settingsLastTab = "models";
    send(ensureAppCtx(), { op: "list_providers" });
  };
}

/* ---------------- providers / settings ---------------- */
function keyTag(key: string | undefined, disabled: boolean | undefined, active: boolean | undefined): string {
  const tags: string[] = [];
  if (active) tags.push(`<span class="tag active">active</span>`);
  else if (!disabled) tags.push(`<span class="tag">inactive</span>`);
  if (key === "keyring") tags.push(`<span class="tag key-ok">key</span>`);
  else if (key === "plaintext-env") tags.push(`<span class="tag key-env">env key</span>`);
  else tags.push(`<span class="tag key-missing">no key</span>`);
  if (disabled) tags.push(`<span class="tag disabled">disabled</span>`);
  return tags.join(" ");
}
function modelTags(entry: ProviderEntry, active: boolean): string {
  const tags: string[] = [];
  tags.push(active ? `<span class="tag active">active</span>` : "");
  if (entry.reasoning_effort) tags.push(`<span class="tag">effort ${escapeHtml(String(entry.reasoning_effort))}</span>`);
  if (entry.context_window) tags.push(`<span class="tag">${Math.round(Number(entry.context_window) / 1000)}k ctx</span>`);
  if (entry.supports_tools === false) tags.push(`<span class="tag">no tools</span>`);
  if (entry.disabled) tags.push(`<span class="tag disabled">disabled</span>`);
  return tags.filter(Boolean).join(" ");
}
/* The provider + model editor. One provider may hold many models, so adding
   a model to an existing provider is the default path. */
function providerForm(existing?: ProviderEntry, groupId?: string): void {
  const e: Partial<ProviderEntry> = existing ?? {};
  const opts = ["default", "low", "medium", "high", "xhigh"];
  const group = groupId ? providerGroups.find((g) => g.id === groupId) : null;
  const isNewModel = !existing;
  const providerOptionsHtml =
    `<option value="">+ New provider…</option>` +
    providerGroups.map((g) => `<option value="${escapeHtml(g.id)}"${group && g.id === group.id ? " selected" : ""}>${escapeHtml(g.name)}</option>`).join("");
  const showNewProvider = isNewModel && !group;
  const keyLabel = existing
    ? (providerStatuses[existing.id]?.key === "keyring" ? "saved key - leave blank to keep" : "no key stored - paste one")
    : "paste API key";
  showModal(
    `<h3>${existing ? "Edit model" : "Add model"}</h3>` +
      `<div class="field"><label>Provider</label><select id="fProvider">${providerOptionsHtml}</select></div>` +
      `<div id="fNewProviderBlock"${showNewProvider ? "" : ' class="hidden"'}>` +
      `<div class="field"><label>Provider name</label><input id="fProviderName" value="${escapeHtml(group?.name ?? "")}" placeholder="NVIDIA, OpenRouter, …" /></div>` +
      `<div class="field"><label>Base URL</label><input id="fBase" value="${escapeHtml(group?.base_url ?? "https://")}" placeholder="https://api.example.com/v1" /></div>` +
      `</div>` +
      `<div class="field"><label>Display name</label><input id="fName" value="${escapeHtml(String(e["name"] ?? ""))}" placeholder="GLM 5.3 Flash" /></div>` +
      `<div class="field"><label>Model id</label><input id="fModel" value="${escapeHtml(String(e["model"] ?? ""))}" placeholder="glm-5.3-flash" /></div>` +
      `<div class="field"><label>API key</label><input id="fKey" type="password" value="" placeholder="${keyLabel}" /></div>` +
      `<div class="form-grid"><div class="field"><label>Reasoning effort</label><select id="fEffort">${opts.map((x) => `<option${String(e["reasoning_effort"] ?? "default") === x ? " selected" : ""}>${x}</option>`).join("")}</select></div>` +
      `<div class="field"><label>Temperature</label><input id="fTemp" value="${e["temperature"] === null || e["temperature"] === undefined ? "" : escapeHtml(String(e["temperature"]))}" placeholder="default" /></div></div>` +
      `<div class="form-grid"><div class="field"><label>Context window (tokens)</label><input id="fCtx" value="${e["context_window"] ? escapeHtml(String(e["context_window"])) : ""}" placeholder="provider default" /></div>` +
      `<div class="field"><label>Max output tokens</label><input id="fMax" value="${e["max_output_tokens"] ? escapeHtml(String(e["max_output_tokens"])) : ""}" placeholder="no limit" /></div></div>` +
      `<div class="set-row"><div class="set-info"><div class="set-label">Tool calling</div><div class="set-desc">Turn off for models that reject the tools parameter.</div></div><div class="set-ctl"><span id="swTools"></span><span class="set-desc" id="toolsState"></span></div></div>` +
      `<div class="set-row"><div class="set-info"><div class="set-label">Reasoning effort parameter</div><div class="set-desc">Turn off for models without a reasoning knob.</div></div><div class="set-ctl"><span id="swReason"></span></div></div>` +
      `<div class="field"><label>Extra parameters (JSON)</label><input id="fExtra" value="${escapeHtml(Object.keys(e["extra_params"] ?? {}).length ? JSON.stringify(e["extra_params"]) : "")}" placeholder='{"top_p":0.9}' /></div>` +
      `<div class="modal-actions"><button class="btn" id="fBack">Back</button><button class="btn primary" id="fSave">Save model</button></div>`,
  );
  const providerSelect = el<HTMLSelectElement>("fProvider");
  const newBlock = el<HTMLDivElement>("fNewProviderBlock");
  const toggleNewBlock = (): void => { newBlock.classList.toggle("hidden", providerSelect.value !== ""); };
  providerSelect.onchange = toggleNewBlock;
  toggleNewBlock();
  const tools = e["supports_tools"] !== false;
  const reasoning = e["supports_reasoning"] !== false;
  const toolsState = el<HTMLSpanElement>("toolsState");
  const describeTools = (value: boolean): void => { toolsState.textContent = value ? "sent with every request" : "omitted from requests"; };
  wireSwitch(modalBox, "swTools", tools, describeTools);
  wireSwitch(modalBox, "swReason", reasoning, () => { /* no label needed */ });
  describeTools(tools);
  el<HTMLButtonElement>("fBack").onclick = () => { settingsRequested = true; settingsLastTab = "models"; send(ensureAppCtx(), { op: "list_providers" }); };
  el<HTMLButtonElement>("fSave").onclick = () => {
    const name = el<HTMLInputElement>("fName").value.trim();
    const model = el<HTMLInputElement>("fModel").value.trim();
    if (!model) { setActivity("a model id is required"); return; }
    const picked = providerSelect.value;
    let entry: Record<string, unknown>;
    if (picked) {
      const target = providerGroups.find((g) => g.id === picked);
      entry = { id: existing?.id ?? "", provider_id: picked, provider_name: target?.name ?? "", name, model };
    } else {
      const base = el<HTMLInputElement>("fBase").value.trim();
      if (!base) { setActivity("a base URL is required for a new provider"); return; }
      const pname = el<HTMLInputElement>("fProviderName").value.trim() || base;
      entry = { id: existing?.id ?? "", provider_id: base, provider_name: pname, name, model, base_url: base };
    }
    const effort = el<HTMLSelectElement>("fEffort").value;
    const temp = el<HTMLInputElement>("fTemp").value.trim();
    const ctxWindow = el<HTMLInputElement>("fCtx").value.trim();
    const maxOut = el<HTMLInputElement>("fMax").value.trim();
    const extra = el<HTMLInputElement>("fExtra").value.trim();
    let extraParams: Record<string, string | number | boolean | null> = {};
    if (extra) {
      try {
        const parsed = JSON.parse(extra) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) extraParams = parsed as Record<string, string | number | boolean | null>;
        else { setActivity("extra parameters must be a JSON object"); return; }
      } catch { setActivity("extra parameters are not valid JSON"); return; }
    }
    Object.assign(entry, {
      reasoning_effort: effort === "default" ? null : effort,
      temperature: temp || null,
      context_window: ctxWindow || null,
      max_output_tokens: maxOut || null,
      supports_tools: tools,
      supports_reasoning: reasoning,
      extra_params: extraParams,
    });
    const key = el<HTMLInputElement>("fKey").value.trim();
    settingsRequested = true;
    settingsLastTab = "models";
    send(ensureAppCtx(), { op: "save_provider", entry: entry as unknown as ProviderEntry, key: key || undefined });
  };
}
const SETTINGS_TABS = [
  { id: "models", label: "Models", icon: ICONS.layers },
  { id: "themes", label: "Themes", icon: ICONS.palette },
  { id: "general", label: "General", icon: ICONS.sliders },
  { id: "workspace", label: "Workspace", icon: ICONS.folder },
  { id: "interface", label: "Interface", icon: ICONS.layout },
];
function openSettings(tab = settingsLastTab): void {
  settingsLastTab = tab;
  modalBox.classList.add("settings-modal");
  // Force a compact, fixed dialog size inline so no other rule can stretch it.
  modalBox.style.width = "700px";
  modalBox.style.maxWidth = "92vw";
  modalBox.style.maxHeight = "72vh";
  modalBox.style.height = "auto";
  modalBox.innerHTML =
    `<div class="modal-head"><h3>Settings</h3><button class="icon-btn small" id="setClose" aria-label="Close">${ICONS.close}</button></div>` +
    `<div class="settings"><nav class="settings-nav">${SETTINGS_TABS.map((t) => `<button class="sn-item ${t.id === tab ? "active" : ""}" data-nav="${t.id}">${t.icon}<span>${t.label}</span></button>`).join("")}</nav>` +
    `<div class="settings-body" id="settingsBody"><div class="settings-panel"></div></div></div>`;
  modal.classList.remove("hidden");
  el<HTMLButtonElement>("setClose").onclick = hideModal;
  modalBox.querySelectorAll<HTMLElement>("[data-nav]").forEach((b) => { b.onclick = () => openSettings(b.dataset["nav"] ?? "models"); });
  const [html, wire] = settingsPanel(tab);
  el<HTMLDivElement>("settingsBody").innerHTML = `<div class="settings-panel">${html}</div>`;
  wire();
}
function seg(current: string, options: Array<{ value: string; label: string }>, hook: string): string {
  return `<div class="seg" data-seg="${hook}">${options.map((o) => `<button data-v="${o.value}" class="${o.value === current ? "sel" : ""}">${o.label}</button>`).join("")}</div>`;
}
function wireSeg(body: HTMLElement, hook: string, onPick: (v: string) => void): void {
  const root = body.querySelector<HTMLElement>(`[data-seg="${hook}"]`);
  if (root) root.querySelectorAll<HTMLButtonElement>("button").forEach((b) => { b.onclick = () => onPick(b.dataset["v"] ?? ""); });
}
function wireSwitch(body: HTMLElement, id: string, on: boolean, onToggle: (v: boolean) => void): void {
  const sw = body.querySelector<HTMLElement>(`#${id}`);
  if (!sw) return;
  sw.classList.add("switch");
  sw.classList.toggle("on", on); sw.setAttribute("role", "switch"); sw.setAttribute("aria-checked", String(on));
  sw.onclick = () => { const next = !sw.classList.contains("on"); sw.classList.toggle("on", next); sw.setAttribute("aria-checked", String(next)); onToggle(next); };
}
function settingsPanel(tab: string): [string, () => void] {
  if (tab === "themes") return settingsThemes();
  if (tab === "general") return settingsGeneral();
  if (tab === "workspace") return settingsWorkspace();
  if (tab === "interface") return settingsInterface();
  return settingsModels();
}
function settingsModels(): [string, () => void] {
  const migrateNote = providerMigration.length
    ? `<div class="mig-note">${ICONS.info}<span>${escapeHtml(providerMigration.join(" "))}</span></div>`
    : "";
  const groups = providerGroups.map((group) => {
    const activeModelId = providerModels.find((m) => m.id === providerActiveModelId && m.provider_id === group.id)?.id;
    const rows = group.models.map((model) => {
      const entry = providerById.get(model.id);
      if (!entry) return "";
      const active = entry.id === providerActiveModelId;
      return `<tr><td class="p-name">${escapeHtml(entry.name || entry.model)}</td>` +
        `<td class="p-model">${escapeHtml(entry.model || "")}</td>` +
        `<td>${modelTags(entry, active)}</td>` +
        `<td><div class="prov-actions"><button class="btn small ${active ? "" : "primary"}" data-act="${escapeHtml(entry.id)}">Use</button> ` +
        `<button class="btn small" data-test="${escapeHtml(entry.id)}">Test</button> ` +
        `<button class="btn small" data-edit="${escapeHtml(entry.id)}">Edit</button> ` +
        `<button class="btn small danger" data-del="${escapeHtml(entry.id)}">Del</button></div></td></tr>`;
    }).join("");
    const st = providerStatuses[group.models[0]?.id ?? ""] ?? {};
    return `<div class="prov-group${activeModelId ? " active" : ""}">` +
      `<div class="prov-group-head"><span class="pg-name">${escapeHtml(group.name)}</span>` +
      `<span class="pg-meta">${escapeHtml(group.base_url || "")} ${keyTag(st.key, group.disabled, false)}</span>` +
      `<span class="prov-actions"><button class="btn small" data-addmodel="${escapeHtml(group.id)}">Add model</button>` +
      `<button class="btn small" data-test="${escapeHtml(group.id)}">Test</button>` +
      `<button class="btn small danger" data-delgroup="${escapeHtml(group.id)}">Delete provider</button></span></div>` +
      (rows ? `<table class="prov-table"><colgroup><col style="width:26%"><col style="width:31%"><col style="width:22%"><col style="width:21%"></colgroup><thead><tr><th>Model</th><th>Model id</th><th>Settings</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="panel-empty">No models in this provider yet.</div>`) +
      `</div>`;
  }).join("");
  const html =
    `<p class="sub">Providers hold credentials; each model keeps its own id and settings. Several models can share one provider, and deleting one leaves the rest alone. The active model is applied here or from the composer.</p>` +
    (providerGroups.length ? groups : `<div class="panel-empty">No providers yet. Add one to get started.</div>`) +
    migrateNote +
    `<div class="modal-actions" style="justify-content:flex-start"><button class="btn" id="mMigrate">Migrate keys</button><button class="btn primary" id="mAdd">Add model</button></div><div id="setStatus" class="status-line"></div>` +
    `<h4 class="card-title" style="margin-top:18px">Default effort</h4>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Reasoning effort for new workspaces</div><div class="set-desc">Each model keeps its own effort; switching models loads that model's setting.</div></div><div class="set-ctl"><select class="sel-inline" id="defEffort">${EFFORTS.map((e) => `<option${e === prefs.effortGlobal ? " selected" : ""}>${e}</option>`).join("")}</select></div></div>`;
  const wire = (): void => {
    const body = el<HTMLDivElement>("settingsBody");
    body.querySelectorAll("[data-act]").forEach((b) => { (b as HTMLButtonElement).onclick = () => {
      settingsRequested = true;
      const id = (b as HTMLElement).dataset["act"] ?? "";
      selectModel(ensureAppCtx(), id, { adoptEffort: true });
      send(ensureAppCtx(), { op: "set_active", id });
    }; });
    body.querySelectorAll("[data-test]").forEach((b) => { (b as HTMLButtonElement).onclick = () => { const s = document.getElementById("setStatus"); if (s) { s.textContent = "testing…"; s.className = "status-line"; } send(ensureAppCtx(), { op: "test_provider", id: (b as HTMLElement).dataset["test"] ?? "" }); }; });
    body.querySelectorAll("[data-edit]").forEach((b) => { (b as HTMLButtonElement).onclick = () => {
      const id = (b as HTMLElement).dataset["edit"] ?? "";
      providerForm(providerById.get(id), providerById.get(id)?.provider_id);
    }; });
    body.querySelectorAll("[data-addmodel]").forEach((b) => { (b as HTMLButtonElement).onclick = () => providerForm(undefined, (b as HTMLElement).dataset["addmodel"]); });
    body.querySelectorAll("[data-del]").forEach((b) => {
      (b as HTMLButtonElement).onclick = () => {
        const id = (b as HTMLElement).dataset["del"] ?? "";
        const model = providerById.get(id);
        showModal(`<h3>Delete model</h3><p>Delete <b>${escapeHtml(model?.name ?? id)}</b>? The provider and its other models stay.</p><div class="modal-actions"><button class="btn" id="pdCancel">Cancel</button><button class="btn danger" id="pdOk">Delete</button></div>`);
        el<HTMLButtonElement>("pdOk").onclick = () => { settingsRequested = true; send(ensureAppCtx(), { op: "delete_provider", id, scope: "model" }); };
        el<HTMLButtonElement>("pdCancel").onclick = () => openSettings("models");
      };
    });
    body.querySelectorAll("[data-delgroup]").forEach((b) => {
      (b as HTMLButtonElement).onclick = () => {
        const id = (b as HTMLElement).dataset["delgroup"] ?? "";
        const group = providerGroups.find((g) => g.id === id);
        showModal(`<h3>Delete provider</h3><p>Delete <b>${escapeHtml(group?.name ?? id)}</b> and its ${group?.models.length ?? 0} model(s)? Stored keys are removed too.</p><div class="modal-actions"><button class="btn" id="pgCancel">Cancel</button><button class="btn danger" id="pgOk">Delete</button></div>`);
        el<HTMLButtonElement>("pgOk").onclick = () => { settingsRequested = true; send(ensureAppCtx(), { op: "delete_provider", id, scope: "provider" }); };
        el<HTMLButtonElement>("pgCancel").onclick = () => openSettings("models");
      };
    });
    el<HTMLButtonElement>("mAdd").onclick = () => providerForm();
    el<HTMLButtonElement>("mMigrate").onclick = () => send(ensureAppCtx(), { op: "migrate_keys" });
    el<HTMLSelectElement>("defEffort").onchange = () => {
      prefs.effortGlobal = el<HTMLSelectElement>("defEffort").value; savePrefs();
      const ctx2 = activeCtx;
      if (ctx2 && !prefs.effortByWs[ctx2.root]) { ctx2.effortVal = prefs.effortGlobal; pushEffort(ctx2); }
    };
  };
  return [html, wire];
}
function settingsThemes(): [string, () => void] {
  const html = `<p class="sub">Choose the accent identity. Themes apply instantly and are remembered.</p><div class="theme-grid">${THEMES.map((t) => `<button class="theme-card ${t.id === prefs.theme ? "sel" : ""}" data-theme="${t.id}"><span class="tc-check">${ICONS.check}</span><div class="theme-swatch">${t.swatch.map((c) => `<span style="background:${c}"></span>`).join("")}</div><div class="theme-name">${t.name}</div><div class="theme-desc">${t.desc}</div></button>`).join("")}</div>`;
  const wire = (): void => {
    el<HTMLDivElement>("settingsBody").querySelectorAll<HTMLElement>("[data-theme]").forEach((b) => { b.onclick = () => { prefs.theme = b.dataset["theme"] ?? "neon"; applyAppearance(); savePrefs(); openSettings("themes"); }; });
  };
  return [html, wire];
}
function settingsGeneral(): [string, () => void] {
  const html =
    `<p class="sub">Behavior defaults that apply across workspaces.</p>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Confirm before editing the default branch</div><div class="set-desc">Ask before a turn that writes directly to the protected branch.</div></div><div class="set-ctl"><span id="swProtected"></span></div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Default mode for new sessions</div><div class="set-desc">Plan mode is read-only.</div></div><div class="set-ctl">${seg(prefs.defaultMode, [{ value: "build", label: "Build" }, { value: "plan", label: "Plan" }], "defMode")}</div></div>` +
    `<h4 class="card-title" style="margin-top:16px">Keyboard shortcuts</h4>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Focus composer</div></div><div class="set-ctl"><span class="set-desc">Ctrl / ⌘ + K</span></div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Send / new line</div></div><div class="set-ctl"><span class="set-desc">Enter / Shift+Enter</span></div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Permission · Allow / Deny</div></div><div class="set-ctl"><span class="set-desc">Enter / Esc</span></div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Cancel edit · close dialog</div></div><div class="set-ctl"><span class="set-desc">Esc</span></div></div>`;
  const wire = (): void => {
    const body = el<HTMLDivElement>("settingsBody");
    wireSeg(body, "defMode", (v) => { prefs.defaultMode = v as "build" | "plan"; savePrefs(); const ctx = activeCtx; if (ctx) applyMode(ctx, prefs.defaultMode); });
    wireSwitch(body, "swProtected", prefs.confirmProtected, (v) => { prefs.confirmProtected = v; savePrefs(); });
  };
  return [html, wire];
}
function settingsWorkspace(): [string, () => void] {
  const items = recents.map((w) => `<div class="ws-item-row"><span class="ws-item-glyph">${ICONS.folder}</span><div class="ws-item-meta"><div class="ws-item-name">${escapeHtml(w.name)}${activeCtx && w.root === activeCtx.root ? ' <span class="tag active">current</span>' : ""}</div><div class="ws-item-path">${escapeHtml(w.root)}</div></div><button class="btn small" data-open="${escapeHtml(w.root)}" ${activeCtx && w.root === activeCtx.root ? "disabled" : ""}>Open</button></div>`).join("");
  const html =
    `<p class="sub">Each open workspace is its own tab/context. Recent workspaces stay listed so you can jump back instantly; open ones stay running.</p>` +
    `<div class="modal-actions" style="justify-content:flex-start;margin:0 0 10px"><button class="btn primary" id="wsBrowse">Open folder…</button></div>${items || `<div class="panel-empty">No recent workspaces.</div>`}`;
  const wire = (): void => {
    el<HTMLButtonElement>("wsBrowse").onclick = () => { hideModal(); void openFolder(); };
    el<HTMLDivElement>("settingsBody").querySelectorAll("[data-open]").forEach((b) => { (b as HTMLButtonElement).onclick = () => { const root = (b as HTMLElement).dataset["open"] ?? ""; hideModal(); openTab(root, "resume", null); }; });
  };
  return [html, wire];
}
function settingsInterface(): [string, () => void] {
  const html =
    `<p class="sub">Adjust density and scale. Changes apply immediately and persist.</p>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Font size</div><div class="set-desc">Scales interface text.</div></div><div class="set-ctl">${seg(prefs.fontScale, [{ value: "sm", label: "Small" }, { value: "base", label: "Default" }, { value: "lg", label: "Large" }], "font")}</div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Density</div><div class="set-desc">Compact tightens spacing in lists and toolbars.</div></div><div class="set-ctl">${seg(prefs.density, [{ value: "comfortable", label: "Comfortable" }, { value: "compact", label: "Compact" }], "density")}</div></div>` +
    `<div class="set-row"><div class="set-info"><div class="set-label">Reduce motion</div><div class="set-desc">Disable transitions and animations.</div></div><div class="set-ctl"><span id="swMotion"></span></div></div>`;
  const wire = (): void => {
    const body = el<HTMLDivElement>("settingsBody");
    wireSeg(body, "font", (v) => { prefs.fontScale = v as Prefs["fontScale"]; applyAppearance(); savePrefs(); openSettings("interface"); });
    wireSeg(body, "density", (v) => { prefs.density = v as Prefs["density"]; applyAppearance(); savePrefs(); openSettings("interface"); });
    wireSwitch(body, "swMotion", prefs.reduceMotion, (v) => { prefs.reduceMotion = v; applyAppearance(); savePrefs(); });
  };
  return [html, wire];
}

/* ---------------- workspace opener ---------------- */
function openFolder(): void {
  if (!window.harness?.selectWorkspace) { setActivity("workspace select unavailable"); return; }
  setActivity("open a folder…");
  void window.harness.selectWorkspace().then((picked) => {
    if (picked) { void fetchRecents(); openTab(picked.projectRoot, "resume", null); }
    else setActivity("ready");
  });
}
/* ---------------- wiring ---------------- */
el<HTMLButtonElement>("btnNew").onclick = () => newSessionTab();
el<HTMLButtonElement>("newTab").onclick = () => newSessionTab();
el<HTMLButtonElement>("landingNew").onclick = () => newSessionTab();
el<HTMLButtonElement>("btnOpenFolder").onclick = () => openFolder();
el<HTMLButtonElement>("editCancel").onclick = () => { if (activeCtx) cancelEdit(activeCtx); };
el<HTMLButtonElement>("btnSettings").onclick = () => {
  dlog("boot", "CLICK settings");
  settingsRequested = true;
  settingsLastTab = settingsLastTab || "models";
  if (!send(ensureAppCtx(), { op: "list_providers" })) settingsRequested = false;
};

sendstop.onclick = () => { if (!activeCtx) return; if (activeCtx.busy) send(activeCtx, { op: "stop" }); else submit(activeCtx, entry.value); };
entry.addEventListener("input", () => { autogrow(); if (activeCtx) activeCtx.entryDraft = entry.value; updateSendState(); });
entry.addEventListener("keydown", (e) => {
  if (activeApproval) return;
  if (e.key === "Escape" && activeCtx && activeCtx.pendingRewind !== null) { e.preventDefault(); cancelEdit(activeCtx); return; }
  if (e.key === "Enter" && !e.shiftKey && activeCtx) { e.preventDefault(); submit(activeCtx, entry.value); }
});
expandBtn.onclick = () => { const ex = promptZone.classList.toggle("expanded"); expandBtn.classList.toggle("on", ex); autogrow(); entry.focus(); };

wireMenu(modeBtn, modeMenu);
wireMenu(effortBtn, effortMenu);
wireMenu(permsBtn, permsMenu);
providerBtn.onclick = (e) => {
  e.stopPropagation();
  if (!activeCtx) return;
  if (!providerOptions.length) { settingsRequested = true; settingsLastTab = "models"; if (!send(ensureAppCtx(), { op: "list_providers" })) settingsRequested = false; else setActivity("no providers yet - opening Settings…"); return; }
  const was = providerMenu.classList.contains("hidden"); closeAllMenus(); if (was) providerMenu.classList.remove("hidden");
};

document.addEventListener("click", (e) => { closeAllMenus(); const t = e.target as HTMLElement; if (contextMenu && !contextMenu.contains(t)) hideContextMenu(); });
document.addEventListener("keydown", (e) => {
  if (activeApproval) {
    if (e.key === "Enter") { e.preventDefault(); resolveApproval(activeApproval.ctx, activeApproval.id, true); }
    else if (e.key === "Escape") { e.preventDefault(); resolveApproval(activeApproval.ctx, activeApproval.id, false); }
    return;
  }
  if (e.key === "Escape") {
    if (activeCtx && activeCtx.pendingRewind !== null) { cancelEdit(activeCtx); return; }
    closeAllMenus(); hideContextMenu(); hideModal(); for (const t of tabs) t.pendingText = null; return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); entry.focus(); }
});

el<HTMLButtonElement>("toggleLog").onclick = (e) => {
  const drawer = el("logDrawer"); const btn = e.currentTarget as HTMLButtonElement;
  const hidden = drawer.classList.contains("hidden");
  drawer.classList.toggle("hidden", !hidden); btn.classList.toggle("on", hidden); btn.classList.toggle("active", hidden);
  if (hidden) { drawer.textContent = logLines.join("\n"); drawer.scrollTop = drawer.scrollHeight; }
};
el<HTMLButtonElement>("toggleLeft").onclick = (e) => { shell.classList.toggle("hide-left"); const btn = e.currentTarget as HTMLButtonElement; btn.classList.toggle("active", !shell.classList.contains("hide-left")); };
el<HTMLButtonElement>("toggleRight").onclick = (e) => {
  shell.classList.toggle("hide-right"); const hidden = shell.classList.contains("hide-right");
  (e.currentTarget as HTMLButtonElement).classList.toggle("active", !hidden);
  usageChip.classList.toggle("hidden", !hidden);
  if (!hidden) usagePop.classList.add("hidden");
};
usageChip.addEventListener("mouseenter", showUsagePop);
usageChip.addEventListener("mouseleave", scheduleHidePop);
usageChip.addEventListener("click", () => (usagePop.classList.contains("hidden") ? showUsagePop() : usagePop.classList.add("hidden")));
usagePop.addEventListener("mouseenter", () => { if (popHideTimer) { clearTimeout(popHideTimer); popHideTimer = null; } });
usagePop.addEventListener("mouseleave", scheduleHidePop);

document.querySelectorAll<HTMLButtonElement>(".empty-chips .chip").forEach((chip) => {
  chip.onclick = () => { entry.value = chip.dataset["fill"] ?? ""; if (activeCtx) activeCtx.entryDraft = entry.value; autogrow(); updateSendState(); entry.focus(); };
});

/* ---------------- boot ---------------- */
renderTabs();
let booted = false;
function boot(source: string): void {
  if (booted) return;
  booted = true;
  const root = bootRoot || "";
  dlog("boot", "complete source=", source, "port=", bootPort, "root=", root, "hasIPC=", Boolean(window.harness?.onConfig));
  const ctx = makeCtx(root, baseName(root || "Workspace"), "resume", null);
  tabs = [ctx];
  activeCtx = ctx;
  autogrow();
  renderActive();
  connect();
}
window.harness?.onConfig((cfg) => { if (cfg.projectRoot) bootRoot = cfg.projectRoot; if (cfg.port) bootPort = String(cfg.port); boot("ipc"); });
setTimeout(() => { if (!booted) boot("query"); }, 2500);


