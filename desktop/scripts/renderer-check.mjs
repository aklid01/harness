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

const liveState = {
  requests: [],
  counters: {},
  writes: 0,
  planBuilds: 0,
  builds: 0,
  server: null,
};

const writePlanResult = (n) =>
  `implemented the approved plan (build ${n}): measured, cached, verified\n`;

const REVIEW_REPLIES = {
  "review-model-a": {
    findings: [
      {
        category: "security",
        severity: "high",
        file: "README.md",
        line: 2,
        summary: "The second line of the readme leaks an internal token.",
        why: "The line is world-readable and reads like a credential.",
        fix: "Take the token out of README.md and rotate it.",
        confidence: "high",
      },
    ],
  },
  "review-model-b": {
    findings: [
      {
        category: "security",
        severity: "high",
        file: "README.md",
        line: 2,
        summary:
          "The second line of the readme leaks what looks like an internal token.",
        why: "The line is world-readable and reads like a credential.",
        fix: "Take the token out of README.md and rotate it.",
        confidence: "high",
      },
      {
        category: "performance",
        severity: "low",
        file: "NOTES.md",
        line: 1,
        summary: "The notes are parsed again on every request.",
        why: "Nothing caches the parsed notes, so the cost repeats.",
        fix: "Cache the parsed notes and rebuild them when the file changes.",
        confidence: "medium",
      },
    ],
  },
  "review-model-broken": "I could not do that.",
};

const writeFixture = (revision) => ({
  "README.md":
    "Throwaway workspace for the renderer check.\n" +
    `Review fixture line two, revision ${revision}.\n` +
    "Review fixture line three.\n",
  "NOTES.md":
    `Review fixture notes, revision ${revision}.\n` +
    "A second note line for the reviewers.\n",
});
function startLiveFixture() {
  const server = http.createServer((req, res) => {
    if (req.method === "GET" && (req.url ?? "").endsWith("/models")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "live-model",
              object: "model",
              created: 1,
              owned_by: "fixture",
              name: "Live Fixture Model",
              context_length: 131072,
              top_provider: { max_completion_tokens: 16384 },
              architecture: { input_modalities: ["text"] },
              supported_parameters: ["temperature", "top_p"],
            },
            {
              id: "writer-model",
              object: "model",
              created: 1,
              owned_by: "fixture",
              context_window: 8192,
            },
          ],
        }),
      );
      return;
    }
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
      const model = payload.model || "";
      const n = (liveState.counters[model] =
        (liveState.counters[model] || 0) + 1);
      if (!payload.stream) {
        let content = REVIEW_REPLIES[model];
        if (!content && model === "writer-model") {
          const ask = (payload.messages || []).find((m) => m.role === "user");
          const raw = String(ask?.content ?? "");
          content = raw.includes(": ")
            ? raw.slice(raw.lastIndexOf(": ") + 2).trim()
            : "ok";
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            id: "chatcmpl-live",
            object: "chat.completion",
            created: 1,
            model,
            choices: [
              {
                index: 0,
                message: {
                  role: "assistant",
                  content:
                    typeof content === "string"
                      ? content
                      : JSON.stringify(content),
                },
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
      if (model === "live-model") {
        const messages = payload.messages || [];
        const history = messages.map((m) => String(m.content ?? "")).join("\n");
        const ask = messages
          .filter((m) => m.role === "user")
          .map((m) => String(m.content ?? ""))
          .join("\n");
        const lastContent = String(
          messages[messages.length - 1]?.content ?? "",
        );
        const toolNames = messages.flatMap((m) =>
          (m.tool_calls ?? []).map((c) => c.function?.name ?? ""),
        );
        // A write that actually landed, keyed on its tool result: the write's
        // path alone is not evidence (the checklist step names it too).
        const wroteResult = (name) =>
          messages.some(
            (m) =>
              m.role === "tool" &&
              String(m.content ?? "").startsWith("Wrote") &&
              String(m.content ?? "").includes(name),
          );
        if (lastContent.includes("Plan mode:")) {
          // A read-only plan turn (explicit /plan, or a converted one):
          // record the plan's steps in the checklist first, then answer with
          // the plan itself - so the checklist tracks the plan and the build
          // that follows can tick steps off from real evidence.
          if (!toolNames.includes("write_todos")) {
            send({
              choices: [
                {
                  index: 0,
                  delta: {
                    role: "assistant",
                    tool_calls: [
                      {
                        index: 0,
                        id: "call_pt1",
                        type: "function",
                        function: { name: "write_todos", arguments: "" },
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
                            todos: [
                              {
                                content: "Write PLAN-RESULT.md with the results",
                                activeForm: "Writing the results",
                                status: "in_progress",
                              },
                              {
                                content: "Benchmark the render loop",
                                activeForm: "Benchmarking the render loop",
                                status: "pending",
                              },
                            ],
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
            return;
          }
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  content:
                    "PLAN: measure first, then optimize. 1. Profile the render loop. 2. Cache the layout pass. 3. Verify with a benchmark.",
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (
          ask.includes("make the app faster") &&
          !toolNames.includes("plan_decision") &&
          !history.includes("<plan>")
        ) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_pd1",
                      type: "function",
                      function: { name: "plan_decision", arguments: "" },
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
                          decision: "plan",
                          reason: "several parts could be the bottleneck",
                        }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (
          history.includes("Entering plan mode") &&
          !history.includes("PLAN:")
        ) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  content:
                    "PLAN: measure first, then optimize. 1. Profile the render loop. 2. Cache the layout pass. 3. Verify with a benchmark.",
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (history.includes("<plan>") && wroteResult("PLAN-RESULT.md")) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  content: "Implemented the plan.",
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (history.includes("<plan>")) {
          liveState.planBuilds += 1;
          const files = writePlanResult(liveState.planBuilds);
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_pb1",
                      type: "function",
                      function: { name: "write_file", arguments: "" },
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
                          path: path.join(WS_ROOT, "PLAN-RESULT.md"),
                          content: files,
                        }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (ask.includes("make the app faster") && history.includes("PLAN:")) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  content: "Built without the plan, straight to the point.",
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (
          ask.includes("rename the foo function to bar everywhere") &&
          !toolNames.includes("plan_decision")
        ) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_pd2",
                      type: "function",
                      function: { name: "plan_decision", arguments: "" },
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
                        arguments: JSON.stringify({ decision: "build" }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (
          history.includes("Proceeding as build.") &&
          history.includes("FACTS.md")
        ) {
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  content: "Renamed foo to bar everywhere.",
                },
              },
            ],
          });
          send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        if (history.includes("Proceeding as build.")) {
          liveState.builds += 1;
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_bd1",
                      type: "function",
                      function: { name: "write_file", arguments: "" },
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
                          path: path.join(WS_ROOT, "FACTS.md"),
                          content: `renamed foo to bar (build ${liveState.builds})\n`,
                        }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
      }
      if (model === "writer-model") {
        const messages = payload.messages || [];
        const alreadyWrote = messages.some((m) =>
          (m.tool_calls ?? []).some((c) => c.function?.name === "write_file"),
        );
        const wants = messages.some((m) =>
          String(m.content ?? "").includes("write the review fixture"),
        );
        if (wants && !alreadyWrote) {
          liveState.writes += 1;
          const files = writeFixture(liveState.writes);
          send({
            choices: [
              {
                index: 0,
                delta: {
                  role: "assistant",
                  tool_calls: [
                    {
                      index: 0,
                      id: "call_w1",
                      type: "function",
                      function: { name: "write_file", arguments: "" },
                    },
                    {
                      index: 1,
                      id: "call_w2",
                      type: "function",
                      function: { name: "write_file", arguments: "" },
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
                          content: files["README.md"],
                        }),
                      },
                    },
                    {
                      index: 1,
                      function: {
                        arguments: JSON.stringify({
                          path: path.join(WS_ROOT, "NOTES.md"),
                          content: files["NOTES.md"],
                        }),
                      },
                    },
                  ],
                },
              },
            ],
          });
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
          send({ choices: [], usage });
          res.write("data: [DONE]\n\n");
          res.end();
          return;
        }
        send({
          choices: [
            {
              index: 0,
              delta: {
                role: "assistant",
                content: "The fixture files are written.",
              },
            },
          ],
        });
        send({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
        send({ choices: [], usage });
        res.write("data: [DONE]\n\n");
        res.end();
        return;
      }
      if (n === 1) {
        send({
          choices: [
            {
              index: 0,
              delta: {
                role: "assistant",
                reasoning_content:
                  "Let me look at the workspace before acting.",
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
          send({
            choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }],
          });
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
            {
              index: 0,
              delta: { role: "assistant", content: "queued turn complete" },
            },
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
              {
                name: "Writer Local",
                model: "writer-model",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: false,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-write",
              },
            ],
          },
          {
            id: "grp-review",
            name: "Review Local",
            base_url: ENDPOINT,
            headers: {},
            disabled: false,
            models: [
              {
                name: "Review A",
                model: "review-model-a",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: false,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-ra",
              },
              {
                name: "Review B",
                model: "review-model-b",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: false,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-rb",
              },
              {
                name: "Review Broken",
                model: "review-model-broken",
                reasoning_effort: null,
                temperature: null,
                context_window: null,
                max_output_tokens: null,
                supports_tools: true,
                supports_reasoning: false,
                supports_text_tools: false,
                extra_params: {},
                disabled: false,
                id: "mdl-rbroke",
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
    } catch {}
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
  const web = spawnSync(
    process.execPath,
    [path.join(ROOT, "desktop", "scripts", "build-web.mjs")],
    {
      cwd: path.join(ROOT, "desktop"),
      stdio: "inherit",
    },
  );
  if (web.status !== 0) throw new Error("build:web failed");

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

    env.WEBVIEW2_USER_DATA_FOLDER = path.join(TMP, "webview2");

    env.HARNESS_DATA_ROOT = APP_DATA;
    env.HARNESS_PORT = String(APP_PORT);
    env.HARNESS_PROJECT_ROOT = WS_ROOT;
    env.HARNESS_APP_DATA = APP_DATA;
    env.HARNESS_SESSIONS_ROOT = path.join(TMP, "sessions");

    env.HARNESS_MAX_ATTACHMENT_BYTES = "2048";
    return env;
  })(),
  stdio: ["ignore", "ignore", "ignore"],
});

const results = [];
const verdict = (name, ok, detail = "") => {
  results.push({ name, ok });
  say(`VERDICT ${name}: ${ok}${detail ? ` (${detail})` : ""}`);
};

const typeEntry = (ws, text) =>
  evaluate(
    ws,
    `(() => {
  const entry = document.getElementById("entry");
  entry.value = ${JSON.stringify(text)};
  entry.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
})()`,
  );
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

const transcriptMessages = () => {
  const out = [];
  const walk = (dir) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!entry.name.endsWith(".jsonl")) continue;
      let raw = "";
      try {
        raw = fs.readFileSync(full, "utf-8");
      } catch {
        continue;
      }
      for (const line of raw.split("\n")) {
        if (!line.trim()) continue;
        try {
          out.push(JSON.parse(line));
        } catch {}
      }
    }
  };
  walk(path.join(TMP, "sessions"));
  return out;
};

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

  const jsErrors = [];
  await rpc(ws, "Runtime.enable");
  ws.addEventListener("message", (raw) => {
    let msg;
    try {
      msg = JSON.parse(String(raw.data));
    } catch {
      return;
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params.exceptionDetails;
      const text =
        d.exception?.description ?? d.text ?? "unknown page exception";
      jsErrors.push(`${d.url ?? ""}:${(d.lineNumber ?? 0) + 1} ${text}`);
    }
  });
  globalThis.__jsErrors = jsErrors;

  for (let i = 0; i < 120; i++) {
    const state = await evaluate(
      ws,
      `document.getElementById("connText")?.textContent ?? ""`,
    );

    if (String(state).startsWith("connected")) break;
    await sleep(500);
  }

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

  const selectorState = await evaluate(
    ws,
    `(() => ({
    selectorGone: document.getElementById("modeBtn") === null,
    chipHidden: document.getElementById("modeChip").classList.contains("hidden"),
    statusModeEmpty: (document.getElementById("statusMode").textContent ?? "").trim() === "",
  }))()`,
  );
  verdict(
    "no-mode-selector-in-the-ui",
    selectorState.selectorGone &&
      selectorState.chipHidden &&
      selectorState.statusModeEmpty,
    JSON.stringify(selectorState),
  );
  await typeEntry(ws, "/");
  await sleep(250);
  const cmdAll = await evaluate(
    ws,
    `(() => ({
    open: !document.getElementById("cmdMenu").classList.contains("hidden"),
    names: [...document.querySelectorAll("#cmdMenu .cmd-name")].map((s) => s.textContent.trim()),
    help: [...document.querySelectorAll("#cmdMenu .m-label small")].map((s) => s.textContent.trim()),
  }))()`,
  );
  verdict(
    "slash-menu-lists-plan-ask-build",
    cmdAll.open &&
      ["[/plan]", "[/ask]", "[/build]"].every((want) =>
        cmdAll.names.some((n) => n === want.slice(1, -1)),
      ) &&
      cmdAll.help.every((h) => h.length > 0),
    JSON.stringify(cmdAll),
  );
  await typeEntry(ws, "/pl");
  await sleep(200);
  const cmdFiltered = await evaluate(
    ws,
    `(() => ({
    names: [...document.querySelectorAll("#cmdMenu .cmd-name")].map((s) => s.textContent.trim()),
  }))()`,
  );
  await evaluate(
    ws,
    `document.getElementById("entry").dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }))`,
  );
  await sleep(200);
  const completed = await evaluate(
    ws,
    `document.getElementById("entry").value`,
  );
  verdict(
    "slash-autocomplete-filters-and-completes",
    cmdFiltered.names.length === 1 &&
      cmdFiltered.names[0] === "/plan" &&
      completed === "/plan ",
    JSON.stringify({ ...cmdFiltered, completed }),
  );
  // The list stays up while the command line is composed, and Esc dismisses.
  await typeEntry(ws, "/plan make it faster, please");
  await sleep(200);
  const composing = await evaluate(
    ws,
    `(() => ({
    open: !document.getElementById("cmdMenu").classList.contains("hidden"),
    names: [...document.querySelectorAll("#cmdMenu .cmd-name")].map((s) => s.textContent.trim()),
  }))()`,
  );
  await evaluate(
    ws,
    `document.getElementById("entry").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))`,
  );
  await sleep(150);
  const cmdDismissedNow = await evaluate(
    ws,
    `document.getElementById("cmdMenu").classList.contains("hidden")`,
  );
  verdict(
    "slash-menu-stays-up-while-composing-and-esc-dismisses",
    composing.open &&
      composing.names.join(",") === "/plan" &&
      Boolean(cmdDismissedNow),
    JSON.stringify({ ...composing, dismissed: Boolean(cmdDismissedNow) }),
  );
  await typeEntry(ws, "");

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

  await typeEntry(ws, "steer mid-turn");
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
    menuSteer &&
      strip1.visible &&
      strip1.items.some((i) => i.badge === "STEER"),
    JSON.stringify({ menuSteer, ...strip1 }),
  );

  await typeEntry(ws, "queued work");
  await evaluate(ws, `document.getElementById("sendExtra")?.click()`);
  await sleep(300);
  const strip2 = await stripState();
  verdict(
    "queue-files-with-a-badge",
    strip2.items.filter((i) => i.badge === "QUEUE").length === 1 &&
      strip2.items.some((i) => i.text === "queued work"),
    JSON.stringify(strip2),
  );

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

  await typeEntry(ws, "remove me");
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

  verdict(
    "pending-remove-drops-the-message",
    Array.isArray(strip5) && strip5.includes("edited queue work"),
    JSON.stringify({ afterFile: strip4, afterRemove: strip5 }),
  );

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

  const chainIdle = await waitFor(
    `!document.getElementById("promptZone").classList.contains("busy")`,
    30000,
  );

  const queueDisk = (() => {
    const messages = transcriptMessages();
    const at = messages.findIndex(
      (m) =>
        m.role === "user" && String(m.content ?? "") === "edited queue work",
    );
    return {
      at,
      answered:
        at >= 0 &&
        messages
          .slice(at)
          .some((m) =>
            String(m.content ?? "").includes("queued turn complete"),
          ),
    };
  })();
  verdict(
    "queue-auto-runs-after-the-turn",
    queueDisk.at >= 0 && queueDisk.answered && chainIdle === true,
    JSON.stringify({ ...queueDisk, chainIdle }),
  );

  const attachFiles = (files) =>
    evaluate(
      ws,
      `(() => {
  const input = document.getElementById("fileInput");
  if (!input) return false;
  const dt = new DataTransfer();
  ${files
    .map(
      (f) =>
        `dt.items.add(new File([${JSON.stringify(f.body)}], ${JSON.stringify(
          f.name,
        )}, { type: ${JSON.stringify(f.type || "application/octet-stream")} }));`,
    )
    .join("\n  ")}
  input.files = dt.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
})()`,
    );
  const attachState = () =>
    evaluate(
      ws,
      `(() => ({
  bar: [...document.querySelectorAll("#attachBar .attach-chip")].map((c) => ({
    name: c.querySelector(".a-name")?.textContent ?? "",
    meta: c.querySelector(".a-meta")?.textContent ?? "",
    failed: Boolean(c.querySelector(".a-error")),
    why: c.querySelector(".a-error")?.title ?? "",
  })),
  sent: [...document.querySelectorAll("#chat .msg.user .attach-chip .a-name")].map(
    (n) => n.textContent ?? "",
  ),
  activity: document.getElementById("activityText")?.textContent ?? "",
}))()`,
    );

  await attachFiles([
    {
      name: "notes.md",
      body: "notes the user attached\n",
      type: "text/markdown",
    },
    {
      name: "data.bin",
      body: "second file\n",
      type: "application/octet-stream",
    },
  ]);
  const picked = await waitFor(
    `(() => {
    const chips = [...document.querySelectorAll("#attachBar .attach-chip")];
    return chips.length === 2 ? chips.length : false;
  })()`,
    15000,
  );
  const pickedState = await attachState();
  verdict(
    "attachment-chips-show-name-type-and-size",
    picked === 2 &&
      pickedState.bar.some(
        (c) => c.name === "notes.md" && c.meta.includes("md"),
      ) &&
      pickedState.bar.some(
        (c) => c.name === "data.bin" && c.meta.includes("B"),
      ),
    JSON.stringify(pickedState.bar),
  );

  await evaluate(
    ws,
    `(() => {
    const chip = [...document.querySelectorAll("#attachBar .attach-chip")].find(
      (c) => c.querySelector(".a-name")?.textContent === "data.bin",
    );
    chip?.querySelector(".attach-x")?.click();
    return Boolean(chip);
  })()`,
  );
  await sleep(300);
  const afterRemove = await attachState();
  verdict(
    "attachment-can-be-removed-before-sending",
    afterRemove.bar.length === 1 &&
      afterRemove.bar[0].name === "notes.md" &&
      !afterRemove.bar[0].failed,
    JSON.stringify(afterRemove.bar),
  );

  await attachFiles([
    {
      name: "huge.bin",
      body: "x".repeat(4096),
      type: "application/octet-stream",
    },
  ]);
  const refused = await waitFor(
    `(() => {
    const chip = [...document.querySelectorAll("#attachBar .attach-chip")].find(
      (c) => c.querySelector(".a-name")?.textContent === "huge.bin",
    );
    return chip && chip.querySelector(".a-error") ? chip.querySelector(".a-error").title : false;
  })()`,
    15000,
  );
  const refusedState = await attachState();
  verdict(
    "oversize-attachment-shows-a-clear-error",
    typeof refused === "string" &&
      refused.includes("huge.bin") &&
      refused.includes("2048") &&
      refusedState.bar.some((c) => c.name === "notes.md" && !c.failed),
    JSON.stringify({ refused, bar: refusedState.bar }),
  );

  const beforeSend = liveState.requests.length;
  await typeEntry(ws, "summarize the attached file");
  await evaluate(ws, `document.getElementById("sendstop").click()`);
  const sentChip = await waitFor(
    `(() => {
    const names = [...document.querySelectorAll("#chat .msg.user .attach-chip .a-name")].map(
      (n) => n.textContent ?? "",
    );
    return names.includes("notes.md") ? names : false;
  })()`,
    20000,
  );
  const overTheWire = await (async () => {
    await sleep(1500);
    return liveState.requests
      .slice(beforeSend)
      .some((r) =>
        (r.messages || []).some(
          (m) =>
            m.role === "user" &&
            typeof m.content === "string" &&
            m.content.includes("<attachments>") &&
            m.content.includes("notes.md"),
        ),
      );
  })();
  verdict(
    "attachment-travels-with-the-message",
    Array.isArray(sentChip) &&
      sentChip.includes("notes.md") &&
      !sentChip.includes("huge.bin") &&
      overTheWire,
    JSON.stringify({ sentChip, overTheWire }),
  );

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

  const freshTab = async () => {
    await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
    await sleep(500);
  };
  const chip = () =>
    evaluate(
      ws,
      `(() => {
  const chip = document.getElementById("modeChip");
  return {
    hidden: chip.classList.contains("hidden"),
    label: document.getElementById("modeChipLabel").textContent,
    title: chip.title,
  };
})()`,
    );
  const planActions = () =>
    evaluate(
      ws,
      `(() => {
  const bar = document.querySelector("#chat .plan-actions");
  if (!bar) return null;
  return {
    auto: bar.classList.contains("auto"),
    tag: (bar.querySelector(".plan-actions-tag")?.textContent ?? "").trim(),
    buttons: [...bar.querySelectorAll("button")].map((b) => b.getAttribute("data-act")),
  };
})()`,
    );
  const modalUp = (id) =>
    `(() => { const m = document.getElementById("modal"); return !m.classList.contains("hidden") && Boolean(document.getElementById(${JSON.stringify(id)})); })()`;
  const clickModal = async (id) => {
    const there = await evaluate(ws, modalUp(id));
    if (!there) return false;
    await evaluate(
      ws,
      `document.getElementById(${JSON.stringify(id)}).click()`,
    );
    return true;
  };

  const idle = () =>
    waitFor(
      `!document.getElementById("promptZone").classList.contains("busy")`,
      60000,
    );

  const latestPlanArtifact = () => {
    const plansDir = path.join(TMP, "sessions");
    let newest = null;
    const walk = (dir) => {
      let entries = [];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!entry.name.endsWith(".json") || !full.includes("_plans")) continue;
        const stat = fs.statSync(full);
        if (!newest || stat.mtimeMs > newest.mtime)
          newest = { full, mtime: stat.mtimeMs };
      }
    };
    walk(plansDir);
    try {
      return newest ? JSON.parse(fs.readFileSync(newest.full, "utf-8")) : {};
    } catch {
      return {};
    }
  };

  await freshTab();
  await submit(ws, "/ask");
  await waitFor(
    `!document.getElementById("modeChip").classList.contains("hidden")`,
    15000,
  );
  const askChip = await chip();
  verdict(
    "bare-ask-turns-on-the-mode-chip",
    askChip.label === "Ask",
    JSON.stringify(askChip),
  );
  await submit(ws, "what does the readme say?");
  await idle();
  const askChipStill = await chip();
  verdict(
    "ask-stays-until-cleared",
    askChipStill.label === "Ask",
    JSON.stringify(askChipStill),
  );
  await evaluate(ws, `document.getElementById("modeChip")?.click()`);
  await sleep(600);
  const clearedChip = await chip();
  verdict(
    "mode-chip-click-clears-to-build",
    clearedChip.hidden,
    JSON.stringify(clearedChip),
  );

  await freshTab();
  await submit(ws, "/plan");
  await waitFor(
    `!document.getElementById("modeChip").classList.contains("hidden")`,
    15000,
  );
  const planChip = await chip();
  verdict(
    "bare-plan-turns-on-the-mode-chip",
    planChip.label === "Plan",
    JSON.stringify(planChip),
  );
  await submit(ws, "/build");
  await sleep(700);
  const buildChip = await chip();
  verdict(
    "build-command-clears-the-chip",
    buildChip.hidden,
    JSON.stringify(buildChip),
  );

  await freshTab();
  await submit(ws, "/ask what does the readme say?");
  const researching = await waitFor(
    `Boolean(document.querySelector("#chat .phase-researching"))`,
    20000,
  );
  await idle();
  const oneTurnChip = await chip();
  const oneTurnDiag = await evaluate(
    ws,
    `(() => ({
    pills: [...document.querySelectorAll("#chat .phase")].map((p) => p.className.replace("phase phase-", "").replace(" settled", "")),
    answers: [...document.querySelectorAll("#chat .assistant")].map((el) => el.textContent.slice(0, 60)),
    errors: document.querySelectorAll("#chat .error").length,
    tabs: [...document.querySelectorAll("#tabs .tab")].map((t) => t.className.includes("active")),
    titles: [...document.querySelectorAll("#tabs .tab")].map((t) => (t.querySelector(".tab-title")?.textContent ?? "").slice(0, 24)),
    children: [...(document.querySelector("#chat")?.firstElementChild?.children ?? [])].map((c) => c.className.split(" ")[0]).slice(0, 40),
  }))()`,
  );
  const disk = (() => {
    const out = [];
    const walk = (dir) => {
      let entries = [];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!entry.name.endsWith(".jsonl")) continue;
        try {
          for (const line of fs.readFileSync(full, "utf-8").split("\n")) {
            if (line.trim()) out.push(JSON.parse(line));
          }
        } catch {}
      }
    };
    walk(path.join(TMP, "sessions"));
    return out;
  })();
  const askTurns = disk.filter(
    (m) => m.role === "user" && String(m.content ?? "").startsWith("/ask what"),
  ).length;
  const askNotices = disk.filter((m) =>
    String(m.content ?? "").includes("ASK (read-only)"),
  ).length;
  verdict(
    "one-turn-ask-runs-read-only-without-sticking",
    oneTurnChip.hidden && askTurns >= 1 && askNotices >= 2,

    JSON.stringify({
      researching: Boolean(researching),
      ...oneTurnChip,
      ...oneTurnDiag,
      askTurns,
      askNotices,
    }),
  );

  await freshTab();
  await submit(ws, "make the app faster");
  const autoChipUp = await waitFor(
    `(() => { const c = document.getElementById("modeChip"); return !c.classList.contains("hidden") && document.getElementById("modeChipLabel").textContent.includes("auto"); })()`,
    30000,
  );
  const autoChip = await chip();
  verdict(
    "auto-plan-chip-shows-with-reason",
    Boolean(autoChipUp) &&
      autoChip.title.includes("several parts could be the bottleneck"),
    JSON.stringify(autoChip),
  );
  const planShown = await waitFor(
    `[...document.querySelectorAll("#chat .assistant")].some((el) => el.textContent.includes("Profile the render loop"))`,
    30000,
  );
  // The plan turn wrote the plan's steps into the checklist.
  const checklistFilled = await waitFor(
    `document.querySelectorAll("#plan .plan-item").length >= 2`,
    30000,
  );
  const gateShown = await waitFor(modalUp("mAllow"), 30000);
  const resultBefore = fs.existsSync(path.join(WS_ROOT, "PLAN-RESULT.md"));
  verdict(
    "auto-plan-gate-asks-before-building",
    Boolean(planShown) && Boolean(gateShown) && !resultBefore,
    JSON.stringify({
      planShown: Boolean(planShown),
      gateShown: Boolean(gateShown),
      resultBefore,
    }),
  );
  await clickModal("mAllow");
  await idle();
  const resultAfter = fs.existsSync(path.join(WS_ROOT, "PLAN-RESULT.md"));
  verdict(
    "auto-plan-approve-builds-exactly-once",
    !resultBefore && resultAfter && liveState.planBuilds === 1,
    JSON.stringify({
      resultBefore,
      resultAfter,
      planBuilds: liveState.planBuilds,
    }),
  );
  // The build achieved the plan's first step: the checklist says so.
  const stepDone = await waitFor(
    `(() => {
    const item = [...document.querySelectorAll("#plan .plan-item")].find((n) => n.textContent.includes("PLAN-RESULT"));
    return item ? item.className.includes("completed") : false;
  })()`,
    30000,
  );
  verdict(
    "checklist-ticks-off-as-the-build-succeeds",
    Boolean(checklistFilled) && Boolean(stepDone),
    JSON.stringify({ checklistFilled: Boolean(checklistFilled), stepDone: Boolean(stepDone) }),
  );

  await freshTab();
  await submit(ws, "make the app faster");
  const skipModal = await waitFor(
    `Boolean(document.getElementById("mSkip"))`,
    30000,
  );
  const skipModalState = await evaluate(
    ws,
    `(() => ({
    busy: document.getElementById("promptZone").classList.contains("busy"),
    modal: (() => { const m = document.getElementById("modal"); return !m.classList.contains("hidden") && Boolean(document.getElementById("mSkip")); })(),
    notes: [...document.querySelectorAll("#chat .note")].map((n) => n.textContent.slice(0, 70)),
    errors: [...document.querySelectorAll("#chat .error")].map((n) => n.textContent.slice(0, 70)),
    tabs: document.querySelectorAll("#tabs .tab").length,
  }))()`,
  );
  await clickModal("mSkip");
  await idle();
  const skippedText = await evaluate(
    ws,
    `[...document.querySelectorAll("#chat .assistant")].some((el) => el.textContent.includes("Built without the plan"))`,
  );
  const skipAfter = await evaluate(
    ws,
    `(() => ({
    busy: document.getElementById("promptZone").classList.contains("busy"),
    modal: (() => { const m = document.getElementById("modal"); return !m.classList.contains("hidden"); })(),
    notes: [...document.querySelectorAll("#chat .note")].map((n) => n.textContent.slice(0, 70)),
    answers: [...document.querySelectorAll("#chat .msg.assistant")].map((n) => n.textContent.slice(0, 70)),
  }))()`,
  );
  const lastTranscript = (() => {
    let newest = null;
    const walk = (dir) => {
      let entries = [];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!entry.name.endsWith(".jsonl")) continue;
        const stat = fs.statSync(full);
        if (!newest || stat.mtimeMs > newest.mtime)
          newest = { full, mtime: stat.mtimeMs };
      }
    };
    walk(path.join(TMP, "sessions"));
    try {
      return newest
        ? fs
            .readFileSync(newest.full, "utf-8")
            .split("\n")
            .filter((l) => l.trim())
            .map((l) => JSON.parse(l))
            .map((m) => `${m.role}:${String(m.content ?? "").slice(0, 50)}`)
        : [];
    } catch {
      return [];
    }
  })();
  verdict(
    "auto-plan-skip-builds-now-without-the-plan",
    Boolean(skippedText) && liveState.planBuilds === 1,
    JSON.stringify({
      skippedText: Boolean(skippedText),
      planBuilds: liveState.planBuilds,
      ...skipAfter,
      lastTranscript: lastTranscript.slice(-6),
    }),
  );

  await freshTab();
  await submit(ws, "make the app faster");
  await waitFor(modalUp("mDeny"), 30000);
  await clickModal("mDeny");
  await sleep(1500);

  const cancelledArtifact = latestPlanArtifact();
  verdict(
    "auto-plan-cancel-builds-nothing",
    cancelledArtifact.phase === "awaiting_user" && liveState.planBuilds === 1,
    JSON.stringify({
      artifact: cancelledArtifact,
      planBuilds: liveState.planBuilds,
    }),
  );

  await freshTab();
  await submit(ws, "rename the foo function to bar everywhere");
  let factsWritten = false;
  for (let i = 0; i < 90 && !factsWritten; i++) {
    await sleep(500);
    factsWritten = fs.existsSync(path.join(WS_ROOT, "FACTS.md"));
  }
  const directiveChip = await chip();
  const directiveGate = await evaluate(
    ws,
    `(() => { const m = document.getElementById("modal"); return !m.classList.contains("hidden") && Boolean(document.getElementById("mAllow")); })()`,
  );
  verdict(
    "directive-request-builds-without-planning",
    factsWritten &&
      directiveChip.hidden &&
      !directiveGate &&
      liveState.builds === 1,
    JSON.stringify({
      factsWritten,
      chip: directiveChip,
      gate: directiveGate,
      builds: liveState.builds,
    }),
  );

  await freshTab();
  await submit(ws, "/plan make the app faster");
  await idle();
  const explicitChip = await chip();
  const explicitBar = await planActions();
  verdict(
    "explicit-plan-offers-one-action-handoff",
    explicitChip.hidden &&
      explicitBar &&
      explicitBar.buttons.join(",") === "approve,skip,cancel",
    JSON.stringify({ chip: explicitChip, bar: explicitBar }),
  );
  const explicitBarState = await evaluate(
    ws,
    `(() => ({
    busy: document.getElementById("promptZone").classList.contains("busy"),
    notes: [...document.querySelectorAll("#chat .note")].map((n) => n.textContent.slice(0, 70)),
    errors: [...document.querySelectorAll("#chat .error")].map((n) => n.textContent.slice(0, 70)),
    answers: [...document.querySelectorAll("#chat .msg.assistant")].map((n) => n.textContent.slice(0, 60)),
    tabs: document.querySelectorAll("#tabs .tab").length,
  }))()`,
  );
  await evaluate(
    ws,
    `document.querySelector("#chat .plan-actions [data-act='approve']")?.click()`,
  );
  let explicitBuilt = false;
  for (let i = 0; i < 90 && !explicitBuilt; i++) {
    await sleep(500);
    explicitBuilt = liveState.planBuilds === 2;
  }
  const barGone = await planActions();
  verdict(
    "explicit-plan-approve-builds-once",
    explicitBuilt && barGone === null,
    JSON.stringify({
      explicitBuilt,
      planBuilds: liveState.planBuilds,
      barGone,
      before: explicitBarState,
    }),
  );

  const reviewCard = () =>
    evaluate(
      ws,
      `(() => {
  const card = document.querySelector("#chat .review");
  if (!card) return null;
  const text = (n) => (n?.textContent ?? "").replace(/\\s+/g, " ").trim();
  const findings = [...card.querySelectorAll(".v-findings .v-finding")].map((f) => ({
    id: f.dataset.id ?? "",
    dismissed: f.classList.contains("dismissed"),
    sev: text(f.querySelector(".v-f-sev")),
    cat: text(f.querySelector(".v-f-cat")),
    where: text(f.querySelector(".v-f-where")),
    models: text(f.querySelector(".v-f-models")),
    summary: text(f.querySelector(".v-f-sum")),
    why: text(f.querySelector(".v-f-why")),
    fix: text(f.querySelector(".v-f-fix")),
    conflict: text(f.querySelector(".v-f-conflict")),
    buttons: [...f.querySelectorAll(".v-f-actions button")].map((b) => (b.textContent ?? "").trim()),
  }));
  return {
    badge: text(card.querySelector(".v-badge")),
    headline: text(card.querySelector(".v-headline")),
    notes: [...card.querySelectorAll(".v-note")].map((n) => text(n)),
    detail: text(card.querySelector(".v-detail")),
    counts: [...card.querySelectorAll(".v-counts .v-sev")].map((c) => text(c)),
    models: [...card.querySelectorAll(".v-model")].map((r) => ({
      name: text(r.querySelector(".v-model-name")),
      state: text(r.querySelector(".v-model-state")),
      failed: r.classList.contains("v-model-failed"),
    })),
    groups: [...card.querySelectorAll(".v-group-title")].map((n) => text(n)),
    findings,
    actions: [...card.querySelectorAll(".v-actions button")].map((b) => (b.textContent ?? "").trim()),
    region: card.getAttribute("aria-label") ?? "",
  };
})()`,
    );

  const waitForCard = async (ready, timeout = 60000) => {
    const limit = Date.now() + timeout;
    let card = null;
    while (Date.now() < limit) {
      card = await reviewCard();
      if (card && ready(card)) return card;
      await sleep(400);
    }
    return card;
  };

  const toolResults = (messages, want) =>
    messages.filter((m) => m.role === "tool" && want(String(m.content ?? "")));

  const savedReview = () => {
    try {
      const raw = JSON.parse(
        fs.readFileSync(path.join(APP_DATA, "agents.json"), "utf-8"),
      );
      return raw.review ?? null;
    } catch {
      return null;
    }
  };
  const waitForSaved = async (want, timeout = 15000) => {
    const limit = Date.now() + timeout;
    let review = null;
    while (Date.now() < limit) {
      review = savedReview();
      if (review && want(review)) return review;
      await sleep(300);
    }
    return review;
  };

  const openReviewTab = async () => {
    await evaluate(
      ws,
      `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`,
    );
    await sleep(200);
    await evaluate(ws, `document.getElementById("btnSettings")?.click()`);
    await sleep(600);
    const opened = await evaluate(
      ws,
      `(() => {
    const btn = document.querySelector(".settings-nav [data-nav='review']");
    if (!btn) return false;
    btn.click();
    return true;
  })()`,
    );
    await sleep(600);
    return opened;
  };

  const saveReviewSettings = async (modelIds, autoRun = "off") => {
    await openReviewTab();
    const picked = await evaluate(
      ws,
      `(() => {
  const list = document.getElementById("rvRows");
  if (!list) return [];
  let guard = 0;
  while (list.querySelectorAll(".rv-row").length && guard++ < 20) {
    list.querySelector('[data-rv-del="0"]')?.click();
  }
  for (let i = 0; i < ${modelIds.length}; i++) document.getElementById("rvAdd")?.click();
  const ids = ${JSON.stringify(modelIds)};
  const rows = [...list.querySelectorAll(".rv-row")];
  rows.forEach((row, i) => {
    const sel = row.querySelector('[data-rv="model"]');
    if (sel && ids[i]) sel.value = ids[i];
  });
  return rows.map((r) => r.querySelector('[data-rv="model"]')?.value ?? "");
})()`,
    );
    await evaluate(
      ws,
      `(() => {
  const sw = document.getElementById("sw_review");
  if (sw && !sw.classList.contains("on")) sw.click();
  const body = document.getElementById("settingsBody");
  [...(body?.querySelectorAll(".rv-row") ?? [])].forEach((row, i) => {
    const rowSw = body.querySelector("#rv_en_" + i);
    if (rowSw && !rowSw.classList.contains("on")) rowSw.click();
  });
  return Boolean(sw);
})()`,
    );

    const auto = await evaluate(
      ws,
      `(() => {
  const root = document.querySelector('[data-seg="rv_auto"]');
  const btn = root?.querySelector('button[data-v="${autoRun}"]');
  if (!btn) return { found: false, wired: false, value: "" };
  btn.click();
  const wired = btn.classList.contains("sel");
  if (!wired) {
    root.querySelectorAll("button").forEach((b) => b.classList.toggle("sel", b === btn));
  }
  return { found: true, wired, value: root.querySelector("button.sel")?.dataset.v ?? "" };
})()`,
    );
    await evaluate(ws, `document.getElementById("rvSave")?.click()`);
    await sleep(700);
    await evaluate(ws, `document.getElementById("setClose")?.click()`);
    await sleep(300);
    return { picked, auto };
  };

  const writerSession = async (firstMessage) => {
    await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
    await sleep(800);
    await evaluate(ws, `document.getElementById("providerBtn")?.click()`);
    await sleep(300);
    const picked = await evaluate(
      ws,
      `(() => {
    const btn = [...document.querySelectorAll("#providerMenu button")].find(
      (b) => b.querySelector(".m-label")?.textContent.trim() === "Writer Local",
    );
    if (!btn) return false;
    btn.click();
    return true;
  })()`,
    );
    await sleep(400);
    await submit(ws, firstMessage);
    const wrote = await waitFor(
      `(() => {
    const names = [...document.querySelectorAll("#chat .tool .t-name")].map((n) => n.textContent ?? "");
    return names.filter((n) => n === "write_file").length >= 2 ? names : false;
  })()`,
      30000,
    );
    const idle = await waitFor(
      `!document.getElementById("promptZone").classList.contains("busy")`,
      30000,
    );
    return { picked, wrote, idle };
  };

  const reviewTabOpened = await openReviewTab();
  const reviewPanel = await evaluate(
    ws,
    `(() => ({
    nav: Boolean(document.querySelector(".settings-nav [data-nav='review']")),
    role: Boolean(document.getElementById("sw_review")),
    rows: document.querySelectorAll("#rvRows .rv-row").length,
    add: Boolean(document.getElementById("rvAdd")),
    save: Boolean(document.getElementById("rvSave")),
    auto: Boolean(document.querySelector('[data-seg="rv_auto"]')),
    mode: Boolean(document.querySelector('[data-seg="rv_mode"]')),
    severity: Boolean(document.getElementById("rv_severity")),
    limits: ["rv_max_reviewers", "rv_max_files", "rv_max_diff", "rv_max_tokens"].every(
      (id) => Boolean(document.getElementById(id)),
    ),
    exclusions: Boolean(document.getElementById("rv_exclusions")),
    prompt: Boolean(document.getElementById("rv_prompt")),
  }))()`,
  );
  verdict(
    "review-settings-tab-is-reachable",
    reviewTabOpened &&
      reviewPanel.nav &&
      reviewPanel.role &&
      reviewPanel.add &&
      reviewPanel.save &&
      reviewPanel.auto &&
      reviewPanel.mode &&
      reviewPanel.severity &&
      reviewPanel.limits &&
      reviewPanel.exclusions &&
      reviewPanel.prompt,
    JSON.stringify(reviewPanel),
  );
  await evaluate(ws, `document.getElementById("setClose")?.click()`);
  await sleep(300);

  const firstSave = await saveReviewSettings(["mdl-ra", "mdl-rb"]);
  const stored = await waitForSaved((r) => (r.reviewers ?? []).length === 2);
  verdict(
    "review-settings-save-two-reviewers",
    firstSave.picked.join(",") === "mdl-ra,mdl-rb" &&
      (stored?.reviewers ?? []).length === 2 &&
      (stored?.reviewers ?? []).every((r) => r.enabled !== false) &&
      stored?.enabled === true,
    JSON.stringify({ picked: firstSave.picked, stored }),
  );
  await openReviewTab();
  const roleSwitch = await evaluate(
    ws,
    `(() => {
    const sw = document.getElementById("sw_review");
    return {
      found: Boolean(sw),
      on: Boolean(sw?.classList.contains("on")),
      checked: sw?.getAttribute("aria-checked") ?? "",
    };
  })()`,
  );
  await evaluate(ws, `document.getElementById("setClose")?.click()`);
  await sleep(300);
  verdict(
    "review-role-switch-shows-on",
    roleSwitch.found && roleSwitch.on,
    JSON.stringify(roleSwitch),
  );

  const sessionA = await writerSession("write the review fixture A");
  verdict(
    "review-session-edits-the-workspace",
    sessionA.picked && Array.isArray(sessionA.wrote) && sessionA.idle === true,
    JSON.stringify({ picked: sessionA.picked, wrote: sessionA.wrote }),
  );
  await submit(ws, "/multi-model-review");
  const merged = await waitForCard(
    (c) => c.badge !== "running" && c.findings.length > 0,
  );
  verdict(
    "review-card-appears-with-a-verdict",
    merged?.badge === "changes requested" &&
      merged?.region === "Multi-model review" &&
      merged?.headline.includes("2 findings"),
    JSON.stringify({
      badge: merged?.badge,
      headline: merged?.headline,
      region: merged?.region,
    }),
  );
  verdict(
    "review-card-lists-both-reviewers",
    merged?.models.length === 2 &&
      merged.models.some((m) => m.name.includes("review-model-a")) &&
      merged.models.some((m) => m.name.includes("review-model-b")),
    JSON.stringify(merged?.models),
  );
  const agreed = merged?.findings.find((f) =>
    f.models.includes("2 reviewers agree"),
  );
  verdict(
    "review-card-shows-two-reviewers-agree",
    Boolean(agreed) &&
      agreed.where === "README.md:2" &&
      agreed.sev.toLowerCase() === "high" &&
      agreed.cat.toLowerCase() === "security",
    JSON.stringify(agreed ?? {}),
  );
  const second = merged?.findings.find((f) => f.id !== agreed?.id);
  verdict(
    "review-card-keeps-the-second-finding",
    merged?.findings.length === 2 &&
      Boolean(second) &&
      second.cat.toLowerCase() === "performance" &&
      second.where.startsWith("NOTES.md"),
    JSON.stringify(
      merged?.findings.map((f) => ({
        id: f.id,
        cat: f.cat,
        where: f.where,
        models: f.models,
      })),
    ),
  );
  const reviewCalls = liveState.requests.filter((r) =>
    String(r.model ?? "").startsWith("review-model"),
  );
  verdict(
    "review-call-carries-no-tools",
    reviewCalls.length > 0 && reviewCalls.every((r) => !r.tools),
    JSON.stringify(
      reviewCalls.map((r) => ({ model: r.model, tools: r.tools ?? null })),
    ),
  );

  const byCategoryClick = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#chat .review .v-toolbar .seg button")].find(
      (b) => (b.textContent ?? "").trim() === "By category",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await sleep(400);
  const byCategory = await reviewCard();
  verdict(
    "review-groups-by-category",
    byCategoryClick &&
      byCategory?.groups.length === 2 &&
      byCategory.groups.some((g) => g.toLowerCase() === "security") &&
      byCategory.groups.some((g) => g.toLowerCase() === "performance"),
    JSON.stringify(byCategory?.groups),
  );
  await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#chat .review .v-toolbar .seg button")].find(
      (b) => (b.textContent ?? "").trim() === "By file",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await sleep(400);
  const byFile = await reviewCard();
  verdict(
    "review-groups-by-file",
    byFile?.groups.length === 2 &&
      byFile.groups.includes("README.md") &&
      byFile.groups.includes("NOTES.md"),
    JSON.stringify(byFile?.groups),
  );

  const dismissClick = await evaluate(
    ws,
    `(() => {
    const f = document.querySelector('#chat .review .v-finding[data-id="R2"]');
    const btn = [...(f?.querySelectorAll(".v-f-actions button") ?? [])].find(
      (b) => (b.textContent ?? "").trim() === "Dismiss",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  const dismissed = await waitForCard((c) =>
    c.findings.some((f) => f.dismissed),
  );
  verdict(
    "review-dismiss-marks-the-finding",
    dismissClick &&
      dismissed?.findings.some((f) => f.id === "R2" && f.dismissed) &&
      dismissed?.findings.some((f) => f.id === "R1" && !f.dismissed),
    JSON.stringify(
      dismissed?.findings.map((f) => ({ id: f.id, dismissed: f.dismissed })),
    ),
  );

  const sendClick = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#chat .review .v-actions button")].find(
      (b) => (b.textContent ?? "").trim() === "Send findings to the agent",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await waitFor(
    `!document.getElementById("promptZone").classList.contains("busy")`,
    30000,
  );
  const sentTools = await waitFor(
    `(() => {
    const names = [...document.querySelectorAll("#chat .tool .t-name")].map((n) => n.textContent ?? "");
    return names.includes("review_findings") ? names : false;
  })()`,
    20000,
  );
  await sleep(500);
  const sentMessages = transcriptMessages();
  const sentCalls = sentMessages.filter((m) =>
    (m.tool_calls ?? []).some((c) => c.function?.name === "review_findings"),
  );
  const sentBodies = toolResults(
    sentMessages,
    (body) => body.includes("[R1]") && body.includes("[R2]"),
  );
  verdict(
    "review-send-hands-the-findings-to-the-agent",
    sendClick && sentCalls.length > 0 && sentBodies.length > 0,
    JSON.stringify({
      calls: sentCalls.length,
      bodies: sentBodies.map((m) => String(m.content).slice(0, 60)),
    }),
  );
  verdict(
    "review-findings-reach-the-transcript-view",
    Array.isArray(sentTools) && sentTools.includes("review_findings"),
    JSON.stringify(sentTools ?? []),
  );

  const applyClick = await evaluate(
    ws,
    `(() => {
    const f = document.querySelector('#chat .review .v-finding[data-id="R1"]');
    const btn = [...(f?.querySelectorAll(".v-f-actions button") ?? [])].find(
      (b) => (b.textContent ?? "").trim() === "Apply fix",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await waitFor(
    `!document.getElementById("promptZone").classList.contains("busy")`,
    30000,
  );
  await sleep(1500);
  const applyBodies = toolResults(
    transcriptMessages(),
    (body) => body.includes("[R1]") && !body.includes("[R2]"),
  );
  const applyTools = await evaluate(
    ws,
    `[...document.querySelectorAll("#chat .tool .t-name")].map((n) => n.textContent ?? "")`,
  );
  verdict(
    "review-apply-fix-sends-one-finding",
    applyClick && applyBodies.length > 0,
    JSON.stringify(applyBodies.map((m) => String(m.content).slice(0, 80))),
  );
  verdict(
    "review-apply-fix-is-a-second-handover",
    applyTools.filter((n) => n === "review_findings").length === 2,
    JSON.stringify(applyTools),
  );

  await saveReviewSettings(["mdl-ra", "mdl-rbroke"]);
  const partialRerun = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#chat .review .v-actions button")].find(
      (b) => (b.textContent ?? "").trim() === "Re-run",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  const partial = await waitForCard((c) => c.badge.includes("partial"));
  verdict(
    "review-partial-when-a-reviewer-fails",
    partialRerun && partial?.badge === "changes requested (partial)",
    JSON.stringify({ badge: partial?.badge, notes: partial?.notes }),
  );
  verdict(
    "review-partial-names-the-failed-model",
    partial?.models.some(
      (m) => m.failed && m.name.includes("review-model-broken"),
    ) &&
      partial?.models.some(
        (m) => !m.failed && m.name.includes("review-model-a"),
      ),
    JSON.stringify(partial?.models),
  );
  verdict(
    "review-partial-keeps-the-good-finding",
    partial?.findings.length === 1 &&
      partial.findings[0].where === "README.md:2" &&
      partial.findings[0].sev.toLowerCase() === "high",
    JSON.stringify(partial?.findings),
  );

  await saveReviewSettings(["mdl-rbroke"]);
  const failedRerun = await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#chat .review .v-actions button")].find(
      (b) => (b.textContent ?? "").trim() === "Re-run",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  const failed = await waitForCard((c) => c.badge === "failed");
  verdict(
    "review-failed-when-no-reviewer-finishes",
    failedRerun &&
      failed?.badge === "failed" &&
      failed.detail.includes("not a usable review"),
    JSON.stringify({ badge: failed?.badge, detail: failed?.detail }),
  );

  await saveReviewSettings(["mdl-ra", "mdl-rb"]);
  await evaluate(ws, `document.querySelector(".tree-new")?.click()`);
  await sleep(800);
  await evaluate(ws, `document.getElementById("providerBtn")?.click()`);
  await sleep(300);
  await evaluate(
    ws,
    `(() => {
    const btn = [...document.querySelectorAll("#providerMenu button")].find(
      (b) => b.querySelector(".m-label")?.textContent.trim() === "Writer Local",
    );
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await sleep(400);
  await submit(ws, "nothing to review here");
  await waitFor(
    `!document.getElementById("promptZone").classList.contains("busy")`,
    30000,
  );
  await submit(ws, "/multi-model-review");
  const nothing = await waitForCard((c) => c.badge === "nothing to review");
  verdict(
    "review-says-nothing-to-review",
    nothing?.badge === "nothing to review",
    JSON.stringify({
      badge: nothing?.badge,
      headline: nothing?.headline,
    }),
  );

  const autoSave = await saveReviewSettings(["mdl-ra", "mdl-rb"], "start");
  const sessionB = await writerSession("write the review fixture B");
  const auto = await waitForCard(
    (c) => c.badge !== "running" && c.findings.length > 0,
  );
  verdict(
    "review-auto-runs-after-a-run-that-edited-files",
    autoSave.auto.value === "start" &&
      sessionB.picked &&
      auto?.badge === "changes requested" &&
      auto?.findings.length === 2,
    JSON.stringify({
      auto: autoSave.auto,
      picked: sessionB.picked,
      badge: auto?.badge,
      findings: auto?.findings.length,
    }),
  );

  const closedTab = await evaluate(
    ws,
    `(() => {
    const btn = document.querySelector("#tabs .tab.active .tab-close");
    btn?.click();
    return Boolean(btn);
  })()`,
  );
  await sleep(1000);
  const reopened = await waitFor(
    `(() => {
    const row = [...document.querySelectorAll("#wsTree .session-item")].find(
      (r) => (r.querySelector(".s-title")?.textContent ?? "").trim() === "write the review fixture B",
    );
    if (!row) return false;
    row.click();
    return true;
  })()`,
    20000,
  );
  await sleep(2500);
  const restored = await reviewCard();
  const restoredTools = await evaluate(
    ws,
    `[...document.querySelectorAll("#chat .tool .t-name")].map((n) => n.textContent ?? "")`,
  );
  verdict(
    "review-card-comes-back-with-the-session",
    closedTab &&
      reopened &&
      restored?.badge === "changes requested" &&
      restored?.findings.length === 2,
    JSON.stringify({
      closedTab,
      reopened,
      badge: restored?.badge,
      findings: restored?.findings.length,
    }),
  );
  verdict(
    "review-reopened-session-shows-its-transcript",
    restoredTools.includes("write_file"),
    JSON.stringify(restoredTools),
  );

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

  await evaluate(ws, `document.getElementById("effortBtn")?.click()`);
  await sleep(200);
  const effortItems = await evaluate(
    ws,
    `[...document.querySelectorAll("#effortMenu button .m-label")].map((b) => b.textContent)`,
  );
  verdict(
    "effort-menu-offers-max",
    effortItems.includes("max"),
    JSON.stringify(effortItems),
  );
  await evaluate(
    ws,
    `document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))`,
  );
  await sleep(300);

  await evaluate(ws, `document.getElementById("btnSettings")?.click()`);
  await sleep(400);
  await evaluate(
    ws,
    `document.querySelector(".settings-nav [data-nav='models']")?.click()`,
  );
  await sleep(300);
  await evaluate(ws, `document.getElementById("mAdd")?.click()`);
  await sleep(200);
  await evaluate(
    ws,
    `(() => {
  const set = (id, v) => {
    const input = document.getElementById(id);
    input.value = v;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  set("fProviderName", "Fixture Models");
  set("fBase", ${JSON.stringify(ENDPOINT)});
  set("fModel", "live-model");
  document.getElementById("fModel").dispatchEvent(new Event("blur"));
  return true;
})()`,
  );
  let prefill = {};
  for (let i = 0; i < 15; i++) {
    await sleep(400);
    prefill = await evaluate(
      ws,
      `(() => {
  const val = (id) => document.getElementById(id)?.value ?? "";
  const note = document.getElementById("fInfoNote");
  return {
    name: val("fName"),
    ctx: val("fCtx"),
    max: val("fMax"),
    note: note?.textContent ?? "",
    noteShown: !note?.hidden,
    imagesOn: document.querySelector("#swImages")?.classList.contains("on"),
    toolsOn: document.querySelector("#swTools")?.classList.contains("on"),
  };
})()`,
    );
    if (prefill.noteShown) break;
  }
  verdict(
    "models-form-prefills-from-endpoint",
    prefill.ctx === "131072" &&
      prefill.max === "16384" &&
      prefill.name === "Live Fixture Model" &&
      prefill.imagesOn === false &&
      prefill.toolsOn === false &&
      /auto-filled from \/models/.test(prefill.note),
    JSON.stringify(prefill),
  );

  await evaluate(
    ws,
    `(() => {
  const ctx = document.getElementById("fCtx");
  ctx.value = "999";
  ctx.dispatchEvent(new Event("input", { bubbles: true }));
  document.getElementById("fModel").dispatchEvent(new Event("blur"));
  return true;
})()`,
  );
  await sleep(1200);
  const overridden = await evaluate(
    ws,
    `document.getElementById("fCtx")?.value ?? ""`,
  );
  verdict(
    "models-prefill-is-overridable",
    overridden === "999",
    `ctx=${overridden}`,
  );

  await evaluate(ws, `document.getElementById("fBack")?.click()`);
  await sleep(800);
  await evaluate(ws, `document.getElementById("mAdd")?.click()`);
  await sleep(200);
  await evaluate(
    ws,
    `(() => {
  const set = (id, v) => {
    const input = document.getElementById(id);
    input.value = v;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  set("fProviderName", "Fixture Models");
  set("fBase", ${JSON.stringify(ENDPOINT)});
  set("fModel", "writer-model");
  return true;
})()`,
  );
  await evaluate(ws, `document.getElementById("fSave")?.click()`);
  let backstop = {};
  for (let i = 0; i < 15; i++) {
    await sleep(400);
    backstop = await evaluate(
      ws,
      `(() => {
  const status = document.getElementById("setStatus");
  const formErr = document.getElementById("fFormErr");
  const group = [...document.querySelectorAll(".prov-group")].find((g) =>
    g.querySelector(".pg-name")?.textContent.includes("Fixture Models"),
  );
  const row = group
    ? [...group.querySelectorAll(".prov-table tr")].find((r) =>
        r.textContent.includes("writer-model"),
      )
    : null;
  return {
    status: status?.textContent ?? "",
    statusOk: status?.classList.contains("ok") ?? false,
    formErr: formErr && !formErr.hidden ? formErr.textContent : "",
    rowTags: row ? [...row.querySelectorAll(".tag")].map((t) => t.textContent) : [],
  };
})()`,
    );
    if (backstop.rowTags.some((t) => t.includes("k ctx"))) break;
  }
  verdict(
    "models-backstop-fills-on-save",
    backstop.rowTags.includes("8k ctx") &&
      backstop.statusOk &&
      /auto-configured from \/models: context window 8192/.test(
        backstop.status,
      ),
    JSON.stringify(backstop),
  );

  ws.close();
} catch (err) {
  say(`FAILED: ${err.message}`);
  results.push({ name: "harness", ok: false });
} finally {
  try {
    liveState.server?.close();
  } catch {}
  try {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
    });
  } catch {}
  cleanupMine();
  await sleep(300);
}

pass = results.length > 0 && results.every((r) => r.ok);
if (globalThis.__jsErrors?.length) {
  lines.push("");
  lines.push("--- page exceptions ---");
  for (const e of globalThis.__jsErrors) lines.push(e);
}
say(
  `RESULT: ${pass ? "PASS" : "FAIL"} (${results.filter((r) => r.ok).length}/${results.length})`,
);
fs.mkdirSync(path.dirname(REPORT), { recursive: true });
fs.writeFileSync(REPORT, lines.join("\n") + "\n", "utf-8");
say(`report: ${REPORT}`);
process.exit(pass ? 0 : 1);
