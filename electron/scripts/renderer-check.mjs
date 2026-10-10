/* End-to-end check of the two rendering rules around a failed request: the
   error card must survive the post-turn transcript reconcile, and deleting a
   session must close the tab that had it open (moving to another tab, or back
   to the default screen when it was the last one).

   Drives the real Electron renderer over the DevTools Protocol against a
   provider whose endpoint is dead, so the retry backoff and the final error
   card are produced by the real engine. Writes a report to REPORT.

   WS_ROOT / APP_DATA / CDP_PORT / APP_PORT / REPORT override the defaults.
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

/* A dead endpoint plus the key that makes the engine actually dial it. */
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
  say(`backend: connected on port ${APP_PORT}`);

  const snapshot = () => evaluate(ws, `(() => {
    const cards = [...document.querySelectorAll("#chat .error")];
    const retry = [...document.querySelectorAll("#chat .retry")];
    return {
      errors: cards.map((c) => c.textContent.replace(/\\s+/g, " ").trim().slice(0, 200)),
      retryCards: retry.map((c) => c.querySelector(".r-sub")?.textContent ?? ""),
    };
  })()`);

  await evaluate(ws, `(() => {
    const entry = document.getElementById("entry");
    entry.value = "hi";
    entry.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("sendstop").click();
    return true;
  })()`);
  say("submitted 'hi' against the dead endpoint");

  const ticks = [];
  for (let i = 0; i < 16; i++) {
    await sleep(1000);
    const state = await snapshot();
    if (state.retryCards.length) ticks.push(state.retryCards[state.retryCards.length - 1]);
    if (state.errors.length && ticks.length) break;
  }
  const ticked = ticks.length >= 2 && new Set(ticks).size >= 2;
  say(`retry card progress: ${JSON.stringify(ticks.slice(-3))}`);
  say(`VERDICT retry-card-ticks: ${ticked}`);

  let final = { errors: [] };
  for (let i = 0; i < 10; i++) {
    await sleep(1000);
    final = await snapshot();
    if (final.errors.length) break;
  }
  const survives = final.errors.length > 0;
  say(`error card: ${final.errors[0] ?? "(none)"}`);
  say(`VERDICT error-card-survives-the-reconcile: ${survives}`);
  pass = pass && ticked && survives;

  const tabs = () => evaluate(ws, `(() => ({
    tabs: [...document.querySelectorAll("#tabs .tab")].map((t) => ({
      title: t.querySelector(".tab-title")?.textContent ?? "",
      active: t.classList.contains("active"),
    })),
    landingVisible: !document.getElementById("landing")?.classList.contains("hidden"),
  }))()`);
  const before = await tabs();
  say(`tabs before delete: ${JSON.stringify(before.tabs)}`);

  await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
  await sleep(800);
  const two = await tabs();
  const victim = two.tabs.find((t) => !t.active) ?? two.tabs[0];
  await evaluate(ws, `(() => {
    const row = [...document.querySelectorAll(".session-item")]
      .find((r) => (r.querySelector(".s-title")?.textContent ?? "").includes(${JSON.stringify(victim.title.slice(0, 18))}));
    row?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    return Boolean(row);
  })()`);
  await sleep(200);
  await evaluate(ws, `document.querySelector("#contextMenu [data-act='delete']")?.click()`);
  await sleep(200);
  await evaluate(ws, `document.getElementById("delOk")?.click()`);
  await sleep(1200);
  const after = await tabs();
  const closed = !after.tabs.some((t) => t.title === victim.title);
  const switched = after.tabs.length === 0 ? after.landingVisible : after.tabs.some((t) => t.active);
  say(`after deleting the tab's own session: ${JSON.stringify(after.tabs)}`);
  say(`VERDICT deleted-session-closes-its-tab: ${closed}`);
  say(`VERDICT another-tab-or-landing-is-active: ${switched}`);

  await evaluate(ws, `(() => {
    const entry = document.getElementById("entry");
    entry.value = "hi again";
    entry.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("sendstop").click();
    return true;
  })()`);
  await sleep(9000);
  await evaluate(ws, `(() => {
    const row = document.querySelector(".session-item.active");
    if (row) row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    return Boolean(row);
  })()`);
  await sleep(200);
  await evaluate(ws, `document.querySelector("#contextMenu [data-act='delete']")?.click()`);
  await sleep(200);
  await evaluate(ws, `document.getElementById("delOk")?.click()`);
  await sleep(1200);
  const last = await tabs();
  const landing = last.tabs.length === 0 && last.landingVisible;
  say(`after deleting the last tab's own session: tabs=${JSON.stringify(last.tabs)} landing=${last.landingVisible}`);
  say(`VERDICT last-tab-returns-to-default-screen: ${landing}`);
  pass = pass && closed && switched && landing;

  ws.close();
} catch (err) {
  say(`FAILED: ${err.message}`);
  pass = false;
} finally {
  try { child.kill("SIGKILL"); } catch { /* already gone */ }
  cleanupMine();
  await sleep(300);
}
say(`RESULT: ${pass ? "PASS" : "FAIL"}`);
fs.mkdirSync(path.dirname(REPORT), { recursive: true });
fs.writeFileSync(REPORT, lines.join("\n") + "\n", "utf-8");
say(`report: ${REPORT}`);
process.exit(pass ? 0 : 1);
