/* End-to-end checks for the failure card, the session library and the
   workspace list, against a provider whose endpoint is dead so the retry
   backoff and the error card come from the real engine.

   Drives the real Tauri renderer over the DevTools Protocol (WebView2 remote
   debugging). Builds the web assets first, and the debug exe when missing.
   Writes a pass/fail report to REPORT. WS_ROOT / APP_DATA / CDP_PORT /
   APP_PORT / REPORT override the defaults.
*/
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
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
const DEAD = "http://127.0.0.1:9/v1";

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

function prepareFixtures() {
  fs.rmSync(TMP, { recursive: true, force: true });
  fs.mkdirSync(WS_ROOT, { recursive: true });
  fs.mkdirSync(APP_DATA, { recursive: true });
  fs.writeFileSync(
    path.join(WS_ROOT, ".env"),
    `BASE_URL=${DEAD}\nAPI_KEY=sk-dead-endpoint-check\nMODEL=dead-model\n`,
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
        active_provider_id: DEAD,
        providers: [
          {
            id: DEAD,
            name: "Dead Local",
            base_url: DEAD,
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
  if (!fs.existsSync(EXE)) {
    const built = spawnSync("cargo", ["build"], {
      cwd: path.join(ROOT, "desktop", "src-tauri"),
      stdio: "inherit",
    });
    if (built.status !== 0) throw new Error("cargo build failed");
  }
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
