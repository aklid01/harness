/* End-to-end checks for the failure card, the session library and the
   workspace list, against a provider whose endpoint is dead so the retry
   backoff and the error card come from the real engine.

   Drives the real Electron renderer over the DevTools Protocol. Writes a
   pass/fail report to REPORT. WS_ROOT / APP_DATA / CDP_PORT / APP_PORT /
   REPORT override the defaults.
*/
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const TMP = path.join(os.tmpdir(), "harness-renderer-check");
const WS_ROOT = process.env.WS_ROOT ?? path.join(TMP, "workspace");
const APP_DATA = process.env.APP_DATA ?? path.join(TMP, "app");
const CDP_PORT = Number(process.env.CDP_PORT ?? 9333);
const APP_PORT = Number(process.env.APP_PORT ?? 8901);
const REPORT = process.env.REPORT ?? path.join(ROOT, "tests", "renderer-check.txt");
const DEAD = "http://127.0.0.1:9/v1";

const lines = [];
const say = (text) => { lines.push(text); console.log(text); };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function prepareFixtures() {
  fs.rmSync(TMP, { recursive: true, force: true });
  fs.mkdirSync(WS_ROOT, { recursive: true });
  fs.mkdirSync(APP_DATA, { recursive: true });
  fs.writeFileSync(path.join(WS_ROOT, ".env"), `BASE_URL=${DEAD}\nAPI_KEY=sk-dead-endpoint-check\nMODEL=dead-model\n`, "utf-8");
  fs.writeFileSync(path.join(WS_ROOT, "README.md"), "Throwaway workspace for the renderer check.\n", "utf-8");
  fs.writeFileSync(path.join(APP_DATA, "providers.json"), JSON.stringify({
    version: 2,
    active_id: "mdl-dead",
    active_provider_id: DEAD,
    providers: [{
      id: DEAD, name: "Dead Local", base_url: DEAD, headers: {}, disabled: false,
      models: [{ name: "Dead Local", model: "dead-model", reasoning_effort: null,
                 temperature: null, context_window: null, max_output_tokens: null,
                 supports_tools: true, supports_reasoning: false,
                 supports_text_tools: false, extra_params: {}, disabled: false,
                 id: "mdl-dead" }],
    }],
  }, null, 2), "utf-8");
}

function cleanupMine() {
  spawnSync("powershell", ["-NoProfile", "-Command",
    "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'electron.exe' -and $_.CommandLine -match 'harness\\electron') -or ($_.Name -match 'python|uv' -and $_.CommandLine -match 'harness-renderer-check') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"], { stdio: "ignore" });
}

let nextId = 1;
function rpc(ws, method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout: ${method}`)), 30000);
    const onMessage = (raw) => {
      let msg;
      try { msg = JSON.parse(String(raw.data)); } catch { return; }
      if (msg.id !== id) return;
      clearTimeout(timer);
      ws.removeEventListener("message", onMessage);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    };
    ws.addEventListener("message", onMessage);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(ws, expression) {
  const result = await rpc(ws, "Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) {
    throw new Error(`page error: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`);
  }
  return result.result.value;
}

async function pageTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch { /* not listening yet */ }
    await sleep(500);
  }
  return null;
}

prepareFixtures();
cleanupMine();
const child = spawn(path.join(ROOT, "electron", "node_modules", "electron", "dist", "electron.exe"),
  [path.join(ROOT, "electron"), `--remote-debugging-port=${CDP_PORT}`], {
    cwd: path.join(ROOT, "electron"),
    env: (() => {
      const env = { ...process.env };
      delete env.ELECTRON_RUN_AS_NODE;
      delete env.NODE_OPTIONS;
      env.HARNESS_PORT = String(APP_PORT);
      env.HARNESS_PROJECT_ROOT = WS_ROOT;
      env.HARNESS_APP_DATA = APP_DATA;
      return env;
    })(),
    stdio: ["ignore", "ignore", "ignore"],
  });

const results = [];
const verdict = (name, ok, detail = "") => {
  results.push({ name, ok });
  say(`VERDICT ${name}: ${ok}${detail ? ` (${detail})` : ""}`);
};
const submit = (ws, text) => evaluate(ws, `(() => {
  const entry = document.getElementById("entry");
  entry.value = ${JSON.stringify(text)};
  entry.dispatchEvent(new Event("input", { bubbles: true }));
  document.getElementById("sendstop").click();
  return true;
})()`);
const snapshot = (ws) => evaluate(ws, `(() => {
  const cards = [...document.querySelectorAll("#chat .error")];
  return {
    errors: cards.map((c) => c.textContent.replace(/\\s+/g, " ").trim().slice(0, 200)),
    hints: cards.map((c) => c.querySelector(".error-hint")?.textContent ?? ""),
    retryCards: [...document.querySelectorAll("#chat .retry")].map((c) => c.querySelector(".r-sub")?.textContent ?? ""),
  };
})()`);
const tabs = (ws) => evaluate(ws, `(() => ({
  tabs: [...document.querySelectorAll("#tabs .tab")].map((t) => t.querySelector(".tab-title")?.textContent ?? ""),
  landingVisible: !document.getElementById("landing")?.classList.contains("hidden"),
}))()`);
const sessionRows = (ws) => evaluate(ws, `[...document.querySelectorAll("#wsTree .session-item")].map((r) => r.querySelector(".s-title")?.textContent ?? "")`);

let pass = true;
try {
  const target = await pageTarget();
  if (!target) throw new Error("no CDP page target - did the app start?");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("cdp socket failed")), { once: true });
  });

  for (let i = 0; i < 60; i++) {
    const state = await evaluate(ws, `document.getElementById("connText")?.textContent ?? ""`);
    if (String(state).includes("connected")) break;
    await sleep(500);
  }

  /* Item 6: the plan/context panel is closed by default. */
  const panel = await evaluate(ws, `({
    hidden: document.getElementById("shell").classList.contains("hide-right"),
    toggleActive: document.getElementById("toggleRight").classList.contains("active"),
  })`);
  verdict("plan-panel-closed-by-default", panel.hidden && !panel.toggleActive, JSON.stringify(panel));

  /* Two turns against the dead endpoint: the first one fails hard, the second
     runs in a second session, so there is something to switch between. */
  await submit(ws, "first turn");
  let ticks = [];
  let final = { errors: [], hints: [] };
  for (let i = 0; i < 16; i++) {
    await sleep(1000);
    const state = await snapshot(ws);
    if (state.retryCards.length) ticks.push(state.retryCards[state.retryCards.length - 1]);
    if (state.errors.length) { final = state; break; }
  }
  const ticked = new Set(ticks).size >= 2;
  verdict("retry-card-ticks", ticked, JSON.stringify(ticks.slice(-2)));
  verdict("error-card-shown", final.errors.length > 0, (final.errors[0] ?? "").slice(0, 90));
  verdict("error-suggests-next-step", final.hints.some((h) => h.length > 5), JSON.stringify(final.hints));

  /* Item 5: the deleted session leaves the list immediately. */
  await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
  await sleep(600);
  const beforeSecond = await sessionRows(ws);
  await submit(ws, "second turn");
  await sleep(9000);
  const withTwo = await sessionRows(ws);
  const victim = beforeSecond[0];
  await evaluate(ws, `(() => {
    const row = [...document.querySelectorAll(".session-item")]
      .find((r) => (r.querySelector(".s-title")?.textContent ?? "").includes(${JSON.stringify(victim?.slice(0, 18) ?? "first")}));
    row?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    return Boolean(row);
  })()`);
  await sleep(200);
  await evaluate(ws, `document.querySelector("#contextMenu [data-act='delete']")?.click()`);
  await sleep(200);
  await evaluate(ws, `document.getElementById("delOk")?.click()`);
  await sleep(1500);
  const afterDelete = await sessionRows(ws);
  const goneImmediately = !afterDelete.includes(victim);
  verdict("deleted-session-leaves-the-list", goneImmediately,
    `before=${JSON.stringify(withTwo)} after=${JSON.stringify(afterDelete)}`);

  /* Item 2: the failure survives switching sessions. */
  await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
  await sleep(600);
  await submit(ws, "third turn");
  await sleep(9000);
  await evaluate(ws, `(() => {
    const rows = [...document.querySelectorAll(".session-item")];
    const other = rows.find((r) => !r.classList.contains("active"));
    other?.click();
    return Boolean(other);
  })()`);
  await sleep(1500);
  const afterSwitch = await snapshot(ws);
  verdict("error-card-survives-session-switch", afterSwitch.errors.length > 0,
    (afterSwitch.errors[0] ?? "").slice(0, 80));

  /* Items 1 + 4: with every tab closed, sessions are still deletable, and the
     workspace itself can be removed from the list. */
  for (let i = 0; i < 5; i++) {
    const closed = await evaluate(ws, `(() => { const btn = document.querySelector("#tabs .tab .tab-close"); if (btn) btn.click(); return Boolean(btn); })()`);
    if (!closed) break;
    await sleep(400);
  }
  const noTabs = await tabs(ws);
  verdict("closing-every-tab-lands", noTabs.tabs.length === 0 && noTabs.landingVisible, JSON.stringify(noTabs));

  const orphan = await sessionRows(ws);
  if (orphan.length) {
    await evaluate(ws, `(() => {
      const row = document.querySelector(".session-item");
      row?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
      return Boolean(row);
    })()`);
    await sleep(200);
    await evaluate(ws, `document.querySelector("#contextMenu [data-act='delete']")?.click()`);
    await sleep(200);
    await evaluate(ws, `document.getElementById("delOk")?.click()`);
    await sleep(1500);
    const orphansAfter = await sessionRows(ws);
    verdict("session-deletable-without-opening", orphansAfter.length < orphan.length,
      `before=${orphan.length} after=${orphansAfter.length}`);
  }

  const wsRowsBefore = await evaluate(ws, `[...document.querySelectorAll("#wsTree .ws-row")].map((r) => r.getAttribute("title") ?? "")`);
  if (wsRowsBefore.length) {
    const targetRoot = wsRowsBefore[0];
    await evaluate(ws, `(() => {
      const row = document.querySelector("#wsTree .ws-row");
      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
      return true;
    })()`);
    await sleep(200);
    await evaluate(ws, `document.querySelector("#contextMenu [data-act='remove']")?.click()`);
    await sleep(200);
    await evaluate(ws, `document.getElementById("wsRemove")?.click()`);
    await sleep(800);
    const afterRemove = await evaluate(ws, `[...document.querySelectorAll("#wsTree .ws-row")].map((r) => r.getAttribute("title") ?? "")`);
    verdict("workspace-removable",
      !afterRemove.includes(targetRoot) && afterRemove.length === wsRowsBefore.length - 1,
      `rows=${wsRowsBefore.length}->${afterRemove.length}`);
    verdict("folder-still-on-disk", fs.existsSync(path.join(WS_ROOT, "README.md")));
  }

  ws.close();
} catch (err) {
  say(`FAILED: ${err.message}`);
  results.push({ name: "harness", ok: false });
} finally {
  try { child.kill("SIGKILL"); } catch { /* already gone */ }
  cleanupMine();
  await sleep(300);
}

pass = results.length > 0 && results.every((r) => r.ok);
say(`RESULT: ${pass ? "PASS" : "FAIL"} (${results.filter((r) => r.ok).length}/${results.length})`);
fs.mkdirSync(path.dirname(REPORT), { recursive: true });
fs.writeFileSync(REPORT, lines.join("\n") + "\n", "utf-8");
say(`report: ${REPORT}`);
process.exit(pass ? 0 : 1);
