"""The turn loop: one Runner drives a conversation with the model.

A turn streams model replies, executes the tool calls it proposes through the
permission and oversight layers, keeps the plan/todos/session context in step,
and hands review requests to the hook the transport installed.
"""

import itertools
import json
from types import SimpleNamespace

from . import (
    agent_roles,
    budget,
    commands,
    compact,
    events,
    history,
    oversight,
    paths,
    permissions,
    providers,
    session,
    texttools,
    tools,
)
from . import attachments as attachments_mod
from . import context as context_mod
from . import errors as errors_mod
from . import plan as plan_mod
from .context import reminder
from .llm import call_llm, system_prompt
from .review import command as review_command
from .todos import active_form
from . import tools
from .tools import available_tool_names, execute

def _has_image_parts(message):
    """Does this request message carry an image part?"""
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in content
    )


PLAN_NOTICE = (
    "<system-reminder>\n"
    "Your operational mode is PLAN (read-only).\n"
    "You are a planning assistant right now, not a coding agent. Explore, explain, "
    "and propose concrete changes, but do NOT call write_file or str_replace - they "
    "are blocked and the call will be wasted.\n"
    "If asked which mode you are in, say plan mode.\n"
    "</system-reminder>"
)
BUILD_NOTICE = (
    "<system-reminder>\n"
    "Your operational mode has changed to BUILD.\n"
    "You are no longer read-only. You may edit files, run commands, and use tools.\n"
    "</system-reminder>"
)
ASK_NOTICE = (
    "<system-reminder>\n"
    "Your operational mode is ASK (read-only).\n"
    "You are a research assistant right now, not a coding agent. Answer questions "
    "and explain the codebase, but do NOT call write_file or str_replace, and do "
    "NOT run commands that modify anything - they are blocked and the call will "
    "be wasted.\n"
    "If asked which mode you are in, say ask mode.\n"
    "</system-reminder>"
)

# The handoff that carries a plan into the build phase. It is stored in the
# transcript (so it is inspectable and survives compaction) and lands as real
# context for the build agent - it is the plan, not a finished result.
PLAN_HANDOFF = (
    "<plan>\n"
    "A planning agent produced the plan below for the task you were given. Treat "
    "it as structured task context - the current plan, not completed work. "
    "Implement it, verifying with the relevant tests as you go. If inspecting the "
    "code or running tests shows a step is wrong or incomplete, do not force it: "
    "adapt, and state plainly what you changed and why.\n\n"
    "{plan}\n"
    "</plan>"
)

# The planner (and Ask mode) is read-only by construction: these tools are
# simply not offered, so the workspace cannot be changed. `task` is withheld
# too: a subagent has its own toolset and would open a second path around this
# fence. This is stronger than the read-only instruction in a prompt, mirroring
# how subagent.py fences a guest.
READONLY_WITHHELD = {"write_file", "str_replace", "write_todos", "task"}
PLAN_MAX_TURNS = 16  # a runaway planner is worse than a missing plan
PLAN_MAX_TOOLS = 40  # the planner inspects; it is not a search budget
BUILD_MAX_TOOLS = 200  # bound a runaway build; a healthy build stops on its own
PLAN_REPLANS = 1  # one targeted replan when planning yields nothing usable


# Every Runner gets its own request-id space, so a tool call made by one
# conversation can never collide with an identical call made by another.
TURN_KEYS = itertools.count(1)


class Runner:
    """One agent conversation. Emits events; never touches the screen."""

    def __init__(
        self, resume=False, debug=False, stream=False, cancel_event=None, mode="build"
    ):
        self.debug = debug
        self.stream = stream
        self.cancel_event = cancel_event
        self.mode = mode
        self._announced_mode = None
        self._turn_index = 0
        self._turn_text = ""
        self._step = 0
        self._text_retries = 0
        self._text_calls: set = set()
        self._role_instructions = ""
        self._oversight = None  # oversight config dict, or None when off
        self._checker_model_id = None
        self._plan_text = None  # plan handed to the build phase, if any
        self._plan_findings: list = []  # what the planner read, carried to build
        self._build_started = False  # idempotent: build runs at most once per task
        self._review_log: dict = {}  # decision fingerprint -> prior verdicts
        self._stop_turn = False  # set when oversight escalates: stop safely
        self._touched: set = set()  # files this session has edited (resolved)
        self._turn_touched: set = set()  # files this turn edited (resolved)
        self._reviewed_files: set = (
            set()
        )  # files whose first edit this turn was approved
        self._user_approved: set = set()  # fingerprints a human approved this turn
        self._request_prefix = f"r{next(TURN_KEYS)}"
        self._file_state = None  # git state snapshot taken at each turn start
        self.mailbox = None  # Session-owned pending steer/queue messages
        self.turn_failed = False  # the current turn ended in a provider error
        # Set by the session: how to start a multi-model review of this run.
        # None means there is nobody to run it (a bare engine, a test).
        self.review_hook = None
        self.messages = [{"role": "system", "content": system_prompt(mode)}]
        if resume:
            saved = session.all_sessions()
            if saved:
                self.messages = session.open_session(saved[0]["id"])
                history.strip(self.messages)
                events.emit(
                    events.SESSION,
                    {
                        "label": "resumed",
                        "messages": self.messages,
                        "id": session.current_id(),
                    },
                )

    def handle_input(self, user_text: str, via: str | None = None, attachments: list | None = None) -> None:
        if user_text.startswith("/"):
            self.messages = commands.handle(user_text, self.messages, self)
            session.save(self.messages)
            return

        self._receive_user_text(user_text, via=via, attachments=attachments)
        self._run_workflow(user_text)

    @property
    def touched(self) -> set:
        """The files this run edited, resolved and absolute."""
        return set(self._touched)

    def request_review(self, args: review_command.ReviewArgs) -> bool:
        """Start a multi-model review of this run's change.

        The review runs in the background and never blocks the agent: the
        card follows it as it goes, and the agent's own message is already
        on its way. Nothing here edits a file.
        """
        if self.review_hook is None:
            events.emit(
                events.NOTE,
                {"text": "reviews need the desktop app - run it from a session there"},
            )
            return False
        try:
            return bool(self.review_hook(args))
        except Exception as failure:  # noqa: BLE001 - a review must not break a turn
            events.emit(
                events.NOTE,
                {
                    "text": (
                        "the review could not start "
                        f"({type(failure).__name__}: {failure})"
                    )
                },
            )
            return False

    def _maybe_auto_review(self):
        """Offer or start a review after a run that edited files.

        Only when the user asked for it: a review that starts by itself
        spends money, so the setting is off by default and "offer" is the
        gentler of the two automatic modes. A run that changed nothing
        never triggers one.
        """
        if not self._turn_touched or self.review_hook is None:
            return
        mode = agent_roles.review_auto_run(agent_roles.get())
        if mode == "off":
            return
        self.request_review(
            review_command.new_args(
                scope="run",
                auto=True,
                offer=mode == "offer",
                paths=sorted(self._turn_touched),
            )
        )

    def _receive_user_text(self, text, via=None, attachments=None):
        """Append a user message, save it, and tell the UI.

        Shared by the opening submission and by steered messages drained
        mid-turn, so both are on disk before the model can act on them.

        The attachments that came with the message travel with it: the
        manifest is part of the message text the model reads, and the
        metadata sits on the message so a reopened session still knows which
        files belong to which message.
        """
        metas = self._adopt(attachments)
        message = {"role": "user", "content": text}
        if metas:
            message["attachments"] = metas
        self.messages.append(message)
        # Saved before the call, so a failed request never loses what was asked.
        session.save(self.messages)
        payload = {"role": "user", "content": text}
        if via:
            payload["via"] = via
        if metas:
            payload["attachments"] = metas
        events.emit(events.MESSAGE, payload)

    def _adopt(self, items):
        """Move this message's files into the session's own directory."""
        if not items:
            return []
        metas = attachments_mod.adopt(
            items, session.current_id(), paths.project_root()
        )
        failed = [m for m in metas if m.get("error")]
        if failed:
            for meta in failed:
                events.emit(
                    events.NOTE,
                    {"text": f"attachment unavailable - {meta['error']}"},
                )
        return metas

    def _session_attachments(self):
        """Every attachment this session knows about, newest last."""
        return attachments_mod.from_messages(self.messages)

    def _with_images(self, messages):
        """Put the attached images into the request, as image parts.

        Only the request changes - the transcript keeps its text form, so a
        reopened session still reads the same and the images are not stored
        twice. A provider that cannot see images is told so in the manifest
        instead, and the file is still there to be read by other means.
        """
        if not providers.active_model_capabilities().get("supports_images", True):
            return messages
        out = []
        for message in messages:
            images = []
            for item in message.get("attachments") or []:
                path = item.get("path")
                if not path or item.get("error"):
                    continue
                url = attachments_mod.image_data_url(path)
                if url:
                    images.append({"type": "image_url", "image_url": {"url": url}})
            if images and message.get("role") == "user":
                text = message.get("content") or ""
                parts = [{"type": "text", "text": text}] if text else []
                message = {**message, "content": parts + images}
            out.append(message)
        return out

    def _with_manifests(self, messages):
        """Add each message's attachment manifest to the request.

        The manifest rides with the message it belongs to, so the agent knows
        what it was given in the very turn it was given. The transcript keeps
        the plain text: the manifest is rebuilt from the metadata, which is
        what gets stored.
        """
        can_view = providers.active_model_capabilities().get("supports_images", True)
        out = []
        for message in messages:
            items = message.get("attachments") or []
            if items and message.get("role") == "user":
                block = attachments_mod.manifest(items, can_view_images=can_view)
                text = message.get("content") or ""
                content = f"{text}\n\n{block}" if text else block
                message = {**message, "content": content}
            out.append(message)
        return out

    def _without_images(self, failure, request, injection):
        """The same request with the image parts taken out, or None.

        Only for a provider that refused the picture: a rejected or
        unsupported request is the provider saying it will not take an image
        part, and losing the turn over one file would be worse than showing
        the agent the file's path instead.
        """
        if getattr(failure, "kind", None) not in ("rejected", "unsupported"):
            return None
        if not any(_has_image_parts(message) for message in request):
            return None
        events.emit(
            events.NOTE,
            {
                "text": (
                    "the provider would not accept the attached image; the file "
                    "is still attached and readable at its path"
                )
            },
        )
        plain, _ = history.prepare(
            self._with_manifests(self.messages + [injection]),
            providers.active_context_window(),
        )
        return plain

    def _run_workflow(self, user_text):
        """Run the workflow the selected mode names, in one session/transcript.

        * build      - execute directly with the build (or default) agent.
        * ask        - read-only Q&A turn: answer and research, never change.
        * plan       - read-only planning turn: propose, never implement.
        * plan-build - bounded planning, then a single automatic handoff to
                       Build (respecting any plan-approval gate). Build always
                       starts exactly once, after a usable plan exists.

        With no custom agents configured this reduces to a plain build turn,
        byte for byte as before. Mode selects the workflow; agent settings only
        choose which model each phase uses - and changing one never edits the
        other.
        """
        config = agent_roles.get()
        default_model = providers.get_active_model_id()
        self._build_started = False  # one submission = one task: reset the guard
        build_cfg = config["build"]

        # Build and Plan are the NORMAL, model-selected workflow: they run on the
        # session's selected model and never consult the plan/build/oversight
        # agents, whether or not those are configured. Only Plan -> Build is the
        # agent-orchestrated workflow. That is what keeps the two worlds separate.
        agent_mode = self.mode == "plan-build"

        if agent_mode:
            # A configured role model that no longer exists must not silently
            # fall back to another model: name it and stop.
            problem = self._missing_role_models(config)
            if problem:
                events.emit(events.PHASE, {"phase": "failed"})
                events.emit(
                    events.ERROR, {"text": problem, "kind": "config", "role": "system"}
                )
                return

        # Role instructions belong to the agent workflow only (the build agent's).
        self._role_instructions = (
            (build_cfg.get("instructions") or "")
            if agent_mode and build_cfg.get("enabled")
            else ""
        )
        # Decision Oversight is an independent feature: it is enabled on its own
        # and applies to implementation in any mode that implements (Build, and
        # the build phase of Plan -> Build), not just the agent workflow. It is
        # live only when enabled and its reviewer model still resolves.
        ov = config["oversight"]
        checker_id = ov.get("model_id") if ov.get("enabled") else None
        self._checker_model_id = checker_id or None
        self._oversight = (
            ov if (checker_id and providers.get_model(checker_id)) else None
        )
        self._plan_text = None
        self._plan_findings = []

        try:
            if self.mode == "ask":
                # Read-only Q&A on the session's own model.
                self._phase("researching")
                self._turn()
                return
            if self.mode == "plan":
                # Normal read-only planning on the session's own model.
                self._phase("planning")
                self._turn()
                return
            if self.mode == "plan-build":
                self._run_plan_build(user_text, config, default_model, build_cfg)
                return
            # Normal direct build on the session's own model - no build agent.
            self._run_build_phase(user_text, default_model, use_agent=False)
        finally:
            self._phase(None)

    def _phase(self, status):
        """Emit the current workflow phase for the UI indicator. None clears."""
        events.emit(events.PHASE, {"phase": status})

    def _missing_role_models(self, config):
        """An actionable message if a used role points at a model that is gone.

        Only the roles this workflow actually uses are checked, and only when the
        role is on with a model chosen. A disabled or unset role falls back to the
        session model as intended.
        """
        used = []
        if self.mode in ("build", "plan-build"):
            used.append(("Build", config["build"]))
        if self.mode in ("plan", "plan-build"):
            used.append(("Plan", config["plan"]))
        missing = [
            f"{label} agent model '{cfg.get('model_id')}'"
            for label, cfg in used
            if cfg.get("enabled")
            and cfg.get("model_id")
            and providers.get_model(cfg.get("model_id")) is None
        ]
        if not missing:
            return None
        return (
            "The configured "
            + " and ".join(missing)
            + " no longer exist. Re-select it in Settings, or switch the role "
            "off to use the session's model. Nothing was run."
        )

    def _persist_plan(self, task, plan_text, completion, phase, **extra):
        fields = {
            "task": task,
            "plan": plan_text or "",
            "completion": completion,
            "phase": phase,
        }
        fields.update(extra)
        try:
            plan_mod.current_save(**fields)
        except Exception:  # noqa: BLE001 - persistence is best-effort, never fatal
            pass

    def _run_build_phase(
        self, user_text, default_model, use_agent=True, build_cfg=None
    ):
        """Run the Build phase exactly once - idempotent across duplicates.

        ``use_agent`` routes to the configured Build agent (the Plan -> Build
        workflow). It is False for the normal Build mode, which runs on the
        session's own model and never consults agent settings.
        """
        if self._build_started:
            return
        self._build_started = True
        if self.cancelled():
            events.emit(events.NOTE, {"text": "stopped"})
            return
        self._phase("building")
        build_id = agent_roles.resolve_model(build_cfg, default_model) if use_agent else None
        if build_id:
            with providers.use(build_id):
                self._turn()
        else:
            self._turn()

    def _run_plan_build(self, user_text, config, default_model, build_cfg):
        plan_cfg = config["plan"]
        plan_id = agent_roles.resolve_model(plan_cfg, default_model)
        self._phase("planning")
        plan_text, completion, reason = self._plan_phase(
            user_text, plan_id, plan_cfg.get("instructions") or ""
        )
        self._plan_text = plan_text
        self._persist_plan(user_text, plan_text, completion, "planned")

        # Bounded recovery: one targeted replan when the first pass yields
        # nothing usable. Never hand an unusable plan to Build, never restart the
        # whole task. One clear message says it is trying again.
        replans = 0
        while (
            not plan_text
            and completion == plan_mod.UNUSABLE
            and replans < PLAN_REPLANS
            and not self.cancelled()
        ):
            replans += 1
            self._phase("replanning")
            events.emit(
                events.NOTE,
                {
                    "text": f"Planning did not produce a usable plan ({reason}) - "
                    "trying once more, more narrowly.",
                    "kind": "plan_replan",
                    "role": "plan",
                },
            )
            plan_text, completion, reason = self._plan_phase(
                user_text + "\n\nThe previous planning attempt did not yield a "
                "usable plan. Produce a concrete, ordered plan now, and do not "
                "spend turns exploring beyond what that plan needs.",
                plan_id,
                plan_cfg.get("instructions") or "",
            )
            self._plan_text = plan_text
            self._persist_plan(
                user_text, plan_text, completion, "planned", attempts=replans + 1
            )

        if self.cancelled():
            return
        if not plan_text:
            # Unusable, after bounded recovery. One recoverable signal (a note,
            # not a hard error) - nothing was built and the session is intact for
            # a retry, a refined request, or Plan reviewed by hand.
            events.emit(
                events.PLAN, {"status": "failed", "reason": reason, "role": "plan"}
            )
            self._phase("failed")
            plan_mod.current_save(phase="failed", completion=plan_mod.UNUSABLE)
            return

        self._phase("validating")
        if plan_cfg.get("approve") and not self._approve_plan(plan_text, config):
            # Declined: keep the plan visible in the transcript and the artifact,
            # and build nothing.
            self.messages.append({"role": "assistant", "content": plan_text})
            session.save(self.messages)
            self._persist_plan(user_text, plan_text, completion, "awaiting_user")
            return

        self._phase("handing_off")
        self._handoff_to_build(plan_text)
        self._run_build_phase(
            user_text, default_model, use_agent=True, build_cfg=build_cfg
        )

    def _handoff_to_build(self, plan_text):
        """Pass the authoritative plan to Build, exactly once.

        The plan becomes the build agent's structured context, preceded by what
        planning already explored so Build does not re-read the same files.
        Guarding on the message already being last makes a replayed or
        duplicated completion event a no-op, so Build is never launched twice.
        """
        if self.cancelled():
            return
        handoff = {"role": "user", "content": PLAN_HANDOFF.format(plan=plan_text)}
        if self.messages and self.messages[-1] == handoff:
            return
        findings = self._exploration_findings()
        if findings:
            self.messages.append({"role": "user", "content": findings})
        self.messages.append(handoff)
        session.save(self.messages)

    def _exploration_findings(self):
        """Carry what the planner read into Build so it need not re-explore.

        Capped so a deep exploration cannot crowd out the task; newest runs are
        kept, each snippet trimmed. Empty when the planner did not look around.
        """
        if not self._plan_findings:
            return ""
        budget_chars = 6000
        kept, running = [], 0
        for finding in reversed(self._plan_findings):
            block = f"### {finding['name']}: {finding['brief']}\n{finding['snippet']}"
            if running + len(block) > budget_chars and kept:
                break
            kept.insert(0, block)
            running += len(block)
        return (
            "<exploration>\nWhat the planning agent already looked at, so you "
            "do not have to re-read it. Treat these as current; verify "
            "anything the plan depends on before editing it.\n\n"
            + "\n\n".join(kept)
            + "\n</exploration>"
        )

    def _plan_phase(self, request, plan_model_id, plan_instructions):
        """A bounded, read-only planning pass on its own model.

        This is the subagent pattern (see subagent.py): a throwaway message list
        that holds read-only tools, runs the normal call/execute loop, and hands
        back only its final write-up. Nothing here is written to the session
        transcript beyond the single handoff message the caller appends, so the
        exploration cost never lands in the persisted history.
        """

        # The planner works on its own message list, so the files the user
        # attached are named here too - otherwise Plan -> Build would plan
        # without the thing the user actually attached.
        attached = self._session_attachments()
        request_message = {"role": "user", "content": request}
        if attached:
            request_message["attachments"] = attached
        messages = self._with_images(
            self._with_manifests(
                [
                    {
                        "role": "system",
                        "content": system_prompt("plan", plan_instructions),
                    },
                    request_message,
                ]
            )
        )
        toolset = [
            s
            for s in tools.TOOL_SCHEMAS
            if s["function"]["name"] not in READONLY_WITHHELD
        ]
        # The planner's own budget - separate from Build's and Oversight's.
        budget_obj = budget.Budget(turns=PLAN_MAX_TURNS, tool_calls=PLAN_MAX_TOOLS)
        progress = budget.Progress(limit=2)
        stopped = None  # why the phase stopped without finalising
        label = plan_model_id
        events.emit(
            events.PLAN,
            {
                "status": "generating",
                "request": request[:200],
                "model": None,
                "role": "plan",
            },
        )
        with providers.use(plan_model_id):
            caps = providers.active_model_capabilities()
            label = caps.get("name") or caps.get("model") or plan_model_id
            window = providers.active_context_window()
            while not self.cancelled():
                if budget_obj.turn() or budget_obj.timed_out():
                    stopped = f"the planner {budget_obj.reason()}"
                    break
                prepared, trimmed = history.prepare(messages, window)
                if trimmed:
                    events.emit(events.NOTE, {"text": trimmed})
                events.emit(events.STATUS, {"active": "planning", "role": "plan"})
                try:
                    message, usage = call_llm(
                        prepared, tools=toolset, cancel=self.cancel_event
                    )
                except Exception as failure:  # noqa: BLE001 - a reason, not a crash
                    events.emit(events.STATUS, {"active": None})
                    return (
                        None,
                        plan_mod.UNUSABLE,
                        f"the planner errored - {errors_mod.explain(failure)}",
                    )
                events.emit(events.STATUS, {"active": None})
                events.emit(events.USAGE, dict(usage))
                messages.append(message.model_dump(exclude_none=True))
                # A turn that keeps making tool calls is still exploring; only a
                # turn that stops calling is a finished plan. Its earlier
                # narration is never mistaken for the plan.
                if not message.tool_calls:
                    final = message.content or ""
                    if final.strip():
                        events.emit(
                            events.PLAN,
                            {
                                "status": "ready",
                                "plan": final,
                                "model": label,
                                "role": "plan",
                            },
                        )
                        return final, plan_mod.COMPLETE, ""
                    break
                stalled = False
                for tool_call in message.tool_calls:
                    if self.cancelled():
                        break
                    if budget_obj.tool():
                        stopped = "the planner hit its tool-call budget"
                        stalled = True
                        break
                    name = tool_call.function.name
                    call_id = getattr(tool_call, "id", None)
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except ValueError:
                        args = {}
                    if name in READONLY_WITHHELD:
                        result = f"Planning is read-only; {name} is not available here."
                    else:
                        args, result = execute(tool_call, note=(message.content or ""))
                    events.emit(
                        events.TOOL_CALL,
                        {
                            "call_id": call_id,
                            "name": name,
                            "args": args,
                            "nested": True,
                            "role": "plan",
                        },
                    )
                    events.emit(
                        events.TOOL_RESULT,
                        {
                            "call_id": call_id,
                            "name": name,
                            "args": args,
                            "result": result,
                            "nested": True,
                            "role": "plan",
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )
                    # Keep what was read, so Build can reuse it instead of
                    # re-exploring the same files. Capped, newest kept.
                    brief = str(args.get("path") or args.get("command") or name)[:200]
                    self._plan_findings.append(
                        {"name": name, "brief": brief, "snippet": (result or "")[:700]}
                    )
                    if len(self._plan_findings) > 30:
                        del self._plan_findings[: len(self._plan_findings) - 30]
                    fingerprint = (
                        f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                    )
                    if progress.repeat(fingerprint):
                        stopped = "the planner kept repeating the same step"
                        stalled = True
                        break
                if stalled:
                    break

        if self.cancelled():
            return None, plan_mod.CANCELLED, "cancelled"
        reason = stopped or "the planner ended without a finished plan"
        return None, plan_mod.UNUSABLE, reason

    def _approve_plan(self, plan_text, config):
        """Pause so the user can review the plan before it is built.

        Returns True to build. Auto-builds (True) when the review toggle is off,
        when no frontend can answer, or on cancel - a plan review is a courtesy,
        never a way to wedge the turn. Blocks on the same interaction the tool
        approval path uses, so an unanswered prompt is bounded by that timeout.
        """

        if not agent_roles.plan_wants_approval(config) or self.cancelled():
            return True
        if not tools.approval_channel_ready():
            return True  # nobody to ask; keep building rather than deadlock
        preview = (
            plan_text
            if len(plan_text) <= 3200
            else plan_text[:3200] + "\n\n… (full plan shown above)"
        )
        try:
            approved = events.interact(
                events.APPROVAL,
                {
                    "tool": "plan",
                    "reason": "Build this plan?",
                    "why": preview,
                    "risk": "low",
                    "risk_reason": "Plan approval",
                    "command": "",
                    "target": "",
                },
            )
        except Exception:  # noqa: BLE001 - never wedge the turn on the prompt
            return True
        if not approved:
            events.emit(
                events.NOTE,
                {
                    "text": "You chose not to build. The plan is above; nothing was changed. "
                    "Send a follow-up to refine it, or resubmit to plan again.",
                    "kind": "plan_skipped",
                },
            )
        return bool(approved)

    # --- decision oversight -------------------------------------------------

    def _reconcile_todos(self, name, args, result):
        """Cross out plan steps whose work just happened, from tool evidence.

        Keeps the checklist honest when the agent moves on without calling
        write_todos: a written file or a passing verification completes the step
        that asked for it, and the UI is told so it can cross it out.
        """
        if self.mode in ("plan", "ask"):
            return
        try:
            from . import todos as todos_mod

            if todos_mod.reconcile(name, args, result):
                events.emit(events.TODOS, {"todos": todos_mod.current()})
        except Exception:  # noqa: BLE001 - reconciliation is best-effort
            pass

    def _needs_review(self, name, args):
        if not self._oversight or self.mode in ("plan", "ask"):
            return False
        probe = args
        if name in ("write_file", "str_replace") and isinstance(args, dict):
            # Grade against the resolved path: the touched set holds what
            # execute() actually wrote, and the model may have used a relative one.
            probe = {**args, "path": tools.resolve_path(args.get("path") or "")}
        return oversight.wants_review(
            name,
            probe,
            self._oversight.get("intensity", "medium"),
            touched=self._touched,
        )

    def _held(self, headline, body):
        """A tool result that says an action was reviewed and NOT run."""
        return (
            "<system-reminder>\n"
            f"{headline}\n"
            f"{body}\n"
            "This action was reviewed by the decision agent and NOT executed. Do "
            "not call it again unchanged. Either act on the guidance above, or - if "
            "you believe it is wrong - say why, with evidence, before proposing it "
            "again.\n"
            "</system-reminder>"
        )

    def _review_gate(self, name, args, message):
        """Review a significant action before it runs.

        Returns None to proceed (the call still goes through the normal
        permission layer), or (held_feedback, marker) to hold this call and let
        the coding agent re-decide on the reviewer's guidance. Bounded: the same
        decision gets at most max_rounds reviews before it escalates; different
        decisions are tracked separately.
        """
        if not self._needs_review(name, args) or self.cancelled():
            return None

        fp = oversight.fingerprint(name, args)
        # Two ways a judgment already happened and the reviewer need not run
        # again: a human approved this exact call through the permission
        # dialog this turn, or this file's first edit this turn was approved
        # and this is another small change to it.
        if fp in self._user_approved or (
            name in ("write_file", "str_replace")
            and tools.resolve_path(args.get("path") or "") in self._reviewed_files
            and oversight.is_small_edit(name, args)
        ):
            return None
        history = self._review_log.get(fp, [])
        try:
            max_rounds = max(1, int(self._oversight.get("max_rounds") or 1))
        except (TypeError, ValueError):
            max_rounds = 1

        if len(history) >= max_rounds:
            events.emit(
                events.DECISION,
                {
                    "status": "escalated",
                    "tool": name,
                    "round": len(history),
                    "review_id": fp,
                    "agent": "checker",
                    "subject": "build",
                    "reasoning": f"unresolved after {len(history)} review round(s)",
                },
            )
            events.emit(
                events.NOTE,
                {
                    "text": "Decision Oversight escalated: the coding agent and the "
                    "reviewer did not agree within the review limit. The action "
                    "was stopped for you to decide.",
                    "kind": "oversight_escalated",
                },
            )
            self._stop_turn = True
            return (
                self._held(
                    "DECISION ESCALATED to the user.",
                    "The action below reached the review limit without agreement. Do "
                    "NOT run it again. Tell the user what you were about to do, what "
                    "the reviewer objected to across the rounds, and what you believe "
                    "instead, so they can decide. Do not proceed on your own.",
                ),
                "escalated",
            )

        events.emit(
            events.DECISION,
            {
                "status": "reviewing",
                "tool": name,
                "round": len(history) + 1,
                "review_id": fp,
                "agent": "checker",
                "subject": "build",
            },
        )
        events.emit(
            events.STATUS, {"active": "reviewing a decision", "role": "checker"}
        )
        verdict, usage = oversight.review(
            self._checker_model_id,
            self._oversight.get("instructions", ""),
            task=self._turn_text,
            plan=self._plan_text,
            name=name,
            args=args,
            note=(message.content or ""),
            history=history,
            cancel=self.cancel_event,
        )
        events.emit(events.STATUS, {"active": None})
        events.emit(events.USAGE, dict(usage))
        history = history + [
            {"verdict": verdict.verdict, "correction": verdict.correction}
        ]
        if fp not in self._review_log and len(self._review_log) >= 64:
            self._review_log.pop(next(iter(self._review_log)))
        self._review_log[fp] = history

        payload = {
            "tool": name,
            "verdict": verdict.verdict,
            "round": len(history),
            "reasoning": verdict.reasoning,
            "risks": verdict.risks,
            "review_id": fp,
            "agent": "checker",
            "subject": "build",
        }

        if verdict.verdict == "error":
            # The reviewer failed. A low/medium action proceeds under the normal
            # permission layer (still fully in force); a destructive/config shell
            # action is held rather than run unseen.
            if oversight.block_on_error(name, args):
                events.emit(events.DECISION, {**payload, "status": "error"})
                events.emit(
                    events.NOTE,
                    {
                        "text": "Decision Oversight failed on a "
                        f"high-risk action ({verdict.error}); it was held, not run.",
                        "kind": "oversight_failed",
                    },
                )
                return (
                    self._held(
                        "DECISION REVIEW COULD NOT RUN.",
                        f"The reviewer failed ({verdict.error}). This is a "
                        "high-risk action, so it was NOT executed. Retry once "
                        "the reviewer is reachable, or ask the user before "
                        "running it.",
                    ),
                    "error",
                )
            events.emit(events.DECISION, {**payload, "status": "skipped"})
            events.emit(
                events.NOTE,
                {
                    "text": "Decision Oversight review failed and was "
                    f"skipped for a lower-risk action ({verdict.error}); "
                    "the tool permission layer still applies.",
                    "kind": "oversight_failed",
                },
            )
            return None

        if verdict.verdict == "approve":
            events.emit(events.DECISION, {**payload, "status": "approved"})
            if name in ("write_file", "str_replace"):
                self._reviewed_files.add(tools.resolve_path(args.get("path") or ""))
            return None

        # revise / clarify / escalate -> hold and hand back the reviewer's guidance
        risks = ", ".join(verdict.risks) or "none stated"
        if verdict.verdict == "revise":
            body = (
                f"The reviewer says: {verdict.reasoning or '(no reason given)'}\n"
                f"Risks: {risks}\n"
                f"Required correction: {verdict.correction or '(none given)'}"
            )
            events.emit(
                events.DECISION,
                {**payload, "status": "revised", "correction": verdict.correction},
            )
            return self._held(
                "DECISION REVIEW - revise before proceeding.", body
            ), "revised"
        if verdict.verdict == "clarify":
            body = (
                f"The reviewer needs more evidence: {verdict.question or verdict.reasoning}\n"
                f"Risks: {risks}"
            )
            events.emit(
                events.DECISION,
                {**payload, "status": "clarify", "question": verdict.question},
            )
            return self._held(
                "DECISION REVIEW - clarify before proceeding.", body
            ), "clarify"
        body = f"The reviewer escalated: {verdict.reasoning}\nRisks: {risks}"
        events.emit(events.DECISION, {**payload, "status": "escalated"})
        self._stop_turn = True
        return self._held("DECISION REVIEW - escalate to the user.", body), "escalated"

    def _announce_mode(self):
        """Put mode switches in the transcript, where the model will see them.

        A fresh build session is the status quo ante, so it stays silent.
        Anything else - starting in plan or ask, or switching either way - gets
        an explicit notice appended silently (no renderer event, never rendered).
        """
        if self.mode == self._announced_mode:
            return
        previous, self._announced_mode = self._announced_mode, self.mode
        if self.mode in ("plan", "ask") or previous is not None:
            notice = (
                PLAN_NOTICE
                if self.mode == "plan"
                else ASK_NOTICE
                if self.mode == "ask"
                else BUILD_NOTICE
            )
            self.messages.append({"role": "user", "content": notice})

    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _answer_pending_calls(self, message, start_index, note):
        """Give every unanswered tool_call a tool result.

        The assistant message is saved with ALL its tool_calls before any of
        them run. Chat-completions requires one tool message per call id: a
        call abandoned by a stop/budget exit would 400 every later request,
        and Retry cannot heal it. Stub results keep the transcript valid.
        """
        for call in list(message.tool_calls)[start_index:]:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(call, "id", None),
                    "content": note,
                }
            )
        session.save(self.messages)

    def _model_label(self):
        caps = providers.active_model_capabilities()
        return caps.get("name") or caps.get("model")

    def _retry_info(self):
        """Where to resume from if the user retries this request later."""
        return {
            "count": self._turn_index,
            "text": self._turn_text,
            "model": self._model_label(),
        }

    def _fail(self, failure):
        """Report a failed request without losing the message that caused it."""
        error = errors_mod.classify(failure, model=self._model_label())
        if error.kind == "cancelled":
            events.emit(events.NOTE, {"text": "stopped"})
            return
        self.turn_failed = True
        events.emit(events.STATUS, {"active": None})
        payload = error.payload()
        payload["text"] = error.text()
        if error.kind != "stream_interrupted":
            payload["retry"] = self._retry_info()
        if self.mailbox is not None:
            # A dead conversation cannot run what was queued for after it:
            # hand the texts back so the UI can name them and restore them.
            discarded = self.mailbox.discard_queues()
            if discarded:
                payload["discarded_queue"] = [item["text"] for item in discarded]
        events.emit(events.ERROR, payload)

    def _text_call(self, message):
        """A model that cannot do native tool calls may write one out in text.

        Only ever consulted when the message carries call markup. Returns
        (call, narration):

        * a usable call and its narration, when this model is allowed the
          text-call format;
        * a refused call explaining why, when the format is not enabled for
          this model or the markup is unusable;
        * (None, None) when there is no call markup at all.
        """
        content = message.content or ""
        if not texttools.looks_like_call(content):
            return None, None
        caps = providers.active_model_capabilities()

        if not caps.get("supports_text_tools"):
            model = caps.get("name") or "this model"
            return (
                texttools.TextCall(
                    error=(
                        f"{model} does not use native tool calls and the text-call format "
                        "is not enabled for it. Nothing was executed - enable "
                        "'text tool calls' for this model in Settings, or ask it in plain "
                        "words."
                    )
                ),
                None,
            )
        available = tools.available_tool_names()
        schemas = tools.tool_schemas()
        return texttools.parse_with_narration(
            content, schemas=schemas, available=available
        )

    def _unrecognised_call(self, text):
        """Tell the user, plainly, that a call was not run - and why."""
        events.emit(
            events.NOTE,
            {
                "text": "The response looked like a tool call but was not one we could run, "
                "so nothing was executed.",
                "detail": text,
                "kind": "unrecognised_tool_call",
            },
        )

    def inject_tool_result(self, name: str, content: str) -> str:
        """Append a synthetic tool call + result pair to the transcript.

        For content that must reach the model through the context budget and
        history compaction exactly like real tool output, without a tool
        actually running. Returns the synthetic call id.
        """
        call_id = f"{name}_{len(self.messages)}"
        # The same shape the engine stores for a real assistant tool call, so
        # the provider sees a valid transcript.
        self.messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            }
        )
        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )
        return call_id

    def _corrective_note(self, call):
        """What the model is told when its text call could not be used."""
        available = ", ".join(
            available_tool_names()
        )
        return (
            "<system-reminder>\n"
            f"Your last message contained a tool call that could not be run: {call.error}\n"
            "Send the call as a real tool call (the tools list), not as text, or answer "
            f"in plain language. Tools available right now: {available}.\n"
            "</system-reminder>"
        )

    def _tools_honesty(self, injection):
        """When this session can run no tools at all, say so - both ways.

        The model must not be told tools exist when they do not, and the user
        must hear why. Only fires when the list is genuinely empty, so a
        single missing dependency never produces this note.
        """

        if not providers.active_model_capabilities().get("supports_tools", True):
            return
        if tools.available_tool_names():
            return
        why = tools.unavailable_summary() or "none of the tools could start"
        injection["content"] += (
            "\n<system-reminder>\n"
            "No tools are available in this session. Do not attempt tool calls; "
            f"answer in plain text. Reasons: {why}\n"
            "</system-reminder>"
        )
        events.emit(
            events.NOTE,
            {
                "text": f"no tools are available in this session: {why}",
                "kind": "tools_unavailable",
            },
        )

    def _build_injection(self):
        """The per-turn reminder: mode rules, role instructions, honesty."""
        injection = reminder(baseline=self._file_state)
        if self.mode == "plan":
            injection["content"] += (
                "\n<mode>\nPlan mode: you are read-only. Explore, explain, and "
                "propose concrete changes, but do NOT call write_file or "
                "str_replace - they are blocked and this turn will be wasted.\n</mode>"
            )
        elif self.mode == "ask":
            injection["content"] += (
                "\n<mode>\nAsk mode: you are read-only. Answer questions and "
                "explain the codebase, but do NOT call write_file or str_replace, "
                "and do NOT run commands that modify anything - both are blocked "
                "and this turn will be wasted.\n</mode>"
            )
        if self._role_instructions:
            injection["content"] += (
                "\n<task-instructions>\n"
                "The user configured these instructions for the build phase. "
                "Follow them; where they conflict with the project rules above, "
                "ask the user rather than choosing yourself.\n"
                f"{self._role_instructions}\n"
                "</task-instructions>"
            )
        self._tools_honesty(injection)
        # The manifest is repeated here every turn, so a transcript that
        # was trimmed or compacted still says which files exist and where
        # they are. The agent never loses track of an attachment.
        note = attachments_mod.session_note(self._session_attachments())
        if note:
            injection["content"] += f"\n{note}"
        # The subagent the agent spawns reads the same list, so it can open
        # the files the user attached without being told about them twice.
        attachments_mod.bind(self._session_attachments())
        events.emit(events.INJECTION, {"content": injection["content"]})
        return injection

    def _turn(self):
        self._announce_mode()
        # The role this turn reports on its events. The mode is locked for the
        # whole turn, so one label at the top is the truth for all of them -
        # the UI shows it verbatim on tool entries and the activity line.
        role = (
            "plan" if self.mode == "plan" else "ask" if self.mode == "ask" else "build"
        )
        # The user message that opened this turn, so a retry resumes from it
        # instead of appending a second copy.
        self._turn_index = len(self.messages) - 1
        self._text_retries = 0
        self._text_calls = set()
        self._review_log = {}
        self._reviewed_files = set()
        self._user_approved = set()
        self._stop_turn = False
        self.turn_failed = False
        # What THIS turn changed. A run that changed nothing must not
        # trigger a review that has nothing to look at.
        self._turn_touched = set()
        # Build's own budget, independent of the plan and review phases.
        self._build_budget = budget.Budget(tool_calls=BUILD_MAX_TOOLS)
        self._build_progress = budget.Progress(limit=4)
        for message in reversed(self.messages[: self._turn_index + 1]):
            if message.get("role") == "user" and not str(
                message.get("content") or ""
            ).startswith("<"):
                self._turn_text = str(message.get("content") or "")
                break
        usage = None  # bound by every call_llm that returns; None on early stop
        history.begin_turn()  # this turn owns its spill files and no others
        # Baseline for "changed since your last turn": the state at turn start.
        # Fixed for the whole turn, so the note is stable across requests and
        # edits the agent makes this turn surface on the next one.
        self._file_state = context_mod.git_state()
        while True:
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                break
            if self._stop_turn:
                # Oversight escalated and already told the coding agent to hand
                # the decision to the user. Stop the turn safely rather than
                # letting it grind through the same disagreement.
                events.emit(
                    events.NOTE,
                    {
                        "text": "the turn was stopped by Decision Oversight; the "
                        "disagreement is above for you to resolve",
                        "kind": "oversight_stopped",
                    },
                )
                break
            if self.mailbox is not None:
                # Steers wait for the current step to finish, then join the
                # conversation here, before the next request is assembled.
                for steer in self.mailbox.take_steers():
                    self._receive_user_text(
                        steer["text"],
                        via="steer",
                        attachments=steer.get("attachments"),
                    )
            injection = self._build_injection()

            # Assemble the request separately from the stored history, so a
            # model with a smaller context window gets a trimmed copy and the
            # transcript on disk stays exactly as it was.
            request, trimmed = history.prepare(
                self._with_images(self._with_manifests(self.messages + [injection])),
                providers.active_context_window(),
            )
            if trimmed:
                events.emit(events.NOTE, {"text": trimmed})

            events.emit(events.STATUS, {"active": active_form(), "role": role})
            self._step += 1
            request_id = f"{self._request_prefix}:{self._step}"
            # Plan and Ask get the same chokepoint the planner has: the withheld
            # tools are not offered at all, so read-only does not depend on the
            # model's cooperation. The post-hoc intercept below stays as the
            # second fence for a model that emits one anyway.
            turn_tools = None
            if self.mode in ("plan", "ask"):
                turn_tools = [
                    s
                    for s in tools.TOOL_SCHEMAS
                    if s["function"]["name"] not in READONLY_WITHHELD
                ]
            try:
                on_delta = None
                if self.stream:

                    def on_delta(text):
                        if self.cancelled():
                            return
                        events.emit(events.MESSAGE_DELTA, {"text": text})

                    def on_reasoning(text):
                        if self.cancelled():
                            return
                        events.emit(events.THINKING, {"text": text})

                try:
                    message, usage = call_llm(
                        request,
                        tools=turn_tools,
                        on_delta=on_delta,
                        on_reasoning=on_reasoning if self.stream else None,
                        cancel=self.cancel_event,
                    )
                except Exception as failure:  # noqa: BLE001 - one image, not the turn
                    # A provider that will not take an image part must not cost
                    # the user their message: the file stays attached, the
                    # request goes again without the picture, and the agent is
                    # told why it cannot see it.
                    fallback = self._without_images(failure, request, injection)
                    if fallback is None:
                        raise
                    request = fallback
                    message, usage = call_llm(
                        request,
                        tools=turn_tools,
                        on_delta=on_delta,
                        on_reasoning=on_reasoning if self.stream else None,
                        cancel=self.cancel_event,
                    )
            except Exception as failure:  # noqa: BLE001 - report, don't kill the session
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    break
                self._fail(failure)
                break
            events.emit(events.STATUS, {"active": None})
            # A generation the user has moved on from (stop, model switch) is
            # abandoned here: its answer is not appended and its turn is over.
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                break

            text_call, narration = (None, None)
            if not message.tool_calls:
                text_call, narration = self._text_call(message)
                if text_call is not None and text_call.ok:
                    # The same call text again means the model is looping, not
                    # working: it already got this exact result. Tell it so and
                    # stop - never execute the same text call twice.
                    fingerprint = f"{text_call.name}:{text_call.arguments}"
                    if fingerprint in self._text_calls:
                        self.messages.append(message.model_dump(exclude_none=True))
                        session.save(self.messages)
                        events.emit(
                            events.NOTE,
                            {
                                "text": "The model sent the same text tool call again "
                                "after receiving its result, so the turn was "
                                "stopped instead of running it twice.",
                                "kind": "repeated_text_call",
                            },
                        )
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "<system-reminder>\n"
                                    "You already sent this exact tool call and its result is "
                                    "in the conversation. Do not send it again; use the result "
                                    "you have and answer the user.\n"
                                    "</system-reminder>"
                                ),
                            }
                        )
                        session.save(self.messages)
                        break
                    self._text_calls.add(fingerprint)
                    # The narration is real output: show it, keep it in the
                    # transcript, then run the call exactly like a native one.
                    if narration:
                        events.emit(
                            events.MESSAGE, {"role": "assistant", "content": narration}
                        )
                elif text_call is not None:
                    # Unusable markup. Say so plainly, and give the model one
                    # bounded chance to redo it as a real call. Never silently.
                    if narration:
                        events.emit(
                            events.MESSAGE, {"role": "assistant", "content": narration}
                        )
                    self.messages.append(message.model_dump(exclude_none=True))
                    session.save(self.messages)
                    events.emit(events.USAGE, dict(usage))
                    self._unrecognised_call(text_call.reason())
                    if self._text_retries < 1:
                        self._text_retries += 1
                        self.messages.append(
                            {
                                "role": "user",
                                "content": self._corrective_note(text_call),
                            }
                        )
                        session.save(self.messages)
                        continue
                    break

            if text_call is not None and text_call.ok:
                converted = SimpleNamespace(
                    content=narration or None,
                    tool_calls=[
                        text_call.as_tool_call(f"text-{self._turn_index}-{self._step}")
                    ],
                )
                converted.model_dump = lambda exclude_none=True: {
                    "role": "assistant",
                    **({"content": narration} if narration else {}),
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in converted.tool_calls
                    ],
                }
                message = converted

            self.messages.append(message.model_dump(exclude_none=True))
            session.save(self.messages)
            events.emit(events.USAGE, dict(usage))

            if self.debug:
                events.emit(events.DEBUG, message.model_dump(exclude_none=True))

            if message.content:
                events.emit(
                    events.MESSAGE,
                    {"role": "assistant", "content": message.content, "agent": role},
                )

            if not message.tool_calls:
                break

            for index, tool_call in enumerate(message.tool_calls):
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    self._answer_pending_calls(
                        message, index, "Stopped by the user before this call ran."
                    )
                    break
                if not self._run_tool_call(message, tool_call, index, request_id, role):
                    break

                if self._stop_turn:
                    # A escalation in this message stops the rest of the batch and
                    # the turn; the remaining calls were never run.
                    self._answer_pending_calls(
                        message, index + 1, "The turn was stopped before this call ran."
                    )
                    break

        history.sweep()  # the turn is over: bin its temp files
        history.strip(self.messages)  # ...and shrink the tool output it produced

        if usage and compact.needed(usage, providers.active_context_window()):
            self.messages = commands.compact(
                self.messages, providers.active_context_window()
            )

        # The agent's own message is already out, so a review started here
        # cannot delay it. The review runs in the background and reports
        # itself through the card.
        self._maybe_auto_review()

    def _run_tool_call(self, message, tool_call, index, request_id, role):
        """Budget, review gate, execution and transcript append for one call.

        False means the turn stops here: the build budget or the
        no-progress guard fired, and the calls after this one were
        already answered.
        """
        name = tool_call.function.name
        call_id = getattr(tool_call, "id", None)
        try:
            args = json.loads(tool_call.function.arguments)
        except (ValueError, TypeError):
            args = {}
        # Build's independent budget and no-progress guard: a runaway or
        # stuck build stops with a clear status instead of grinding.
        over_budget = self._build_budget.tool()
        looping = self._build_progress.repeat(
            f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        )
        if over_budget or looping:
            events.emit(
                events.NOTE,
                {
                    "text": (
                        "Build stopped - it "
                        + (
                            "reached its tool-call budget"
                            if over_budget
                            else "kept repeating the same step"
                        )
                        + " without finishing. Work so far is saved; refine "
                        "the request to continue."
                    ),
                    "kind": "build_budget",
                },
            )
            events.emit(events.PHASE, {"phase": "blocked"})
            self._answer_pending_calls(
                message,
                index,
                "The turn stopped before this call ran "
                "(budget or a repeated step).",
            )
            self._stop_turn = True
            return False
        review_marker = None
        # Fingerprinted before execute() resolves paths, so a re-proposed
        # identical call fingerprints the same way it did at review time.
        decision_fp = oversight.fingerprint(name, args)
        if self.mode in ("plan", "ask") and name in (
            "write_file",
            "str_replace",
        ):
            verb = (
                "Describe the change"
                if self.mode == "plan"
                else "Answer from what you can read"
            )
            result = (
                f"{self.mode.capitalize()} mode: read-only, no edits made. "
                f"{verb} instead of calling {name}."
            )
        elif (
            self.mode == "ask"
            and name == "bash"
            and permissions.decide(str(args.get("command") or "")) != "allow"
        ):
            result = (
                "Ask mode is read-only: this command can change the "
                "workspace, so it was not run. Answer from what you can "
                "read instead."
            )
        else:
            # Decision oversight runs before, never after: it can hold a
            # significant action and hand the coding agent the reviewer's
            # guidance, but it never runs the tool itself and never
            # bypasses the permission layer an approved call still passes
            # through below.
            hold = self._review_gate(name, args, message)
            if hold is not None:
                result, review_marker = hold
            else:
                # One call, one request, one execution: a replayed frame or
                # a re-entered stream replays the recorded result.
                args, result = execute(
                    tool_call,
                    note=(message.content or ""),
                    call_key=(request_id, call_id or index),
                    cancel=self.cancel_event,
                )
                if tools.user_approved(name, args):
                    self._user_approved.add(decision_fp)
                if name in ("write_file", "str_replace") and not str(
                    result
                ).startswith(
                    ("Error:", "Blocked by policy:", "The user denied")
                ):
                    # A landed edit: later small edits to this file are
                    # follow-up work on a decision already reviewed.
                    self._touched.add(str(args.get("path") or ""))
                    self._turn_touched.add(str(args.get("path") or ""))
                self._reconcile_todos(name, args, result)
        extra = {"reviewed": review_marker} if review_marker else {}
        events.emit(
            events.TOOL_CALL,
            {
                "call_id": call_id,
                "name": name,
                "args": args,
                "nested": False,
                "agent": role,
                **extra,
            },
        )
        events.emit(
            events.TOOL_RESULT,
            {
                "call_id": call_id,
                "name": name,
                "args": args,
                "result": result,
                "nested": False,
                "agent": role,
                **extra,
            },
        )
        if tool_call.function.name == "write_todos" and isinstance(
            args.get("todos"), list
        ):
            events.emit(events.TODOS, {"todos": args["todos"]})

        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
        )
        session.save(self.messages)
        return True
