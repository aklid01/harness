# Harness

A minimal desktop coding agent (personal educational build).
Tauri 2 shell + Python agent engine: the UI is TypeScript/HTML, the engine
stays Python, and the two talk over a local WebSocket.
Inspired by the [Neural Code walkthrough](https://youtu.be/Lu1UWqVTbQg) - renamed to `harness` for personal use.

## Run

```powershell
uv sync
cd desktop
npm install
npm run dev          # build:web + tauri dev
npm run build        # release build without the installer bundle
```

The Tauri shell spawns the Python sidecar itself (`uv run python -m harness.server`,
falling back to the project venv interpreter), picks a free localhost port and
loads the renderer against it, and kills the whole sidecar process tree on
exit. The window opens with the last active workspace and provider. Type a
message, Enter sends it (Shift+Enter for a newline). The round send/stop
button shows an arrow when idle and an animated spinner while the agent works;
clicking it stops the turn between steps (the in-flight model call finishes
first).

The engine can also be run on its own, with no UI at all:

```powershell
uv run python -m harness.server --port 8765 --project-root . --app-data-dir <data-dir>
```

## UI tour

- Composer: message box plus Build/Ask/Plan mode, provider/model picker,
  reasoning effort picker, permission level (Default, Strict, Allow All,
  Autopilot), attach-files button, send/stop button, Settings. Workspace and
  git branch shown in the context panel.
- Left: workspaces, each listing its sessions with LLM-generated titles
  (rename/delete via right-click, current session highlighted). New session
  opens as a tab; tabs, sidebar and the backend stay on one session id.
- Center: chat with markdown answers, compact one-line tool entries (output
  collapsed until opened, denied/blocked/skipped calls badged), a live
  activity line narrating each step, thinking indicator, red error cards.
  Each assistant message streams into one bubble and is finalized in place.
- Right: plan checklist (from `write_todos`) and running token totals.

Ask mode is read-only Q&A: fresh Ask sessions start with a research-identity
system prompt, the write tools are withheld exactly as in Plan mode, and any
bash command the permission classifier does not rate as read-only is refused
outright - no approval dialog offers an edit path. Read-only commands
(grep, ls, git status/diff/log, ...) still run silently.

Plan mode is read-only: fresh Plan sessions start with a planning-identity
system prompt, mid-session switches add an explicit mode notice to the
transcript (plus a per-turn reminder), and any write call is refused before
it runs, so the model describes changes instead of making them. Sessions
started in Build keep their build identity; switch modes with a new session
or a mid-session switch notice. Reasoning effort and temperature are per-model
settings, forwarded to the API only when set, and only to models that support
them.

The mode control locks while a turn runs: a mode binds a whole turn, so stop
the turn to switch. Permission levels stay changeable mid-turn by design.

While a turn runs, typing and pressing Enter files the message instead of
dropping it. A **steer** joins the running conversation at the next
model-call boundary (the current tool or answer finishes first), so the model
adapts work in progress; if the turn ends first, the steer auto-sends after
it. A **queue** waits for the turn to end, then runs as the next turn
automatically. Pending messages show above the composer with a STEER or
QUEUE badge and can be edited or removed until they are sent. The default
(Enter) action is Queue, changeable in Settings > General; the caret next to
Send picks the other one per message. If the provider fails, queued messages
are discarded and named in the error card (their text returns to the
composer); steers stay pending. A manual Stop keeps everything pending.

Models that stream reasoning show it in a collapsible "Thinking" block above
the answer (toggle next to the effort picker; the control appears only for
models with reasoning enabled). Reasoning is kept in the transcript but is
never sent back to the provider.

## Attachments

Attach files to a message with the paperclip button or by dropping them on
the composer. Every file type is accepted - nothing is filtered by extension
or MIME type. Attached files show as chips (name, type, size) above the
composer, can be removed before sending, and stay on the sent message.

Attachments work in every submission path: a normal send, a mid-turn steer,
and a queued message. Pending items keep their files while they wait, and a
message can be nothing but files.

Each file is copied into session-scoped storage under the app data area
(`sessions/attachments/<project>/<session id>/`), never into the workspace
and never modified where the user left it. The agent receives a manifest with
the name, type, size and path of every file, delivered with the message
itself, so it can refer to a file by name in later turns. The manifest is
repeated every turn, so a trimmed or compacted transcript still says which
files exist.

The agent reads attachments with its usual `read_file`: text files come back
as text, and PDFs and Office documents are extracted to text first (PDF text
needs `pypdf`, which `uv sync` installs). A file with no readable text - a
scan, a legacy `.doc`, an archive - says so plainly instead of handing back
mojibake, and the agent is told to report that rather than guess. Images are
passed to the model as images when the model is marked as able to see them
(Settings > Models); otherwise they are delivered as a path only.

The manifest also gives the agent the staged path, so it can look at an
attachment with the shell (`ls`, `head`) as well as with `read_file`. That
works in Ask mode too: a command the permission layer rates as read-only
runs there without a prompt, whatever shape it has - a merged stderr and a
shell variable included.

Attached files are read-only: the agent cannot edit them in place, and any
change it makes goes to a copy in the workspace. Reading them needs no
permission prompt in any permission mode, in every workflow (Build, Ask, Plan
and Plan -> Build, whose planner is told about them too), and subagents can
read them too.

Limits are configurable with `HARNESS_MAX_ATTACHMENT_BYTES` (25 MB per file)
and `HARNESS_MAX_ATTACHMENT_FILES` (10 files per message). A file over the
limit is refused by name, with the limit, and the rest of the message still
goes. Two files with the same name both survive - the second one is staged
as `name (2).ext`.

The session log keeps the attachment metadata only (name, type, size, staged
path), never the bytes. Deleting the session deletes the files with it.

Attached content is untrusted data. Instructions inside a file are content to
analyse, not orders to follow, and the system prompt says so.

## Project rules

Every workspace can keep its instructions in `AGENTS.md`, and they are
injected into the system prompt the model sees. Three files are read, project
first then global, each capped at 20,000 characters:

1. `<workspace>\AGENTS.md` - the project's own rules
2. `~\.agents\AGENTS.md` - the engine's global rules
3. `<app-data>\AGENTS.md` - the app profile's global rules (`%APPDATA%\Harness`
   by default, alongside `providers.json`; the old `harness-electron` tree is
   adopted on first launch)

The prompt says outright that where the project rules disagree with the app's
own defaults, the project rules win. Skills are listed the same way but by
name and description only - their bodies are read on demand with
`read_skill`, so a 26 KB skill costs nothing until it is used. The rules are
re-read whenever a session starts, so editing `AGENTS.md` takes effect on the
next session in that workspace.

## Providers, models and keys

Non-secret config lives in `providers.json`: providers, each holding its own
models (id, name, base_url, disabled, plus a `models` list). Secrets live in
the OS keyring:

- Windows Credential Manager, macOS Keychain, Linux Secret Service
- service `harness`, one entry per provider id

A provider owns the connection and the credentials; each model owns its
display name, its API model id and its own settings (reasoning effort,
temperature, context window, output token limit, tool calling, image support,
extra parameters). Ids are generated and stable, so several models can share
one provider (GLM 5.3 Flash and DeepSeek V4.1 Flash under NVIDIA), each can be
edited, renamed or deleted on its own, and none of them overwrite another.
The one-model-per-provider file the early builds wrote migrates on first use;
the original is kept as `providers.json.bak` and the Settings panel reports
what moved.

The UI never displays full secrets, only masked (`sk-...xyz`) and key status.
Settings offers Add model, Add model to an existing provider, Edit, Test
(runs the `harness-ok` probe, then a side-effect-free tool-calling probe, and
reports latency plus the model it tested), Delete model, Delete provider, Set
active, and one-time Migrate (imports `settings.json`/`.env` keys into the
keyring).

Fallback: if a provider has no keyring entry, `./.env` (then `~/.agents/env`)
is used and flagged as plaintext in Settings - but only when that key points
at the provider's own host, so a mismatched key fails fast with "no API key"
instead of a confusing 401. `settings.json`, `.env`, `providers.json`,
`providers.json.bak` and `*.jsonl` session logs are git-ignored.

## When a request fails

A failed model request never ends the session. `harness/errors.py` classifies
the failure - connection, DNS, timeout, 408/429/5xx, auth, unknown model id,
unsupported parameter, malformed body, interrupted stream - and
`harness/retry.py` retries only the transient ones, on a fixed schedule:
3s, 5s, 15s, 20s and 30s before retries one through five. The schedule runs
inside one shared window (343s) sized to span every attempt at its full read
timeout plus every scheduled wait, so a first attempt that burns its whole
45s read timeout still leaves the whole schedule behind it - a 30s window
that could not hold two 20s timeouts meant a slow provider killed
conversations instead of getting the second chance it needed. `Retry-After`
is respected when it fits the window, one retry slot per endpoint so a
failing host is not hammered, and every wait is cancellable (Stop, or
switching model). A bad key or an unknown model id fails straight away; a
stream that breaks after text has already arrived is reported rather than
silently re-sent, since sending it again would duplicate the answer.

The composer shows one status area for the whole retry process: the exact
error the upstream returned (`the provider reported a server error:
overloaded (HTTP 503)`), a **Things to do** line taken from the failure kind
and status (`401` wants a new key, `404` a different model id, `429` a wait
and a Retry), and the countdown (`Retry 2 of 5 · trying again in 5s`, then
`attempt in flight…`). When the retries are spent, a final error card carries
the provider's own message, the same next-step hint, and **Retry** /
**Switch model**. The card is part of the conversation: it survives the
transcript reconcile that follows a turn, and it stays put when you switch
sessions. Retrying rewinds the conversation to the message that failed - the
message is never duplicated - and leaves project files alone. Switching model
keeps the workspace, session, history, unsaved composer text and permission
state, and while a request is in flight it stops that request first rather
than letting two models write into one session. No restart is ever needed to
recover from an upstream failure.

## Sessions

Chats live under the app data dir (`%APPDATA%\Harness\sessions\<project>\`
by default; change it in Settings > Storage) as one JSONL file each, with a
`titles.json` sidecar. A legacy `~/.agents/sessions` tree is migrated on
startup. New sessions get an LLM-generated title after the
first turn; rename or delete any session by right-clicking it - the workspace
does not have to be open, the op carries its own root - and it disappears
from the sidebar at once, closing any tab that had it. Workspaces themselves
can be removed from the list by right-clicking their row: their tabs close,
their sessions leave the sidebar, and the folder on disk is untouched. An
early Windows build saved sessions next to the code instead; the app moves
those into place on startup.

## Architecture

```text
Tauri webview (renderer)  <-- WebSocket -->  harness/server.py  <-- events -->  agent engine (Runner)
```

- `desktop/`: the Tauri 2 shell. `src-tauri/src/` (Rust) spawns and tree-kills
  the sidecar, picks the port, owns workspace and storage plumbing over IPC;
  `web/` holds the renderer (tabs, chat, composer, Settings) and the
  `window.harness` bridge over Tauri IPC. Build: `npm run build:web`,
  typecheck: `npm run typecheck`.
- `harness/server.py`: the sidecar. One WebSocket, one `Session` per client
  key, so many tabs and workspaces share one engine process.
- `harness/events.py`: bus (`message`, `message_delta`, `tool_call/result`,
  `todos`, `usage`, `status`, `note`, `session`, `compacted`, `error`, `retry`,
  ...) and blocking `interact()` for approvals and picks. No UI imports.
- `harness/agent.py`: `Runner` emits events; same turn logic as the original
  loop (cap/strip/compact, permissions, subagents unchanged).
- `harness/permissions.py`: allow/ask/deny policy with modes (default, strict,
  allow-all, autopilot). The deny list wins in every mode; autopilot only ever
  auto-allows what the policy would otherwise ask about.
- `harness/providers.py`: registry + keyring + migration + connection test.
- `harness/attachments.py`: files the user attached - staging under the app
  data area, the manifest the agent reads, text extraction, the limits.
- `harness/llm.py`: lazy per-provider client, optional token streaming, the
  request body for the active model.
- `harness/errors.py` + `harness/retry.py`: failure classification and the
  bounded retry loop `call_llm` runs inside.

Possible future API clients can attach to the same event layer; none is built.

## Tests

One entrypoint runs the whole engine suite (20 modules) and its exit code is
the verdict:

```powershell
python scripts/run_tests.py                  # or: uv run python scripts/run_tests.py
```

Single files run the same way:

```powershell
uv run python tests/test_engine_headless.py   # engine with no UI attached
uv run python tests/test_providers.py         # registry, masking, fallback
uv run python tests/test_provider_models.py   # several models per provider, migration
uv run python tests/test_retry.py             # classification, deadline, cancel
uv run python tests/test_llm_retry.py         # call_llm retries, stream breaks
uv run python tests/test_model_switch.py      # retry after switching models
uv run python tests/test_session_lifecycle.py # one session, one tab, one request per turn
uv run python tests/test_tool_flow.py         # tool calls end to end against a mock endpoint
uv run python tests/test_text_tools.py        # XML text tool calls: parse, validate, refuse
uv run python tests/test_sessions.py          # storage, migration, LLM titles
uv run python tests/test_permissions.py       # policy modes, deny-list supremacy
uv run python tests/test_llm_params.py        # effort/temperature forwarding
uv run python tests/test_agents.py            # plan/build agent routing
uv run python tests/test_e2e_workflow.py      # plan -> build -> oversight, end to end
uv run python tests/test_instructions.py      # AGENTS.md injection, 20k cap
uv run python tests/test_oversight.py         # decision review gate
uv run python tests/test_paths.py             # workspace/app-data resolution
uv run python tests/test_sandbox.py           # sandbox wrap/run
uv run python tests/test_server_protocol.py   # sidecar ops and replies
uv run python tests/test_attachments.py       # attach, stage, manifest, limits
```

The renderer has no unit harness, so its rules are checked end to end:
`node desktop/scripts/renderer-check.mjs` builds the web assets, launches the
real Tauri window against a provider whose endpoint is dead, drives the
composer and the sidebar over the DevTools Protocol (WebView2 remote
debugging), and writes a pass/fail report to `tests/renderer-check.txt`: the
retry card ticking, the error card offering a
next step and surviving both the post-turn reconcile and a session switch,
a deleted session leaving the sidebar at once (tab or no tab, workspace open
or not), the workspace removal, the plan panel starting closed, and the
attachment flow: picking files, taking one back, a file over the limit
refused by name, and the accepted file travelling with its message.

## Notes

- On Windows there is no OS sandbox (`sandbox: none`). The approval dialog is
  the guardrail: `rm`, `sudo`, `curl`, `git push`-style commands stay denied
  in every permission mode.
- `.explain/` holds personal study notes (git-ignored).
