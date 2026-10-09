# Harness

A minimal desktop coding agent (personal educational build).
PySide6 GUI + Python agent engine, one process, no browser, no JavaScript stack.
Inspired by the [Neural Code walkthrough](https://youtu.be/Lu1UWqVTbQg) - renamed to `harness` for personal use.

## Run

```powershell
uv sync
uv run harness
```

The window opens with the last active provider. Type a message, Enter sends it
(Shift+Enter for a newline). The round send/stop button shows an arrow when
idle and an animated spinner while the agent works; clicking it stops the turn
between steps (the in-flight model call finishes first).

## GUI tour

- Composer: message box plus Build/Plan mode, provider/model picker, reasoning
  effort picker, permission level (Default, Strict, Allow All, Autopilot),
  send/stop button, Settings. Project and git branch shown underneath.
- Left: sessions with LLM-generated titles (rename/delete via right-click,
  current session in bold). The Sessions and Plan panels collapse via the top
  bar toggles so the chat takes the width.
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
or a mid-session switch notice. Reasoning effort
and temperature are per-provider settings, forwarded to the API only when set.

## Providers and keys

Non-secret config lives in `providers.json` (id, name, base_url, model,
disabled, active id). Secrets live in the OS keyring:

- Windows Credential Manager, macOS Keychain, Linux Secret Service
- service `harness`, one entry per provider id

The GUI never displays full secrets, only masked (`sk-...xyz`) and key status.
Settings offers Add/Edit/Delete, Set active, a Test button (runs the
`harness-ok` probe and reports latency), and one-time Migrate (imports
`settings.json`/`.env` keys into the keyring).

Fallback: if a provider has no keyring entry, `./.env` (then `~/.agents/env`)
is used and flagged as plaintext in Settings. `settings.json`, `.env`,
`providers.json`, and `*.jsonl` session logs are git-ignored.

## Sessions

Chats live under `~/.agents/sessions/<project>/` as one JSONL file each, with
a `titles.json` sidecar. New sessions get an LLM-generated title after the
first turn; rename or delete any session by right-clicking it (renames only
touch the sidecar, so the transcript and resume keep working). An early
Windows build saved sessions next to the code instead; the app moves those
into place on startup.

## Architecture

```text
PySide6 GUI  <-- events / interact -->  agent engine (Runner)
```

- `harness/events.py`: bus (`message`, `message_delta`, `tool_call/result`,
  `todos`, `usage`, `status`, `note`, `session`, `compacted`, `error`, ...) and
  blocking `interact()` for approvals and picks. No UI imports.
- `harness/agent.py`: `Runner` emits events; same turn logic as the original
  loop (cap/strip/compact, permissions, subagents unchanged).
- `harness/permissions.py`: allow/ask/deny policy with modes (default, strict,
  allow-all, autopilot). The deny list wins in every mode; autopilot only ever
  auto-allows what the policy would otherwise ask about.
- `harness/gui/`: `bridge.py` runs the Runner on a worker thread (Qt signals
  cross the boundary), plus main window, chat view, dialogs, settings.
- `harness/providers.py`: registry + keyring + migration + connection test.
- `harness/llm.py`: lazy per-provider client, optional token streaming.

Possible future API clients can attach to the same event layer; none is built.

## Tests

```powershell
uv run python tests/test_engine_headless.py   # engine with no UI attached
uv run python tests/test_providers.py         # registry, masking, fallback
uv run python tests/test_gui_smoke.py         # offscreen drive of the real window
uv run python tests/test_sessions.py          # storage, migration, LLM titles
uv run python tests/test_permissions.py       # policy modes, deny-list supremacy
uv run python tests/test_llm_params.py        # effort/temperature forwarding
```

## Notes

- On Windows there is no OS sandbox (`sandbox: none`). The approval dialog is
  the guardrail: `rm`, `sudo`, `curl`, `git push`-style commands stay denied
  in every permission mode.
- `.explain/` holds personal study notes (git-ignored).
