/* End-to-end checks for the failure card, the session library, the
   workspace list, and the mid-turn steer/queue composer, against two
   providers: a dead endpoint so the retry backoff and the error card come
   from the real engine, and a live scripted SSE endpoint so a real turn can
   run (steer drain, queue chain, reasoning stream).

   Drives the real Tauri renderer over the DevTools Protocol (WebView2 remote
   debugging). Builds the web assets first, and the debug exe when missing.
   Writes a pass/fail report to REPORT. WS_ROOT / APP_DATA / CDP_PORT /
   APP_PORT / REPORT override the defaults.
*/
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..", "..");
const TMP = path.join(os.tmpdir(), "harness-renderer-check");
const WS_ROOT = process.env.WS_ROOT ?? path.join(TMP, "workspace");
const APP_DATA = process.env.APP_DATA ?? path.join(TMP, "app");
const USER_DATA = process.env.USER_DATA ?? path.join(TMP, "userdata");
const CDP_PORT = Number(process.env.CDP_PORT ?? 9333);
const APP_PORT = Number(process.env.APP_PORT ?? 8901);
const REPORT =
  process.env.REPORT ?? path.join(ROOT, "tests", "renderer-check.txt");

/* One scripted endpoint plays both providers, so a single .env key is borrowed
   by both groups (the engine only borrows the plaintext key for an exact
   base_url match). Requests for the dead model get their socket destroyed,
   which is exactly the connection error the retry card and error card need;
   requests for the live model run the scenario. Request 1 reasons then calls
   read_file, request 2 streams slowly (the window where pending strips are
   edited), request 3+ answer fast (the queued turn and the title call). */
const liveState = { requests: [], server: null };
function startLiveFixture() {
  const server = http.createServer((req, res) => {
    if (req.method !== "POST") {
      res.writeHead(404).end();
      return;
    }
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      let payload = {};
      try {
        payload = JSON.parse(body || "{}");
      } catch {
        payload = {};
      }
      if ((payload.model || "") === "dead-model") {
        req.socket.destroy();
        return;
      }
      liveState.requests.push(payload);
      const n = liveState.requests.length;
      if (!payload.stream) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            id: "chatcmpl-live",
            object: "chat.completion",
            created: 1,
            model: "live-model",
            choices: [
              {
                index: 0,
                message: { role: "assistant", content: "ok" },
                finish_reason: "stop",
              },
            ],
            usage: { prompt_tokens: 10, completion_tokens: 5 },
          }),
        );
        return;
      }
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
      const usage = { prompt_tokens: 10, completion_tokens: 20 };
      if (n === 1) {
        send({
          choices: [
            {
              index: 0,
              delta: {
                role: "assistant",
                reasoning_content: "Let me look at the workspace before acting.",
              },
            },
          ],
        });
        setTimeout(() => {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_1",
                      type: "function",
                      function: { name: "read_file", arguments: "" },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: 0,
                      function: {
                        arguments: JSON.stringify({
                          path: path.join(WS_ROOT, "README.md"),
                        }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
        }, 1500);
      } else if (n === 2) {
        const text =
          "The readme says this workspace is a throwaway fixture for the renderer check, " +
          "and I have now read it end to end, so here is the summary you asked for, word by word. ";
        let i = 0;
        const timer = setInterval(() => {
          if (i >= text.length) {
            clearInterval(timer);
            send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
            send({ choices: [], usage });
            res.write("data: [DONE]\n\n");
            res.end();
            return;
          }
          send({
            choices: [{ index: 0, delta: { content: text.slice(i, i + 8) } }],
          });
          i += 8;
        }, 100);
      } else {
        send({
          choices: [
            { index: 0, delta: { role: "assistant", content: "queued turn complete" } },
          ],
        });
        send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
        send({ choices: [], usage });
        res.write("data: [DONE]\n\n");
        res.end();
      }
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      liveState.server = server;
      resolve(server.address().port);
    });
  });
}

const lines = [];
/* The report is read wherever it lands, so keep it ASCII: the card text uses
   typographic characters that survive a console but not a plain-text diff. */
const plain = (text) =>
  text
    .replace(/·/g, "-")
    .replace(/…/g, "...")
    .replace(/[^\x20-\x7e\n]/g, "?");
const say = (text) => {
  lines.push(plain(text));
  console.log(text);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const LIVE_PORT = await startLiveFixture();
const ENDPOINT = `http://127.0.0.1:${LIVE_PORT}/v1`;

function prepareFixtures() {
  fs.rmSync(TMP, { recursive: true, force: true });
  fs.mkdirSync(WS_ROOT, { recursive: true });
  fs.mkdirSync(APP_DATA, { recursive: true });
  fs.writeFileSync(
    path.join(WS_ROOT, ".env"),
    `BASE_URL=${ENDPOINT}\nAPI_KEY=sk-renderer-check\nMODEL=dead-model\n`,
    "utf-8",
  );
  fs.writeFileSync(
    path.join(WS_ROOT, "README.md"),
    "Throwaway workspace for the renderer check.\n",
    "utf-8",
  );
  fs.writeFileSync(
    path.join(APP_DATA, "providers.json"),
    JSON.stringify(
      {
        version: 2,
        active_id: "mdl-dead",
        active_provider_id: "grp-dead",
        providers: [
          {
            id: "grp-dead",
            name: "Dead Local",
            base_url: ENDPOINT,
            headers: {},
            disabled: false,
            models: [
              {
                name: "Dead Local",
                model: "dead-model",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: false,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-dead",
              },
            ],
          },
          {
            id: "grp-live",
            name: "Live Local",
            base_url: ENDPOINT,
            headers: {},
            disabled: false,
            models: [
              {
                name: "Live SSE",
                model: "live-model",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: true,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-live",
              },
            ],
          },
        ],
      },
      null,
      2,
    ),
    "utf-8",
  );
}

function cleanupMine() {
  spawnSync(
    "powershell",
    [
      "-NoProfile",
      "-Command",
      "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'harness-desktop.exe') -or ($_.Name -match 'python|uv' -and $_.CommandLine -match 'harness-renderer-check') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ],
    { stdio: "ignore" },
  );
}

let nextId = 1;
function rpc(ws, method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`timeout: ${method}`)),
      30000,
    );
    const onMessage = (raw) => {
      let msg;
      try {
        msg = JSON.parse(String(raw.data));
      } catch {
        return;
      }
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
  const result = await rpc(ws, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(
      `page error: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`,
    );
  }
  return result.result.value;
}

async function pageTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (
        await fetch(`http://127.0.0.1:${CDP_PORT}/json`)
      ).json();
      const page = list.find(
        (t) => t.type === "page" && t.webSocketDebuggerUrl,
      );
      if (page) return page;
    } catch {
      /* not listening yet */
    }
    await sleep(500);
  }
  return null;
}

prepareFixtures();
cleanupMine();

const EXE = path.join(
  ROOT,
  "desktop",
  "src-tauri",
  "target",
  "debug",
  "harness-desktop.exe",
);

function buildIfNeeded() {
  const web = spawnSync(process.execPath, [path.join(ROOT, "desktop", "scripts", "build-web.mjs")], {
    cwd: path.join(ROOT, "desktop"),
    stdio: "inherit",
  });
  if (web.status !== 0) throw new Error("build:web failed");
  // frontendDist is embedded into the exe at build time, so an existing exe
  // can be stale. cargo build is incremental, so always run it.
  const built = spawnSync("cargo", ["build"], {
    cwd: path.join(ROOT, "desktop", "src-tauri"),
    stdio: "inherit",
  });
  if (built.status !== 0) throw new Error("cargo build failed");
  if (!fs.existsSync(EXE)) throw new Error(`sidecar shell exe missing: ${EXE}`);
}
buildIfNeeded();

const child = spawn(EXE, [], {
  cwd: path.join(ROOT, "desktop"),
  env: (() => {
    const env = { ...process.env };
    env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = `--remote-debugging-port=${CDP_PORT}`;
    // A dedicated WebView2 profile: the real one carries the user's panel
    // prefs and localStorage, which leaked straight into the verdicts.
    env.WEBVIEW2_USER_DATA_FOLDER = path.join(TMP, "webview2");
    // Shell state (recents, last workspace) and engine data both isolated.
    env.HARNESS_DATA_ROOT = APP_DATA;
    env.HARNESS_PORT = String(APP_PORT);
    env.HARNESS_PROJECT_ROOT = WS_ROOT;
    env.HARNESS_APP_DATA = APP_DATA;
    env.HARNESS_SESSIONS_ROOT = path.join(TMP, "sessions");
    return env;
  })(),
  stdio: ["ignore", "ignore", "ignore"],
});

const results = [];
const verdict = (name, ok, detail = "") => {
  results.push({ name, ok });
  say(`VERDICT ${name}: ${ok}${detail ? ` (${detail})` : ""}`);
};
const submit = (ws, text) =>
  evaluate(
    ws,
    `(() => {
  const entry = document.getElementById("entry");
  entry.value = ${JSON.stringify(text)};
  entry.dispatchEvent(new Event("input", { bubbles: true }));
  document.getElementById("sendstop").click();
  return true;
})()`,
  );
const snapshot = (ws) =>
  evaluate(
    ws,
    `(() => {
  const cards = [...document.querySelectorAll("#chat .error")];
  return {
    errors: cards.map((c) => c.textContent.replace(/\\s+/g, " ").trim().slice(0, 200)),
    hints: cards.map((c) => c.querySelector(".error-hint")?.textContent ?? ""),
    retryCards: [...document.querySelectorAll("#chat .retry")].map((c) => c.querySelector(".r-sub")?.textContent ?? ""),
  };
})()`,
  );
const tabs = (ws) =>
  evaluate(
    ws,
    `(() => ({
  tabs: [...document.querySelectorAll("#tabs .tab")].map((t) => t.querySelector(".tab-title")?.textContent ?? ""),
  landingVisible: !document.getElementById("landing")?.classList.contains("hidden"),
}))()`,
  );
const sessionRows = (ws) =>
  evaluate(
    ws,
    `[...document.querySelectorAll("#wsTree .session-item")].map((r) => r.querySelector(".s-title")?.textContent ?? "")`,
  );

let pass = true;
try {
  const target = await pageTarget();
  if (!target) throw new Error("no CDP page target - did the app start?");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("cdp socket failed")), {
      once: true,
    });
  });

  for (let i = 0; i < 120; i++) {
    const state = await evaluate(
      ws,
      `document.getElementById("connText")?.textContent ?? ""`,
    );
    // startsWith, not includes: "disconnected - retrying..." contains the
    // substring "connected" and broke the wait on the first poll, so the
    // first submit fired before the socket was up and was dropped silently.
    if (String(state).startsWith("connected")) break;
    await sleep(500);
  }

  /* Item 6: the plan/context panel is closed by default. */
  const panel = await evaluate(
    ws,
    `({
    hidden: document.getElementById("shell").classList.contains("hide-right"),
    toggleActive: document.getElementById("toggleRight").classList.contains("active"),
  })`,
  );
  verdict(
    "plan-panel-closed-by-default",
    panel.hidden && !panel.toggleActive,
    JSON.stringify(panel),
  );

  /* Item 7: the mode menu offers Ask, and picking it updates the composer
     label. Build is restored afterwards so later items run in the default
     workflow. */
  const pickMode = (name) =>
    evaluate(
      ws,
      `(() => {
    const btn = [...document.querySelectorAll("#modeMenu button")].find(
      (b) => b.querySelector(".m-label")?.textContent.trim() === ${JSON.stringify(name)},
    );
    if (!btn) return false;
    btn.click();
    return true;
  })()`,
    );
  await evaluate(ws, `document.getElementById("modeBtn")?.click()`);
  await sleep(200);
  const menu = await evaluate(
    ws,
    `(() => ({
    labels: [...document.querySelectorAll("#modeMenu button .m-label")].map((s) => s.textContent.trim()),
    open: !document.getElementById("modeMenu").classList.contains("hidden"),
  }))()`,
  );
  const pickedAsk = await pickMode("Ask");
  await sleep(200);
  const askLabel = await evaluate(
    ws,
    `document.getElementById("modeLabel")?.textContent ?? ""`,
  );
  verdict(
    "mode-menu-offers-ask",
    menu.open && menu.labels.includes("Ask") && pickedAsk && askLabel === "Ask",
    JSON.stringify({ labels: menu.labels, label: askLabel, pickedAsk }),
  );
  await evaluate(ws, `document.getElementById("modeBtn")?.click()`);
  await sleep(150);
  await pickMode("Build");
  await sleep(150);

  /* The Storage tab is driven entirely by the Electron main process, so it is
     checked before the turns: it must pass even when the engine never starts. */
  await evaluate(ws, `document.getElementById("btnSettings")?.click()`);
  await sleep(400);
  await evaluate(
    ws,
    `document.querySelector(".settings-nav [data-nav='storage']")?.click()`,
  );
  let storage = { rows: 0, labels: [], dataFolder: "", sessions: "" };
  for (let i = 0; i < 10 && !storage.rows; i++) {
    await sleep(400);
    storage = await evaluate(
      ws,
      `(() => {
      const rows = [...document.querySelectorAll("#storageBody .set-row")];
      const labelOf = (row) => row.querySelector(".set-label")?.textContent ?? "";
      const sessionsRow = rows.find((r) => labelOf(r).startsWith("Sessions"));
      return {
        rows: rows.length,
        labels: rows.map(labelOf),
        dataFolder: rows[0]?.querySelector(".set-desc[title]")?.getAttribute("title") ?? "",
        sessions: sessionsRow?.querySelector(".set-desc[title]")?.getAttribute("title") ?? "",
        canChange: Boolean(document.getElementById("storageChange")),
      };
    })()`,
    );
  }
  verdict(
    "storage-tab-lists-locations",
    storage.rows >= 3 &&
      storage.labels[0]?.startsWith("Data folder") &&
      storage.dataFolder.length > 0 &&
      storage.sessions.startsWith(storage.dataFolder) &&
      storage.canChange,
    JSON.stringify(storage),
  );
  await evaluate(
    ws,
    `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`,
  );
  await sleep(300);

  /* Two turns against the dead endpoint: the first one fails hard, the second
     runs in a second session, so there is something to switch between. The
     final error card lands only after the retry schedule is spent (3+5+15+
     20+30s), so the poll has to span all of it. */
  await submit(ws, "first turn");

  /* The mode control locks while the turn runs: disabled, with the reason on
     hover, and a forced click cannot open the menu. */
  await sleep(600);
  const locked = await evaluate(
    ws,
    `(() => ({
    disabled: document.getElementById("modeBtn").disabled,
    title: document.getElementById("modeBtn").title,
    menuHidden: document.getElementById("modeMenu").classList.contains("hidden"),
  }))()`,
  );
  await evaluate(ws, `document.getElementById("modeBtn").click()`);
  await sleep(150);
  const stillHidden = await evaluate(
    ws,
    `document.getElementById("modeMenu").classList.contains("hidden")`,
  );
  verdict(
    "mode-control-locks-mid-turn",
    locked.disabled &&
      locked.menuHidden &&
      stillHidden &&
      locked.title.includes("Locked"),
    JSON.stringify({ ...locked, stillHidden }),
  );

  let ticks = [];
  let final = { errors: [], hints: [] };
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const state = await snapshot(ws);
    if (state.retryCards.length)
      ticks.push(state.retryCards[state.retryCards.length - 1]);
    if (state.errors.length) {
      final = state;
      break;
    }
  }
  const ticked = new Set(ticks).size >= 2;
  verdict("retry-card-ticks", ticked, JSON.stringify(ticks.slice(-2)));
  // Isolation guard: the error card must name the fixture's dead provider.
  // Anything else means the renderer dialed a sidecar that is not ours.
  verdict(
    "engine-is-the-fixture",
    final.errors.some((e) => e.includes("Dead Local")),
    JSON.stringify(final.errors.slice(0, 1)),
  );
  /* The lock releases once the turn is over (here: failed), so the next
     message can start in a different mode. */
  let released = false;
  for (let i = 0; i < 12 && !released; i++) {
    await sleep(500);
    released = await evaluate(
      ws,
      `!document.getElementById("modeBtn").disabled`,
    );
  }
  verdict("mode-control-releases-after-the-turn", released, "");
  verdict(
    "error-card-shown",
    final.errors.length > 0,
    (final.errors[0] ?? "").slice(0, 90),
  );
  verdict(
    "error-suggests-next-step",
    final.hints.some((h) => h.length > 5),
    JSON.stringify(final.hints),
  );

  /* Shared helpers: the sidebar rows, the tab labels, a new session in a new
     tab, and a right-click row that can be renamed or deleted. */
  const tabLabels = () =>
    evaluate(
      ws,
      `[...document.querySelectorAll("#tabs .tab .tab-title")].map((t) => t.textContent ?? "")`,
    );
  const newSession = async (label) => {
    await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
    await sleep(500);
    await submit(ws, label);
    await sleep(9000);
  };
  const renameRow = (title) =>
    evaluate(
      ws,
      `(() => {
    const row = [...document.querySelectorAll(".session-item")]
      .find((r) => (r.querySelector(".s-title")?.textContent ?? "").includes(${JSON.stringify(title)}));
    if (!row) return false;
    row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    return true;
  })()`,
    );
  const menuAction = (action) =>
    evaluate(
      ws,
      `(() => {
    const btn = document.querySelector("#contextMenu [data-act='${action}']");
    if (!btn) return false;
    btn.click();
    return true;
  })()`,
    );
  const doRename = (newTitle) =>
    evaluate(
      ws,
      `(() => {
    const input = document.getElementById("renameInput");
    if (!input) return false;
    input.value = ${JSON.stringify(newTitle)};
    document.getElementById("renameSave").click();
    return true;
  })()`,
    );

  await newSession("second turn");

  /* Item 2: the failure outlives switching sessions. Switch to the other tab
     and back to the one that failed. */
  const firstLabel = (await tabLabels())[0] ?? "";
  const secondLabel = (await tabLabels()).find((l) => l !== firstLabel) ?? "";
  await evaluate(
    ws,
    `(() => {
    const tab = [...document.querySelectorAll("#tabs .tab")]
      .find((t) => (t.querySelector(".tab-title")?.textContent ?? "").includes(${JSON.stringify(secondLabel)}));
    tab?.click();
    return Boolean(tab);
  })()`,
  );
  await sleep(1200);
  const awayFrom = await snapshot(ws);
  await evaluate(
    ws,
    `(() => {
    const tab = [...document.querySelectorAll("#tabs .tab")]
      .find((t) => (t.querySelector(".tab-title")?.textContent ?? "").includes(${JSON.stringify(firstLabel)}));
    tab?.click();
    return Boolean(tab);
  })()`,
  );
  await sleep(1200);
  const afterSwitch = await snapshot(ws);
  verdict(
    "error-card-survives-session-switch",
    afterSwitch.errors.length > 0,
    `other=${awayFrom.errors.length} back=${afterSwitch.errors.length}`,
  );

  /* ---- steer / queue / thinking against the live scripted endpoint ---- */
  const waitFor = async (expr, timeout = 30000, poll = 200) => {
    const limit = Date.now() + timeout;
    let last;
    while (Date.now() < limit) {
      last = await evaluate(ws, expr);
      if (last) return last;
      await sleep(poll);
    }
    return last;
  };
  const typeEntry = (text) =>
    evaluate(
      ws,
      `(() => {
    const entry = document.getElementById("entry");
    entry.value = ${JSON.stringify(text)};
    entry.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`,
    );
  const stripState = () =>
    evaluate(
      ws,
      `(() => ({
    visible: !document.getElementById("pendingBar").classList.contains("hidden"),
    items: [...document.querySelectorAll("#pendingBar .pending-item")].map((row) => ({
      badge: row.querySelector(".p-badge")?.textContent ?? "",
      text: row.querySelector(".p-text")?.textContent ?? "",
    })),
  }))()`,
    );

  await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
  await sleep(500);
  // The live model exposes reasoning, so the thinking control appears for it.
  await evaluate(ws, `document.getElementById("providerBtn")?.click()`);
  await sleep(200);
  const pickedLive = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#providerMenu button")].find(
      (b) => b.querySelector(".m-label")?.textContent.trim() === "Live SSE",
    );
    if (!btn) return false;
    btn.click();
    return true;
  })()`,
  );
  await sleep(400);
  const thinkVisible = await evaluate(
    ws,
    `(() => ({
    hidden: document.getElementById("thinkBtn").classList.contains("hidden"),
    on: document.getElementById("thinkBtn").classList.contains("on"),
  }))()`,
  );
  verdict(
    "thinking-toggle-offers-for-reasoning-models",
    pickedLive && !thinkVisible.hidden && thinkVisible.on,
    JSON.stringify({ pickedLive, ...thinkVisible }),
  );

  // The turn reasons, reads a file, then streams slowly: the window in which
  // steer/queue are filed and edited. The wait returns only once the reasoning
  // block exists - an object result is always truthy, so gating matters.
  await submit(ws, "summarize the readme");
  const busyUI = await waitFor(
    `(() => {
    const block = document.querySelector("#chat .think-block");
    if (!block) return false;
    return {
      busy: document.getElementById("promptZone").classList.contains("busy"),
      sendExtraVisible:
        getComputedStyle(document.getElementById("sendExtra")).display !== "none",
      blockVisible: getComputedStyle(block).display !== "none",
    };
  })()`,
  );
  verdict(
    "busy-turn-affords-steer-and-queue",
    busyUI.busy && busyUI.sendExtraVisible,
    JSON.stringify(busyUI),
  );
  verdict(
    "thinking-block-renders-while-streaming",
    Boolean(busyUI.blockVisible),
    JSON.stringify(busyUI),
  );

  // Steer via the caret menu (explicit per-message choice).
  await typeEntry("steer mid-turn");
  await evaluate(ws, `document.getElementById("sendExtraCaret")?.click()`);
  await sleep(200);
  const menuSteer = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#sendExtraMenu button .m-label")].find(
      (l) => l.textContent.includes("Send as steer"),
    );
    if (!btn) return false;
    btn.closest("button").click();
    return true;
  })()`,
  );
  await sleep(300);
  const strip1 = await stripState();
  verdict(
    "steer-files-with-a-badge",
    menuSteer && strip1.visible && strip1.items.some((i) => i.badge === "STEER"),
    JSON.stringify({ menuSteer, ...strip1 }),
  );

  // Queue via the default action (the extra button itself).
  await typeEntry("queued work");
  await evaluate(ws, `document.getElementById("sendExtra")?.click()`);
  await sleep(300);
  const strip2 = await stripState();
  verdict(
    "queue-files-with-a-badge",
    strip2.items.filter((i) => i.badge === "QUEUE").length === 1 &&
      strip2.items.some((i) => i.text === "queued work"),
    JSON.stringify(strip2),
  );

  // Edit the queued message before it is sent.
  await evaluate(
    ws,
    `(() => {
    const row = [...document.querySelectorAll("#pendingBar .pending-item")].find(
      (r) => r.querySelector(".p-text")?.textContent === "queued work",
    );
    row?.querySelector(".p-btn")?.click();
    return Boolean(row);
  })()`,
  );
  await sleep(200);
  await evaluate(
    ws,
    `(() => {
    const area = document.querySelector("#pendingBar .pending-item.editing .p-edit");
    if (!area) return false;
    area.value = "edited queue work";
    area.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`,
  );
  await sleep(100);
  await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#pendingBar .pending-item.editing .p-actions button")].find(
      (b) => b.textContent === "Save",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  const strip3 = await waitFor(
    `(() => {
    const items = [...document.querySelectorAll("#pendingBar .pending-item")].map((row) => ({
      badge: row.querySelector(".p-badge")?.textContent ?? "",
      text: row.querySelector(".p-text")?.textContent ?? "",
    }));
    return items.some((i) => i.text === "edited queue work") ? items : false;
  })()`,
  );
  const editLanded = Array.isArray(strip3);
  verdict("pending-edit-updates-the-strip", editLanded, JSON.stringify(strip3));

  // File a third message and remove it again.
  await typeEntry("remove me");
  await evaluate(ws, `document.getElementById("sendExtra")?.click()`);
  const strip4 = await waitFor(
    `(() => {
    const items = [...document.querySelectorAll("#pendingBar .pending-item .p-text")].map((t) => t.textContent);
    return items.includes("remove me") ? items : false;
  })()`,
  );
  await evaluate(
    ws,
    `(() => {
    const row = [...document.querySelectorAll("#pendingBar .pending-item")].find(
      (r) => r.querySelector(".p-text")?.textContent === "remove me",
    );
    const btns = row ? [...row.querySelectorAll(".p-btn")] : [];
    btns[btns.length - 1]?.click();
    return Boolean(row);
  })()`,
  );
  const strip5 = await waitFor(
    `(() => {
    const items = [...document.querySelectorAll("#pendingBar .pending-item .p-text")].map((t) => t.textContent);
    return !items.includes("remove me") ? items : false;
  })()`,
  );
  // The steer may already have drained by now (it lands at the tool boundary),
  // so only the removal itself is asserted: the item is gone, the rest stays.
  verdict(
    "pending-remove-drops-the-message",
    Array.isArray(strip4) &&
      Array.isArray(strip5) &&
      strip5.length === strip4.length - 1 &&
      strip5.includes("edited queue work"),
    JSON.stringify({ afterFile: strip4, afterRemove: strip5 }),
  );

  // The steer drains at the tool boundary: a user bubble with a steered chip
  // appears mid-turn and the strip item is gone.
  const steerDrained = await waitFor(
    `(() => {
    const chip = document.querySelector("#chat .via-chip.steer");
    return chip ? chip.closest(".msg")?.textContent.includes("steer mid-turn") : false;
  })()`,
    20000,
  );
  const stripAfterDrain = await stripState();
  verdict(
    "steer-drains-into-the-running-turn",
    steerDrained === true &&
      !stripAfterDrain.items.some((i) => i.badge === "STEER"),
    JSON.stringify({ steerDrained, strip: stripAfterDrain }),
  );

  // The thinking toggle hides and restores reasoning blocks in place.
  await evaluate(ws, `document.getElementById("thinkBtn")?.click()`);
  await sleep(200);
  const hiddenNow = await evaluate(
    ws,
    `document.body.classList.contains("hide-thinking") && (() => {
    const block = document.querySelector("#chat .think-block");
    return !block || getComputedStyle(block).display === "none";
  })()`,
  );
  await evaluate(ws, `document.getElementById("thinkBtn")?.click()`);
  await sleep(200);
  const shownAgain = await evaluate(
    ws,
    `(() => {
    const block = document.querySelector("#chat .think-block");
    return Boolean(block) && getComputedStyle(block).display !== "none";
  })()`,
  );
  verdict(
    "thinking-toggle-hides-and-restores",
    hiddenNow && shownAgain,
    JSON.stringify({ hiddenNow, shownAgain }),
  );

  // The queued message auto-runs when the turn ends: a user bubble with a
  // queued chip, then the chain goes idle again.
  const queuedRan = await waitFor(
    `(() => {
    const chips = [...document.querySelectorAll("#chat .via-chip.queue")];
    return chips.some((c) => c.closest(".msg")?.textContent.includes("edited queue work"));
  })()`,
    30000,
  );
  const chainIdle = await waitFor(
    `!document.getElementById("modeBtn").disabled`,
    30000,
  );
  verdict(
    "queue-auto-runs-after-the-turn",
    queuedRan === true && chainIdle === true,
    JSON.stringify({ queuedRan, chainIdle }),
  );

  /* A renamed session takes the new name on its tab at once - the one you
     renamed from, and any other tab that has it open. */
  const labelsBefore = await tabLabels();
  const activeRow = await evaluate(
    ws,
    `document.querySelector(".session-item.active .s-title")?.textContent ?? ""`,
  );
  await renameRow(activeRow);
  await sleep(200);
  await menuAction("rename");
  await doRename("renamed active");
  await sleep(1200);
  const labelsAfterActive = await tabLabels();
  verdict(
    "rename-updates-the-tab-it-was-renamed-from",
    labelsAfterActive.includes("renamed active"),
    JSON.stringify({ before: labelsBefore, after: labelsAfterActive }),
  );

  const otherLabel = labelsBefore.find((l) => l !== activeRow);
  if (otherLabel) {
    await renameRow(otherLabel);
    await sleep(200);
    await menuAction("rename");
    await doRename("renamed other");
    await sleep(1200);
    const labelsAfterOther = await tabLabels();
    verdict(
      "rename-updates-any-open-tab",
      labelsAfterOther.includes("renamed other"),
      JSON.stringify(labelsAfterOther),
    );
  } else {
    verdict("rename-updates-any-open-tab", false, "no second tab to rename");
  }

  /* Item 5: the deleted session leaves the list immediately. It runs after the
     rename checks because deleting a session closes its tab. */
  const rowsBefore = await sessionRows(ws);
  if (rowsBefore.length >= 2) {
    await renameRow(rowsBefore[rowsBefore.length - 1]);
    await sleep(200);
    await menuAction("rename");
    await doRename("removable session");
    await sleep(1200);
    const marked = await sessionRows(ws);
    if (marked.includes("removable session")) {
      await renameRow("removable session");
      await sleep(200);
      await menuAction("delete");
      await sleep(200);
      await evaluate(ws, `document.getElementById("delOk")?.click()`);
      await sleep(1500);
      const afterDelete = await sessionRows(ws);
      verdict(
        "deleted-session-leaves-the-list",
        !afterDelete.includes("removable session") &&
          afterDelete.length === marked.length - 1,
        `before=${marked.length} after=${afterDelete.length}`,
      );
    } else {
      verdict(
        "deleted-session-leaves-the-list",
        false,
        "could not mark a session for deletion",
      );
    }
  } else {
    verdict(
      "deleted-session-leaves-the-list",
      false,
      "no session rows to delete",
    );
  }
  /* Items 1 + 4: with every tab closed, sessions are still deletable, and the
     workspace itself can be removed from the list. */
  for (let i = 0; i < 5; i++) {
    const closed = await evaluate(
      ws,
      `(() => { const btn = document.querySelector("#tabs .tab .tab-close"); if (btn) btn.click(); return Boolean(btn); })()`,
    );
    if (!closed) break;
    await sleep(400);
  }
  const noTabs = await tabs(ws);
  verdict(
    "closing-every-tab-lands",
    noTabs.tabs.length === 0 && noTabs.landingVisible,
    JSON.stringify(noTabs),
  );

  const orphan = await sessionRows(ws);
  if (orphan.length) {
    await evaluate(
      ws,
      `(() => {
      const row = document.querySelector(".session-item");
      row?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
      return Boolean(row);
    })()`,
    );
    await sleep(200);
    await evaluate(
      ws,
      `document.querySelector("#contextMenu [data-act='delete']")?.click()`,
    );
    await sleep(200);
    await evaluate(ws, `document.getElementById("delOk")?.click()`);
    await sleep(1500);
    const orphansAfter = await sessionRows(ws);
    verdict(
      "session-deletable-without-opening",
      orphansAfter.length < orphan.length,
      `before=${orphan.length} after=${orphansAfter.length}`,
    );
  }

  const wsRowsBefore = await evaluate(
    ws,
    `[...document.querySelectorAll("#wsTree .ws-row")].map((r) => r.getAttribute("title") ?? "")`,
  );
  if (wsRowsBefore.length) {
    const targetRoot = wsRowsBefore[0];
    await evaluate(
      ws,
      `(() => {
      const row = document.querySelector("#wsTree .ws-row");
      row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
      return true;
    })()`,
    );
    await sleep(200);
    await evaluate(
      ws,
      `document.querySelector("#contextMenu [data-act='remove']")?.click()`,
    );
    await sleep(200);
    await evaluate(ws, `document.getElementById("wsRemove")?.click()`);
    await sleep(800);
    const afterRemove = await evaluate(
      ws,
      `[...document.querySelectorAll("#wsTree .ws-row")].map((r) => r.getAttribute("title") ?? "")`,
    );
    verdict(
      "workspace-removable",
      !afterRemove.includes(targetRoot) &&
        afterRemove.length === wsRowsBefore.length - 1,
      `rows=${wsRowsBefore.length}->${afterRemove.length}`,
    );
    verdict(
      "folder-still-on-disk",
      fs.existsSync(path.join(WS_ROOT, "README.md")),
    );
  }

  ws.close();
} catch (err) {
  say(`FAILED: ${err.message}`);
  results.push({ name: "harness", ok: false });
} finally {
  try {
    liveState.server?.close();
  } catch {
    /* already gone */
  }
  try {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  } catch {
    /* already gone */
  }
  cleanupMine();
  await sleep(300);
}

pass = results.length > 0 && results.every((r) => r.ok);
say(
  `RESULT: ${pass ? "PASS" : "FAIL"} (${results.filter((r) => r.ok).length}/${results.length})`,
);
fs.mkdirSync(path.dirname(REPORT), { recursive: true });
fs.writeFileSync(REPORT, lines.join("\n") + "\n", "utf-8");
say(`report: ${REPORT}`);
process.exit(pass ? 0 : 1);
