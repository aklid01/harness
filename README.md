# Harness

Harness is a personal desktop coding agent. A Tauri 2 desktop shell hosts a
TypeScript web UI and a Python engine connected over a local WebSocket.

## Quick start

Requirements: Python with `uv`, Node.js, Rust, and the Tauri 2 prerequisites.

```powershell
uv sync
cd desktop
npm install
npm run dev
```

`npm run dev` builds the web UI, starts the Tauri app, and lets the app start
the Python sidecar. For a release build without an installer bundle:

```powershell
npm run build
```

The engine can also run without the desktop app:

```powershell
uv run python -m harness.server --port 8765 --project-root . --app-data-dir <data-dir>
```

## Using Harness

Type a request in the composer and press Enter. Use Shift+Enter for a newline.
The default build mode can inspect and edit the workspace through the agent's
tools. The send button becomes Stop while a turn is running.

Commands:

```text
/plan <message>   plan one turn without editing files
/ask <message>    answer one question without editing files
/build <message>  build one turn
/rewind           return to an earlier point in the session
/sessions         browse previous sessions
/compact          summarize the current history
/multi-model-review  review the current change with configured reviewers
```

A bare `/plan` or `/ask` makes that mode sticky until `/build` or the mode
control clears it. An ambiguous build request may enter planning automatically;
the UI lets you approve the plan, skip it, or cancel it.

Sessions, workspaces, model selection, permissions, plans, token usage, and
provider settings are managed in the desktop UI. Messages can be steered into
a running turn or queued for the next turn. Files can be attached or dropped
onto the composer; they are copied into session-scoped storage and treated as
read-only input.

## Providers and configuration

Providers and models are configured in `providers.json`. API keys are stored in
the operating system keyring when possible. The app can fall back to `.env` or
`~/.agents/env` for a key belonging to the configured provider, and labels that
key as plaintext in Settings.

Workspace instructions can be placed in `AGENTS.md`. The engine also reads
global instructions from `~/.agents/AGENTS.md` and the app data directory.
These files are injected into the model context at the start of a session.

Session data is stored under the app data directory, normally
`%APPDATA%\\Harness`. Local settings, provider files, keys, and session logs
should not be committed.

## Safety and failures

Permission modes control tool execution. Read-only plan and ask turns withhold
write tools, and denied commands remain denied in every permission mode.
Attached files are staged outside the workspace and are never edited in place.
Treat their contents as untrusted data.

Transient provider failures are classified and retried with bounded delays.
Authentication errors and unknown model IDs fail immediately. A failed request
keeps the session alive and offers retry or model switching in the UI.

## Multi-model review

The optional multi-model review is advisory and read-only. Configure reviewers
in Settings, then run `/multi-model-review` or use the headless command:

```powershell
uv run python -m harness.review --scope uncommitted --json
```

Reviewers receive the selected diff, not tools or another reviewer's response.
Secrets are redacted before review, and applying findings sends them back
through the normal agent and permission flow.

## Architecture

```text
Tauri webview  <-- local WebSocket -->  harness/server.py  <-- events -->  Runner
```

- `desktop/` contains the Tauri shell and web renderer.
- `harness/server.py` exposes the local WebSocket sidecar.
- `harness/agent.py` runs turns, tools, permissions, planning, and subagents.
- `harness/events.py` defines the UI-independent event bus.
- `harness/providers.py` manages providers, models, keys, and migrations.
- `harness/attachments.py` stages files and extracts readable content.
- `harness/review/` implements multi-model review and report persistence.

## Development checks

Run the full engine suite:

```powershell
python scripts/run_tests.py
```

Run the renderer end to end check:

```powershell
node desktop/scripts/renderer-check.mjs
```

Check the desktop shell and web build:

```powershell
cd desktop/src-tauri
cargo check
cd ..
npm run typecheck
npm run build:web
```

The renderer check writes its report to `tests/renderer-check.txt`.
