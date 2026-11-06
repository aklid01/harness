import {
  activeCtx,
  agentConfig,
  clearRetryCard,
  el,
  ICONS,
  modalBox,
  selectModel,
  escapeHtml,
  hideModal,
  modelSubtitle,
  openSettings,
  providerById,
  providerGroups,
  providerOptions,
  request,
  scrollBottom,
  send,
  setActivity,
  showModal,
} from "./renderer.js";
import type { Ctx } from "./renderer.js";
import type {
  ReviewEstimate,
  ReviewEventPayload,
  ReviewFinding,
  ReviewReport,
  ReviewSeverity,
  ReviewStage,
} from "../shared/protocol.js";

interface ReviewModelRow {
  model_id: string;
  label: string;
  state: string;
  findings?: number;
  rejected?: number;
  error?: string;
  seconds?: number;
  tokens?: number;
}

export interface ReviewView {
  stage: string;
  reviewId: string;
  report: ReviewReport | null;
  group: "file" | "category";
  selected: Set<string>;
  models: ReviewModelRow[];
  files: string[];
  message: string;
  error: string;
  redacted: number;
  estimate: ReviewEstimate | null;
}

export const REVIEW_CATEGORY_OPTIONS: Array<{ value: string; label: string }> =
  [
    { value: "", label: "All targets" },
    { value: "correctness", label: "Correctness" },
    { value: "error-handling", label: "Error handling" },
    { value: "security", label: "Security" },
    { value: "performance", label: "Performance" },
    { value: "concurrency", label: "Concurrency" },
    { value: "quality", label: "Code quality" },
    { value: "maintainability", label: "Maintainability" },
    { value: "tests", label: "Tests" },
    { value: "dependencies", label: "Dependencies" },
    { value: "documentation", label: "Documentation" },
    { value: "observability", label: "Observability" },
    { value: "agent-experience", label: "Agent experience" },
  ];

export const REVIEW_SEVERITY_ORDER: ReviewSeverity[] = [
  "blocker",
  "high",
  "medium",
  "low",
  "nit",
];

const REVIEW_GROUP_OPTIONS: Array<{
  value: "file" | "category";
  label: string;
}> = [
  { value: "file", label: "By file" },
  { value: "category", label: "By category" },
];

export function reviewView(ctx: Ctx): ReviewView {
  if (!ctx.review) {
    ctx.review = {
      stage: "",
      reviewId: "",
      report: null,
      group: "file",
      selected: new Set<string>(),
      models: [],
      files: [],
      message: "",
      error: "",
      redacted: 0,
      estimate: null,
    };
  }
  return ctx.review;
}

function reviewSeverityRank(severity: string): number {
  const index = REVIEW_SEVERITY_ORDER.indexOf(severity as ReviewSeverity);
  return index === -1 ? REVIEW_SEVERITY_ORDER.length : index;
}

function reviewBadgeClass(severity: string): string {
  return `v-sev v-sev-${severity}`;
}

function reviewFindingSeverityClass(severity: string): string {
  return `v-f-sev v-sev v-sev-${severity}`;
}

function reviewGroups(
  report: ReviewReport,
  group: "file" | "category",
): Array<[string, ReviewFinding[]]> {
  const buckets = new Map<string, ReviewFinding[]>();
  for (const finding of report.findings) {
    const key =
      group === "file"
        ? finding.file
        : finding.category_label || finding.category;
    const list = buckets.get(key);
    if (list) list.push(finding);
    else buckets.set(key, [finding]);
  }
  return [...buckets.entries()];
}

function reviewAction(
  ctx: Ctx,
  action: "dismiss" | "send",
  findingIds: string[],
  findingId?: string,
  apply?: boolean,
): void {
  send(ctx, {
    op: "review_action",
    action,
    review_id: reviewView(ctx).reviewId,
    finding_ids: findingIds,
    finding_id: findingId,
    apply,
  });
}

export function reviewRerun(ctx: Ctx, force: boolean, models?: string[]): void {
  send(ctx, {
    op: "review_start",
    args: { scope: "run" },
    force,
    models: models ?? [],
  });
}

function reviewSelectedIds(view: ReviewView): string[] {
  if (view.selected.size) return [...view.selected];
  return (view.report?.findings ?? []).map((f) => f.id);
}

export function clearReviewCard(ctx: Ctx): void {
  ctx.reviewCard?.remove();
  ctx.reviewCard = null;
}

function reviewFindingNode(
  ctx: Ctx,
  finding: ReviewFinding,
  dismissed: Set<string>,
): HTMLElement {
  const node = document.createElement("details");
  node.className = "v-finding";
  node.dataset["id"] = finding.id;
  if (dismissed.has(finding.id)) node.classList.add("dismissed");

  const head = document.createElement("summary");
  head.className = "v-f-head";

  const id = document.createElement("span");
  id.className = "v-f-id";
  id.textContent = finding.id;

  const sev = document.createElement("span");
  sev.className = reviewFindingSeverityClass(finding.severity);
  sev.textContent = finding.severity;

  const cat = document.createElement("span");
  cat.className = "v-f-cat";
  cat.textContent = finding.category_label || finding.category;

  const where = document.createElement("span");
  where.className = "v-f-where";
  where.textContent = finding.line
    ? `${finding.file}:${finding.line}`
    : finding.file;

  const models = document.createElement("span");
  models.className = "v-f-models";
  if (finding.models?.length > 1) {
    models.textContent = `${finding.models.length} reviewers agree`;
    models.title = finding.models.join(", ");
  } else if (finding.models?.length === 1) {
    models.textContent = finding.models[0];
  }

  head.append(id, sev, cat, where, models);

  const body = document.createElement("div");
  body.className = "v-f-body";

  const summary = document.createElement("div");
  summary.className = "v-f-sum";
  summary.textContent = finding.summary;
  body.appendChild(summary);

  if (finding.why) {
    const why = document.createElement("div");
    why.className = "v-f-why";
    why.textContent = finding.why;
    body.appendChild(why);
  }
  if (finding.fix) {
    const fix = document.createElement("pre");
    fix.className = "v-f-fix";
    fix.textContent = finding.fix;
    body.appendChild(fix);
  }
  if (finding.conflict_with?.length) {
    const clash = document.createElement("div");
    clash.className = "v-f-conflict";
    clash.textContent = `Another reviewer read this differently: ${finding.conflict_with.join(", ")}`;
    body.appendChild(clash);
  }

  const actions = document.createElement("div");
  actions.className = "v-f-actions";
  const apply = document.createElement("button");
  apply.className = "btn small primary";
  apply.textContent = "Apply fix";
  apply.setAttribute(
    "aria-label",
    `Ask the agent to apply the suggested fix for ${finding.id}`,
  );
  apply.onclick = (event) => {
    event.stopPropagation();
    reviewAction(ctx, "send", [finding.id], finding.id, true);
    setActivity(`sent ${finding.id} to the agent`);
  };
  const pick = document.createElement("button");
  pick.className = "btn small";
  pick.textContent = "Select";
  pick.setAttribute(
    "aria-label",
    `Select finding ${finding.id} to send to the agent`,
  );
  pick.onclick = (event) => {
    event.stopPropagation();
    const view = reviewView(ctx);
    if (view.selected.has(finding.id)) view.selected.delete(finding.id);
    else view.selected.add(finding.id);
    pick.textContent = view.selected.has(finding.id) ? "Selected" : "Select";
    pick.setAttribute("aria-pressed", String(view.selected.has(finding.id)));
  };
  const dismiss = document.createElement("button");
  dismiss.className = "btn small";
  dismiss.textContent = "Dismiss";
  dismiss.setAttribute("aria-label", `Dismiss finding ${finding.id}`);
  dismiss.onclick = (event) => {
    event.stopPropagation();
    reviewAction(ctx, "dismiss", [], finding.id);
  };
  const open = document.createElement("button");
  open.className = "btn small";
  open.textContent = "Open file";
  open.setAttribute(
    "aria-label",
    `Open ${finding.file} at line ${finding.line || 1}`,
  );
  open.onclick = (event) => {
    event.stopPropagation();
    const root = ctx.root.replace(/\\/g, "/");
    const target = `${root}/${finding.file}`;
    void window.harness?.openPath(target);
  };
  actions.append(apply, pick, dismiss, open);

  node.append(head, body, actions);
  return node;
}

function reviewCardNode(ctx: Ctx, view: ReviewView): HTMLDivElement {
  const card = document.createElement("div");
  card.className = "review";
  card.setAttribute("role", "region");
  card.setAttribute("aria-label", "Multi-model review");

  const head = document.createElement("div");
  head.className = "v-head";
  const ico = document.createElement("span");
  ico.className = "v-ico";
  ico.setAttribute("aria-hidden", "true");
  ico.innerHTML = ICONS.branch;
  const title = document.createElement("div");
  title.className = "v-title";
  title.textContent = "Multi-model review";
  const badge = document.createElement("span");
  badge.className = "v-badge";
  head.append(ico, title, badge);
  card.appendChild(head);

  const report = view.report;
  const state = view.stage;

  if (state === "offer") {
    badge.textContent = "not started";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent =
      view.message ||
      "the agent changed some files - run a multi-model review?";
    const actions = document.createElement("div");
    actions.className = "v-actions";
    const start = document.createElement("button");
    start.className = "btn small primary";
    start.textContent = "Run review";
    start.onclick = () => reviewRerun(ctx, false);
    const skip = document.createElement("button");
    skip.className = "btn small";
    skip.textContent = "Not now";
    skip.onclick = () => clearReviewCard(ctx);
    actions.append(start, skip);
    card.append(headline, actions);
    return card;
  }

  if (state === "running") {
    badge.textContent = "running";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent = "Reviewing the change with independent models…";
    const cost = document.createElement("div");
    cost.className = "v-note";
    if (view.estimate) {
      const per = view.estimate.tokens_per_model ?? 0;
      const total = view.estimate.tokens_total ?? 0;
      cost.textContent = view.estimate.over_limit
        ? `About ${total} tokens (limit ${view.estimate.limit}) - this review is over its budget.`
        : `About ${per} tokens per model, ${total} in total.`;
    }
    const list = document.createElement("div");
    list.className = "v-models";
    for (const model of view.models) {
      const row = document.createElement("div");
      row.className = `v-model v-model-${model.state}`;
      const name = document.createElement("span");
      name.className = "v-model-name";
      name.textContent = model.label || model.model_id;
      const status = document.createElement("span");
      status.className = "v-model-state";
      status.textContent =
        model.state === "running"
          ? "reviewing…"
          : model.state === "done"
            ? `${model.findings ?? 0} finding(s)`
            : model.state === "failed"
              ? "failed"
              : "skipped";
      row.append(name, status);
      list.appendChild(row);
    }
    const actions = document.createElement("div");
    actions.className = "v-actions";
    const cancel = document.createElement("button");
    cancel.className = "btn small";
    cancel.textContent = "Cancel";
    cancel.onclick = () => send(ctx, { op: "review_cancel" });
    actions.appendChild(cancel);
    card.append(headline, cost, list, actions);
    return card;
  }

  if (state === "nothing") {
    badge.textContent = "nothing to review";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent =
      view.message || "there was nothing to review - no files changed";
    card.appendChild(headline);
    return card;
  }

  if (state === "failed") {
    badge.textContent = "failed";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent = "The review did not finish.";
    const detail = document.createElement("div");
    detail.className = "v-detail";
    detail.textContent = view.error || view.message || "";
    card.append(headline, detail);
    if (view.error?.includes("no reviewer model")) {
      const actions = document.createElement("div");
      actions.className = "v-actions";
      const open = document.createElement("button");
      open.className = "btn small primary";
      open.textContent = "Open review settings";
      open.onclick = () => openSettings("review");
      actions.appendChild(open);
      card.appendChild(actions);
    }
    return card;
  }

  if (state === "cancelled") {
    badge.textContent = "cancelled";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent = "The review was cancelled - a new run took over.";
    card.appendChild(headline);
    return card;
  }

  if (!report) {
    badge.textContent = "no report";
    const headline = document.createElement("div");
    headline.className = "v-headline";
    headline.textContent = "There is no review report to show.";
    card.appendChild(headline);
    return card;
  }
  const findings = report.findings ?? [];
  const models = report?.models ?? [];
  const failed = models.filter((m) => m.status === "failed");
  const skipped = models.filter((m) => m.status === "skipped");
  const partial = failed.length > 0 || skipped.length > 0;
  const verdict = report?.verdict ?? "clean";
  badge.textContent = partial
    ? `${report?.verdict_label ?? verdict} (partial)`
    : (report?.verdict_label ?? verdict);
  if (verdict === "changes_requested") badge.classList.add("v-badge-warn");
  if (verdict === "clean") badge.classList.add("v-badge-ok");

  const headline = document.createElement("div");
  headline.className = "v-headline";
  headline.textContent = findings.length
    ? `${findings.length} finding${findings.length === 1 ? "" : "s"} across ${report?.scope?.files?.length ?? 0} file(s).`
    : "No issues were found.";
  card.appendChild(headline);

  if (report?.stale) {
    const stale = document.createElement("div");
    stale.className = "v-note";
    stale.textContent =
      "This review is out of date - the work it looked at was rewound.";
    card.appendChild(stale);
  }
  if (view.redacted || report?.redacted) {
    const redacted = document.createElement("div");
    redacted.className = "v-note";
    redacted.textContent = `${report?.redacted ?? view.redacted} secret-looking value(s) were redacted before the reviewers saw them.`;
    card.appendChild(redacted);
  }
  if (partial) {
    const note = document.createElement("div");
    note.className = "v-note";
    const names = [
      ...failed.map((m) => `${m.label || m.model_id} (failed)`),
      ...skipped.map((m) => `${m.label || m.model_id} (skipped)`),
    ];
    note.textContent = `Not every reviewer finished: ${names.join(", ")}.`;
    card.appendChild(note);
  }

  const counts = document.createElement("div");
  counts.className = "v-counts";
  for (const severity of REVIEW_SEVERITY_ORDER) {
    const count = report?.by_severity?.[severity] ?? 0;
    if (!count) continue;
    const chip = document.createElement("span");
    chip.className = reviewBadgeClass(severity);
    chip.textContent = `${count} ${severity}`;
    counts.appendChild(chip);
  }
  if (counts.childElementCount) card.appendChild(counts);

  if (models.length) {
    const list = document.createElement("div");
    list.className = "v-models";
    for (const model of models) {
      const row = document.createElement("div");
      row.className = `v-model v-model-${model.status}`;
      const name = document.createElement("span");
      name.className = "v-model-name";
      name.textContent = model.label || model.model_id;
      const status = document.createElement("span");
      status.className = "v-model-state";
      status.textContent =
        model.status === "ran"
          ? `${model.finding_count} finding(s) · ${model.tokens} tokens · ${model.seconds}s`
          : model.status === "failed"
            ? `failed: ${model.error}`
            : `skipped: ${model.error}`;
      row.append(name, status);
      list.appendChild(row);
    }
    card.appendChild(list);
  }

  if (findings.length) {
    const toolbar = document.createElement("div");
    toolbar.className = "v-toolbar";
    const groups = document.createElement("div");
    groups.className = "seg";
    groups.setAttribute("role", "group");
    groups.setAttribute("aria-label", "Group findings");
    for (const option of REVIEW_GROUP_OPTIONS) {
      const button = document.createElement("button");
      button.textContent = option.label;
      button.className = view.group === option.value ? "sel" : "";
      button.setAttribute("aria-pressed", String(view.group === option.value));
      button.onclick = () => {
        reviewView(ctx).group = option.value;
        showReview(ctx, { stage: "results" });
      };
      groups.appendChild(button);
    }
    toolbar.appendChild(groups);
    card.appendChild(toolbar);

    const dismissed = new Set(report?.dismissed ?? []);
    const list = document.createElement("div");
    list.className = "v-findings";
    for (const [key, group] of reviewGroups(report!, view.group)) {
      const section = document.createElement("div");
      section.className = "v-group";
      const heading = document.createElement("div");
      heading.className = "v-group-title";
      heading.textContent = key;
      section.appendChild(heading);
      for (const finding of group) {
        section.appendChild(reviewFindingNode(ctx, finding, dismissed));
      }
      list.appendChild(section);
    }
    card.appendChild(list);
  }

  const actions = document.createElement("div");
  actions.className = "v-actions";
  const sendBtn = document.createElement("button");
  sendBtn.className = "btn small primary";
  const chosen = reviewSelectedIds(view);

  sendBtn.textContent = "Send findings to the agent";
  sendBtn.setAttribute(
    "aria-label",
    `Send ${chosen.length} review finding(s) to the agent`,
  );
  sendBtn.disabled = !findings.length;
  sendBtn.onclick = () => {
    reviewAction(ctx, "send", chosen);
    setActivity("findings sent to the agent");
  };
  const rerun = document.createElement("button");
  rerun.className = "btn small";
  rerun.textContent = "Re-run";
  rerun.onclick = () => reviewRerun(ctx, true);
  const other = document.createElement("button");
  other.className = "btn small";
  other.textContent = "Re-run with other models";
  other.onclick = () => askReviewModels(ctx);
  const close = document.createElement("button");
  close.className = "btn small";
  close.textContent = "Dismiss card";
  close.onclick = () => clearReviewCard(ctx);
  actions.append(sendBtn, rerun, other, close);
  card.appendChild(actions);
  return card;
}

export function askReviewModels(ctx: Ctx): void {
  const configured = (agentConfig?.review?.reviewers ?? []).filter(
    (r) => r.enabled,
  );
  const options = configured.length
    ? configured
    : providerOptions.map((o) => ({
        model_id: o.id,
        label: o.label,
        enabled: false,
        focus: "",
      }));
  if (!options.length) {
    setActivity("no model to review with - add one in Settings, Review");
    return;
  }
  const body = options
    .map(
      (o, i) =>
        `<label class="rv-model"><input type="checkbox" data-i="${i}"${o.enabled ? " checked" : ""}> ` +
        `<span>${escapeHtml(o.label || o.model_id)}</span></label>`,
    )
    .join("");
  showModal(
    `<div class="modal-head"><h3>Review with</h3></div>` +
      `<div class="rv-models">${body}</div>` +
      `<div class="modal-actions"><button class="btn primary" id="rvGo">Run review</button>` +
      `<button class="btn" id="rvCancel">Cancel</button></div>`,
    "keep",
  );
  el<HTMLButtonElement>("rvCancel").onclick = hideModal;
  el<HTMLButtonElement>("rvGo").onclick = () => {
    const picked: string[] = [];
    document
      .querySelectorAll<HTMLInputElement>(".rv-models input[type=checkbox]")
      .forEach((box) => {
        if (box.checked) {
          const option = options[Number(box.dataset["i"] ?? 0)];
          if (option) picked.push(option.model_id);
        }
      });
    hideModal();
    if (!picked.length) {
      setActivity("pick at least one model");
      return;
    }
    reviewRerun(ctx, true, picked);
  };
}

export function showReview(ctx: Ctx, p: ReviewEventPayload): void {
  const view = reviewView(ctx);
  const stage = p.stage ?? "started";

  const phase: ReviewStage =
    stage === "offer"
      ? "offer"
      : stage === "completed" ||
          stage === "results" ||
          stage === "stale" ||
          stage === "updated"
        ? "results"
        : stage === "failed"
          ? "failed"
          : stage === "nothing"
            ? "nothing"
            : stage === "cancelled"
              ? "cancelled"
              : "running";
  view.stage = phase;
  if (p.review_id) view.reviewId = p.review_id;
  if (p.message) view.message = p.message;
  if (p.error) view.error = p.error;
  if (p.redacted) view.redacted = p.redacted;
  if (p.files) view.files = p.files;
  if (p.model_id) {
    const row = view.models.find((m) => m.model_id === p.model_id);
    const label = p.label || row?.label || p.model_id;
    if (!row) {
      view.models.push({
        model_id: p.model_id,
        label,
        state: p.state ?? "running",
      });
    } else {
      row.state = p.state ?? row.state;
      row.label = label;
    }
    const current = view.models.find((m) => m.model_id === p.model_id);
    if (current && p.state === "done") {
      current.findings = p.findings ?? current.findings;
      current.rejected = p.rejected ?? current.rejected;
      current.seconds = p.seconds ?? current.seconds;
    }
    if (current && p.state === "failed")
      current.error = p.error ?? current.error;
  }
  if (stage === "started" && p.reviewers) {
    view.models = p.reviewers.map((r) => ({
      model_id: r.model_id,
      label: r.label,
      state: "running",
    }));
  }
  if (p.estimate) view.estimate = p.estimate;
  if (stage === "completed" && p.report) {
    view.report = p.report;
    view.reviewId = p.report.review_id || view.reviewId;
    view.selected = new Set<string>();
  }
  if (stage === "updated" && p.report) view.report = p.report;
  if (stage === "stale" && p.report) view.report = p.report;

  clearReviewCard(ctx);
  const card = reviewCardNode(ctx, view);
  ctx.chatBody.appendChild(card);
  ctx.reviewCard = card;
  scrollBottom(ctx);
  if (ctx === activeCtx) {
    if (phase === "results")
      setActivity(
        `review: ${view.report?.verdict_label ?? "done"} (${view.report?.findings?.length ?? 0} finding(s))`,
      );
    else if (phase === "failed") setActivity("the review did not finish");
    else if (phase === "running") setActivity("reviewing the change…");
  }
}

export function openModelPicker(ctx: Ctx): void {
  const usable = providerGroups.filter(
    (g) => !g.disabled && g.models.some((m) => !m.disabled),
  );
  if (!usable.length) {
    setActivity("no other model to switch to - open Settings to add one");
    return;
  }
  const groups = usable
    .map((group) => {
      const rows = group.models
        .filter((m) => !m.disabled)
        .map((model) => {
          const entry = providerById.get(model.id);
          if (!entry) return "";
          const isCur = entry.id === ctx.providerId;
          return (
            `<button class="pick-item${isCur ? " selected" : ""}" data-model="${escapeHtml(entry.id)}">` +
            `<span class="pm-name">${escapeHtml(entry.name || entry.model)}</span>` +
            `<span class="pm-sub">${escapeHtml(modelSubtitle(entry))}</span></button>`
          );
        })
        .join("");
      return `<div class="pm-group"><div class="pm-group-name">${escapeHtml(group.name)}</div>${rows}</div>`;
    })
    .join("");
  const failed = ctx.failure?.retry;
  const hint = failed
    ? `<p class="perm-why">The request that failed can be retried with the model you pick. Your session, history and draft are untouched.</p>`
    : "";
  showModal(
    `<h3>Switch model</h3>${hint}<div class="pick-list pm-list">${groups}</div><div class="modal-actions"><button class="btn" id="pmCancel">Cancel</button></div>`,
  );
  modalBox
    .querySelectorAll<HTMLButtonElement>("[data-model]")
    .forEach((btn) => {
      btn.onclick = () => {
        const id = btn.dataset["model"] ?? "";
        const entry = providerById.get(id);
        hideModal();
        const changed = id !== ctx.providerId;
        selectModel(ctx, id, { adoptEffort: true });
        clearRetryCard(ctx);
        if (ctx === activeCtx)
          setActivity(
            `switched to ${entry?.name ?? id}${changed ? "" : " (already selected)"}`,
          );
        if (failed) {
          const card = Array.from(
            ctx.chatBody.querySelectorAll<HTMLElement>(".error"),
          ).pop();
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
