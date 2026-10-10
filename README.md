# Harness

A minimal desktop coding agent (personal educational build).
Electron shell + Python agent engine: the UI is TypeScript/HTML, the engine
stays Python, and the two talk over a local WebSocket.
Inspired by the [Neural Code walkthrough](https://youtu.be/Lu1UWqVTbQg) - renamed to `harness` for personal use.

## Run

```powershell
uv sync
cd electron
npm install
npm start
```

Electron spawns the Python sidecar itself (`uv run python -m harness.server`),
picks a free localhost port and loads the renderer against it. The window opens
with the last active workspace and provider. Type a message, Enter sends it
(Shift+Enter for a newline). The round send/stop button shows an arrow when
idle and an animated spinner while the agent works; clicking it stops the turn
between steps (the in-flight model call finishes first).

The engine can also be run on its own, with no UI at all:

```powershell
uv run harness --port 8765 --project-root . --app-data-dir ~/.agents
```

## UI tour

- Composer: message box plus Build/Plan mode, provider/model picker, reasoning
  effort picker, permission level (Default, Strict, Allow All, Autopilot),
  send/stop button, Settings. Workspace and git branch shown in the context
  panel.
- Left: workspaces, each listing its sessions with LLM-generated titles
  (rename/delete via right-click, current session highlighted). New session
  opens as a tab; tabs, sidebar and the backend stay on one session id.
- Center: chat with markdown answers, compact one-line tool entries (output
  collapsed until opened, denied/blocked/skipped calls badged), a live
  activity line narrating each step, thinking indicator, red error cards.
  Each assistant message streams into one bubble and is finalized in place.
- Right: plan checklist (from `write_todos`) and running token totals.

Plan mode is read-only: fresh Plan sessions start with a planning-identity
system prompt, mid-session switches add an explicit mode notice to the
transcript (plus a per-turn reminder), and any write call is refused before
it runs, so the model describes changes instead of making them. Sessions
started in Build keep their build identity; switch modes with a new session
or a mid-session switch notice. Reasoning effort and temperature are per-model
settings, forwarded to the API only when set, and only to models that support
them.

## Project rules

Every workspace can keep its instructions in `AGENTS.md`, and they are
injected into the system prompt the model sees: the workspace's own file
first, then `~/.agents/AGENTS.md` as the global fallback, each capped at
20,000 characters. The prompt says outright that where the project rules
disagree with the app's own defaults, the project rules win. Skills are
listed the same way but by name and description only - their bodies are read
on demand with `read_skill`, so a 26 KB skill costs nothing until it is used.
The rules are re-read whenever a session starts, so editing `AGENTS.md` takes
effect on the next session in that workspace.

## Providers, models and keys

Non-secret config lives in `providers.json`: providers, each holding its own
models (id, name, base_url, disabled, plus a `models` list). Secrets live in
the OS keyring:

- Windows Credential Manager, macOS Keychain, Linux Secret Service
- service `harness`, one entry per provider id

A provider owns the connection and the credentials; each model owns its
display name, its API model id and its own settings (reasoning effort,
temperature, context window, output token limit, tool calling, extra
parameters). Ids are generated and stable, so several models can share one
provider (GLM 5.3 Flash and DeepSeek V4.1 Flash under NVIDIA), each can be
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
`harness/retry.py` retries only the transient ones, up to `MAX_ATTEMPTS`
attempts inside one shared window that is sized to span all of them. The
window is only a backstop: a first attempt that burns its whole read timeout
still leaves a whole retry behind it, because a 30s window that could not
hold two 20s timeouts meant a slow provider killed conversations instead of
getting the second chance it needed. The read timeout itself is 45s - long
enough for a reasoning model's first token at high effort, short enough that
a provider that has gone quiet is cut off and retried instead of costing a
stall per attempt. Backoff is exponential with jitter, `Retry-After` is
respected when it fits the budget, one retry slot per endpoint so a failing
host is not hammered, and every wait is cancellable (Stop, or switching
model). A bad key or an unknown model id fails straight away; a stream that
breaks after text has already arrived is reported rather than silently
re-sent, since sending it again would duplicate the answer.

The composer shows one status area that ticks as retries progress
(`Retry 2 · waiting 3s · 5s of 150s elapsed`) and a final error card with
the provider's own message, a suggested next step taken from the HTTP status
(`401` wants a new key, `404` a different model id, `429` a wait and a
Retry), and **Retry** / **Switch model**. The card is part of the
conversation: it survives the transcript reconcile that follows a turn, and
it stays put when you switch sessions. Retrying rewinds the conversation to
the message that failed - the message is never duplicated - and leaves
project files alone. Switching model keeps the workspace, session, history,
unsaved composer text and permission state, and while a request is in flight
it stops that request first rather than letting two models write into one
session. No restart is ever needed to recover from an upstream failure.

## Sessions

Chats live under `~/.agents/sessions/<project>/` as one JSONL file each, with
a `titles.json` sidecar. New sessions get an LLM-generated title after the
first turn; rename or delete any session by right-clicking it - the workspace
does not have to be open, the op carries its own root - and it disappears
from the sidebar at once, closing any tab that had it. Workspaces themselves
can be removed from the list by right-clicking their row: their tabs close,
their sessions leave the sidebar, and the folder on disk is untouched. An
early Windows build saved sessions next to the code instead; the app moves
those into place on startup.

## Architecture

```text
Electron renderer  <-- WebSocket -->  harness/server.py  <-- events -->  agent engine (Runner)
```

- `electron/`: main process (spawns the sidecar, picks the port, window and
  workspace plumbing) and the renderer (tabs, chat, composer, Settings).
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
- `harness/llm.py`: lazy per-provider client, optional token streaming, the
  request body for the active model.
- `harness/errors.py` + `harness/retry.py`: failure classification and the
  bounded retry loop `call_llm` runs inside.

Possible future API clients can attach to the same event layer; none is built.

## Tests

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
```

The renderer has no unit harness, so its rules are checked end to end:
`node electron/scripts/renderer-check.mjs` launches the app against a
provider whose endpoint is dead, drives the composer and the sidebar over
the DevTools Protocol, and writes a pass/fail report to
`tests/renderer-check.txt`: the retry card ticking, the error card offering a
next step and surviving both the post-turn reconcile and a session switch,
a deleted session leaving the sidebar at once (tab or no tab, workspace open
or not), the workspace removal, and the plan panel starting closed.

## Notes

- On Windows there is no OS sandbox (`sandbox: none`). The approval dialog is
  the guardrail: `rm`, `sudo`, `curl`, `git push`-style commands stay denied
  in every permission mode.
- `.explain/` holds personal study notes (git-ignored).
