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
or a mid-session switch notice. Reasoning effort and temperature are per-model
settings, forwarded to the API only when set, and only to models that support
them.

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

The GUI never displays full secrets, only masked (`sk-...xyz`) and key status.
Settings offers Add model, Add model to an existing provider, Edit, Test
(runs the `harness-ok` probe and reports latency), Delete model, Delete
provider, Set active, and one-time Migrate (imports `settings.json`/`.env`
keys into the keyring).

Fallback: if a provider has no keyring entry, `./.env` (then `~/.agents/env`)
is used and flagged as plaintext in Settings - but only when that key points
at the provider's own host, so a mismatched key fails fast with "no API key"
instead of a confusing 401. `settings.json`, `.env`, `providers.json`,
`providers.json.bak` and `*.jsonl` session logs are git-ignored.

## When a request fails

A failed model request never ends the session. `harness/errors.py` classifies
the failure - connection, DNS, timeout, 408/429/5xx, auth, unknown model id,
unsupported parameter, malformed body, interrupted stream - and
`harness/retry.py` retries only the transient ones inside a single monotonic
30-second window, with exponential backoff and jitter, `Retry-After` respected
when it fits the deadline, one retry slot per endpoint so a failing host is
not hammered, and every wait cancellable (Stop, or switching model). A bad
key or an unknown model id fails straight away; a stream that breaks after
text has already arrived is reported rather than silently re-sent, since
sending it again would duplicate the answer.

The composer shows one status area that updates as retries progress
(`Retry 2 · waiting 3s · 5s of 30s elapsed`) and a final error card with
**Retry** and **Switch model**. Retrying rewinds the conversation to the
message that failed - the message is never duplicated - and leaves project
files alone. Switching model keeps the workspace, session, history, unsaved
composer text and permission state, and while a request is in flight it
stops that request first rather than letting two models write into one
session. No restart is ever needed to recover from an upstream failure.

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
  `todos`, `usage`, `status`, `note`, `session`, `compacted`, `error`, `retry`,
  ...) and blocking `interact()` for approvals and picks. No UI imports.
- `harness/agent.py`: `Runner` emits events; same turn logic as the original
  loop (cap/strip/compact, permissions, subagents unchanged).
- `harness/permissions.py`: allow/ask/deny policy with modes (default, strict,
  allow-all, autopilot). The deny list wins in every mode; autopilot only ever
  auto-allows what the policy would otherwise ask about.
- `harness/gui/`: `bridge.py` runs the Runner on a worker thread (Qt signals
  cross the boundary), plus main window, chat view, dialogs, settings.
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
