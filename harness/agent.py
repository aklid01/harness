import itertools
import json
from types import SimpleNamespace

from . import commands
from . import compact
from . import budget
from . import events
from . import history
from . import providers
from . import agents
from . import oversight
from . import plan as plan_mod
from . import texttools
from . import session
from .context import reminder
from .errors import ModelError
from . import errors as errors_mod
from .llm import call_llm, system_prompt
from .todos import active_form
from .tools import execute


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

# The planner is read-only by construction: these tools are simply not offered
# to it, so it cannot change the workspace. This is stronger than the read-only
# instruction in its prompt, mirroring how subagent.py fences a guest.
PLAN_WITHHELD = {"write_file", "str_replace", "write_todos"}
PLAN_MAX_TURNS = 16  # a runaway planner is worse than a missing plan
PLAN_MAX_TOOLS = 40  # the planner inspects; it is not a search budget
BUILD_MAX_TOOLS = 200  # bound a runaway build; a healthy build stops on its own
PLAN_REPLANS = 1  # one targeted replan when planning yields nothing usable


# Every Runner gets its own request-id space, so a tool call made by one
# conversation can never collide with an identical call made by another.
TURN_KEYS = itertools.count(1)


class Runner:
    """One agent conversation. Emits events; never touches the screen."""

    def __init__(self, resume=False, debug=False, stream=False, cancel_event=None, mode="build"):
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
        self._oversight = None          # oversight config dict, or None when off
        self._checker_model_id = None
        self._plan_text = None          # plan handed to the build phase, if any
        self._build_started = False     # idempotent: build runs at most once per task
        self._review_log: dict = {}     # decision fingerprint -> prior verdicts
        self._stop_turn = False         # set when oversight escalates: stop safely
        self._request_prefix = f"r{next(TURN_KEYS)}"
        self.messages = [{"role": "system", "content": system_prompt(mode)}]
        if resume:
            saved = session.all_sessions()
            if saved:
                self.messages = session.open_session(saved[0]["id"])
                history.strip(self.messages)
                events.emit(events.SESSION, {"label": "resumed", "messages": self.messages, "id": session.current_id()})

    def handle_input(self, user_text):
        if user_text.startswith("/"):
            self.messages = commands.handle(user_text, self.messages)
            session.save(self.messages)
            return

        self.messages.append({"role": "user", "content": user_text})
        # Saved before the call, so a failed request never loses what was asked.
        session.save(self.messages)
        events.emit(events.MESSAGE, {"role": "user", "content": user_text})
        self._run_workflow(user_text)

    def _run_workflow(self, user_text):
        """Run the workflow the selected mode names, in one session/transcript.

        * build      - execute directly with the build (or default) agent.
        * plan       - read-only planning turn: propose, never implement.
        * plan-build - bounded planning, then a single automatic handoff to
                       Build (respecting any plan-approval gate). Build always
                       starts exactly once, after a usable plan exists.

        With no custom agents configured this reduces to a plain build turn,
        byte for byte as before. Mode selects the workflow; agent settings only
        choose which model each phase uses - and changing one never edits the
        other.
        """
        config = agents.get()
        default_model = providers.get_active_id()
        self._build_started = False  # one submission = one task: reset the guard
        build_cfg = config["build"]
        building = self.mode in ("build", "plan-build")
        self._role_instructions = (build_cfg.get("instructions") or "") \
            if (build_cfg.get("enabled") and building) else ""

        # Decision oversight: live only when enabled AND its reviewer model still
        # resolves. An enabled role whose model was deleted is inert rather than
        # silently reviewed by some other model.
        ov = config["oversight"]
        checker_id = ov.get("model_id") if ov.get("enabled") else None
        self._checker_model_id = checker_id or None
        self._oversight = ov if (checker_id and providers.get_model(checker_id)) else None
        self._plan_text = None

        try:
            if self.mode == "plan":
                self._phase("planning")
                self._turn()
                return
            if self.mode == "plan-build":
                self._run_plan_build(user_text, config, default_model, build_cfg)
                return
            self._run_build_phase(user_text, build_cfg, default_model)
        finally:
            self._phase(None)

    def _phase(self, status):
        """Emit the current workflow phase for the UI indicator. None clears."""
        events.emit(events.PHASE, {"phase": status})

    def _persist_plan(self, task, plan_text, completion, phase, **extra):
        fields = {"task": task, "plan": plan_text or "", "completion": completion,
                  "phase": phase}
        fields.update(extra)
        try:
            plan_mod.current_save(**fields)
        except Exception:  # noqa: BLE001 - persistence is best-effort, never fatal
            pass

    def _run_build_phase(self, user_text, build_cfg, default_model):
        """Run the Build phase exactly once - idempotent across duplicates."""
        if self._build_started:
            return
        self._build_started = True
        if self.cancelled():
            events.emit(events.NOTE, {"text": "stopped"})
            return
        self._phase("building")
        build_id = agents.resolve_model(build_cfg, default_model)
        if build_id:
            with providers.use(build_id):
                self._turn()
        else:
            self._turn()

    def _run_plan_build(self, user_text, config, default_model, build_cfg):
        plan_cfg = config["plan"]
        plan_id = agents.resolve_model(plan_cfg, default_model)
        self._phase("planning")
        plan_text, completion = self._plan_phase(
            user_text, plan_id, plan_cfg.get("instructions") or "")
        self._plan_text = plan_text
        self._persist_plan(user_text, plan_text, completion, "planned")

        # Bounded recovery: one targeted replan when the first pass yields
        # nothing usable. Never hand an unusable plan to Build, never restart
        # the whole task, never switch workflows on the user's behalf.
        replans = 0
        while not plan_text and completion == plan_mod.UNUSABLE \
                and replans < PLAN_REPLANS and not self.cancelled():
            replans += 1
            self._phase("replanning")
            events.emit(events.NOTE, {
                "text": "planning produced no usable plan - trying once more, "
                        "narrowly", "kind": "plan_replan", "role": "plan"})
            plan_text, completion = self._plan_phase(
                user_text + "\n\nThe previous planning attempt did not yield a "
                "usable plan. Produce a concrete, ordered plan now, and do not "
                "spend turns exploring beyond what that plan needs.",
                plan_id, plan_cfg.get("instructions") or "")
            self._plan_text = plan_text
            self._persist_plan(user_text, plan_text, completion, "planned",
                               attempts=replans + 1)

        if self.cancelled():
            return
        if not plan_text:
            # Unusable, after bounded recovery. Surface and stop; a recoverable
            # error lets the user retry, refine, or fall back to Build/Direct.
            self._phase("failed")
            plan_mod.current_save(phase="failed")
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
        self._run_build_phase(user_text, build_cfg, default_model)

    def _handoff_to_build(self, plan_text):
        """Pass the authoritative plan to Build, exactly once.

        The plan becomes the build agent's structured context. Guarding on the
        message already being last makes a replayed or duplicated completion
        event a no-op, so Build is never launched twice for one task.
        """
        if self.cancelled():
            return
        handoff = {"role": "user", "content": PLAN_HANDOFF.format(plan=plan_text)}
        if self.messages and self.messages[-1] == handoff:
            return
        self.messages.append(handoff)
        session.save(self.messages)

    def _plan_phase(self, request, plan_model_id, plan_instructions):
        """A bounded, read-only planning pass on its own model.

        This is the subagent pattern (see subagent.py): a throwaway message list
        that holds read-only tools, runs the normal call/execute loop, and hands
        back only its final write-up. Nothing here is written to the session
        transcript beyond the single handoff message the caller appends, so the
        exploration cost never lands in the persisted history.
        """
        from . import tools as tools_module

        messages = [
            {"role": "system", "content": system_prompt("plan", plan_instructions)},
            {"role": "user", "content": request},
        ]
        toolset = [s for s in tools_module.TOOL_SCHEMAS
                   if s["function"]["name"] not in PLAN_WITHHELD]
        # The planner's own budget - separate from Build's and Oversight's.
        budget_obj = budget.Budget(turns=PLAN_MAX_TURNS, tool_calls=PLAN_MAX_TOOLS)
        progress = budget.Progress(limit=2)
        final = None  # only a clean answer (a turn with no tool calls) is a plan
        completion = plan_mod.UNUSABLE
        stopped = None  # why the phase stopped without finalising
        label = plan_model_id
        events.emit(events.PLAN, {"status": "generating", "request": request[:200],
                                  "model": None, "role": "plan"})
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
                    message, usage = call_llm(prepared, tools=toolset, cancel=self.cancel_event)
                except Exception as failure:  # noqa: BLE001 - report, don't fake a plan
                    events.emit(events.STATUS, {"active": None})
                    events.emit(events.PLAN, {"status": "failed",
                                              "reason": type(failure).__name__, "model": label,
                                              "role": "plan"})
                    events.emit(events.NOTE, {
                        "text": f"the planning pass failed ({type(failure).__name__}); "
                                "nothing was built. Press retry, or revise the request.",
                        "kind": "plan_failed", "role": "plan"})
                    return None, plan_mod.UNUSABLE
                events.emit(events.STATUS, {"active": None})
                events.emit(events.USAGE, dict(usage))
                messages.append(message.model_dump(exclude_none=True))
                # A turn that keeps making tool calls is still exploring; only a
                # turn that stops calling is a finished plan. Its earlier
                # narration is never mistaken for the plan.
                if not message.tool_calls:
                    final = message.content or ""
                    completion = plan_mod.COMPLETE if final.strip() else plan_mod.UNUSABLE
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
                    if name in PLAN_WITHHELD:
                        result = f"Planning is read-only; {name} is not available here."
                    else:
                        args, result = execute(tool_call, note=(message.content or ""))
                    events.emit(events.TOOL_CALL, {"call_id": call_id, "name": name,
                                                   "args": args, "nested": True, "role": "plan"})
                    events.emit(events.TOOL_RESULT, {"call_id": call_id, "name": name,
                                                     "args": args, "result": result, "nested": True,
                                                     "role": "plan"})
                    messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": result})
                    fingerprint = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                    if progress.repeat(fingerprint):
                        stopped = "the planner kept repeating the same step"
                        stalled = True
                        break
                if stalled:
                    break

        if self.cancelled():
            return None, plan_mod.CANCELLED
        if completion == plan_mod.COMPLETE:
            events.emit(events.PLAN, {"status": "ready", "plan": final, "model": label, "role": "plan"})
            return final, plan_mod.COMPLETE
        reason = stopped or ("the planner returned an empty plan" if final is not None
                              else "the planner ended without a finished plan")
        events.emit(events.PLAN, {"status": "failed", "reason": reason,
                                  "model": label, "role": "plan"})
        events.emit(events.NOTE, {
            "text": f"planning did not produce a usable plan ({reason}); nothing was built. "
                    "Try retrying, or refine the request.",
            "kind": "plan_failed", "role": "plan"})
        return None, plan_mod.UNUSABLE

    def _approve_plan(self, plan_text, config):
        """Pause so the user can review the plan before it is built.

        Returns True to build. Auto-builds (True) when the review toggle is off,
        when no frontend can answer, or on cancel - a plan review is a courtesy,
        never a way to wedge the turn. Blocks on the same interaction the tool
        approval path uses, so an unanswered prompt is bounded by that timeout.
        """
        from . import tools as tools_module

        if not agents.plan_wants_approval(config) or self.cancelled():
            return True
        if not tools_module.approval_channel_ready():
            return True  # nobody to ask; keep building rather than deadlock
        preview = plan_text if len(plan_text) <= 3200 else plan_text[:3200] + "\n\n… (full plan shown above)"
        try:
            approved = events.interact(events.APPROVAL, {
                "tool": "plan", "reason": "Build this plan?",
                "why": preview, "risk": "low", "risk_reason": "Plan approval",
                "command": "", "target": "",
            })
        except Exception:  # noqa: BLE001 - never wedge the turn on the prompt
            return True
        if not approved:
            events.emit(events.NOTE, {
                "text": "You chose not to build. The plan is above; nothing was changed. "
                        "Send a follow-up to refine it, or resubmit to plan again.",
                "kind": "plan_skipped"})
        return bool(approved)

    # --- decision oversight -------------------------------------------------

    def _needs_review(self, name, args):
        if not self._oversight or self.mode == "plan":
            return False
        return oversight.wants_review(name, args, self._oversight.get("intensity", "medium"))

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
        history = self._review_log.get(fp, [])
        try:
            max_rounds = max(1, int(self._oversight.get("max_rounds") or 1))
        except (TypeError, ValueError):
            max_rounds = 1

        if len(history) >= max_rounds:
            events.emit(events.DECISION, {"status": "escalated", "tool": name, "round": len(history),
                                          "review_id": fp, "agent": "checker", "subject": "build",
                                          "reasoning": f"unresolved after {len(history)} review round(s)"})
            events.emit(events.NOTE, {
                "text": "Decision Oversight escalated: the coding agent and the "
                        "reviewer did not agree within the review limit. The action "
                        "was stopped for you to decide.",
                "kind": "oversight_escalated"})
            self._stop_turn = True
            return (
                self._held(
                    "DECISION ESCALATED to the user.",
                    "The action below reached the review limit without agreement. Do "
                    "NOT run it again. Tell the user what you were about to do, what "
                    "the reviewer objected to across the rounds, and what you believe "
                    "instead, so they can decide. Do not proceed on your own."),
                "escalated",
            )

        events.emit(events.DECISION, {"status": "reviewing", "tool": name, "round": len(history) + 1,
                                      "review_id": fp, "agent": "checker", "subject": "build"})
        events.emit(events.STATUS, {"active": "reviewing a decision", "role": "checker"})
        verdict, usage = oversight.review(
            self._checker_model_id, self._oversight.get("instructions", ""),
            task=self._turn_text, plan=self._plan_text, name=name, args=args,
            note=(message.content or ""), history=history, cancel=self.cancel_event)
        events.emit(events.STATUS, {"active": None})
        events.emit(events.USAGE, dict(usage))
        history = history + [{"verdict": verdict.verdict, "correction": verdict.correction}]
        if fp not in self._review_log and len(self._review_log) >= 64:
            self._review_log.pop(next(iter(self._review_log)))
        self._review_log[fp] = history

        payload = {"tool": name, "verdict": verdict.verdict, "round": len(history),
                   "reasoning": verdict.reasoning, "risks": verdict.risks,
                   "review_id": fp, "agent": "checker", "subject": "build"}

        if verdict.verdict == "error":
            # The reviewer failed. A low/medium action proceeds under the normal
            # permission layer (still fully in force); a destructive/config shell
            # action is held rather than run unseen.
            if oversight.block_on_error(name, args):
                events.emit(events.DECISION, {**payload, "status": "error"})
                events.emit(events.NOTE, {"text": "Decision Oversight failed on a "
                                          f"high-risk action ({verdict.error}); it was held, not run.",
                                          "kind": "oversight_failed"})
                return (
                    self._held("DECISION REVIEW COULD NOT RUN.",
                               f"The reviewer failed ({verdict.error}). This is a "
                               "high-risk action, so it was NOT executed. Retry once "
                               "the reviewer is reachable, or ask the user before "
                               "running it."),
                    "error",
                )
            events.emit(events.DECISION, {**payload, "status": "skipped"})
            events.emit(events.NOTE, {"text": "Decision Oversight review failed and was "
                                       f"skipped for a lower-risk action ({verdict.error}); "
                                       "the tool permission layer still applies.",
                                       "kind": "oversight_failed"})
            return None

        if verdict.verdict == "approve":
            events.emit(events.DECISION, {**payload, "status": "approved"})
            return None

        # revise / clarify / escalate -> hold and hand back the reviewer's guidance
        risks = ", ".join(verdict.risks) or "none stated"
        if verdict.verdict == "revise":
            body = (f"The reviewer says: {verdict.reasoning or '(no reason given)'}\n"
                    f"Risks: {risks}\n"
                    f"Required correction: {verdict.correction or '(none given)'}")
            events.emit(events.DECISION, {**payload, "status": "revised", "correction": verdict.correction})
            return self._held("DECISION REVIEW - revise before proceeding.", body), "revised"
        if verdict.verdict == "clarify":
            body = (f"The reviewer needs more evidence: {verdict.question or verdict.reasoning}\n"
                    f"Risks: {risks}")
            events.emit(events.DECISION, {**payload, "status": "clarify", "question": verdict.question})
            return self._held("DECISION REVIEW - clarify before proceeding.", body), "clarify"
        body = (f"The reviewer escalated: {verdict.reasoning}\nRisks: {risks}")
        events.emit(events.DECISION, {**payload, "status": "escalated"})
        self._stop_turn = True
        return self._held("DECISION REVIEW - escalate to the user.", body), "escalated"

    def _announce_mode(self):
        """Put mode switches in the transcript, where the model will see them.

        A fresh build session is the status quo ante, so it stays silent.
        Anything else - starting in plan, or switching either way - gets an
        explicit notice appended silently (no GUI event, never rendered).
        """
        if self.mode == self._announced_mode:
            return
        previous, self._announced_mode = self._announced_mode, self.mode
        if self.mode == "plan" or previous is not None:
            notice = PLAN_NOTICE if self.mode == "plan" else BUILD_NOTICE
            self.messages.append({"role": "user", "content": notice})

    def cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _model_label(self):
        caps = providers.active_model_capabilities()
        return caps.get("name") or caps.get("model")

    def _retry_info(self):
        """Where to resume from if the user retries this request later."""
        return {"count": self._turn_index, "text": self._turn_text,
                "model": self._model_label()}

    def _fail(self, failure):
        """Report a failed request without losing the message that caused it."""
        api_key = providers.active_config()[1]
        error = errors_mod.classify(failure, api_key=api_key, model=self._model_label())
        if error.kind == "cancelled":
            events.emit(events.NOTE, {"text": "stopped"})
            return
        events.emit(events.STATUS, {"active": None})
        payload = error.payload()
        payload["text"] = error.text()
        if error.kind != "stream_interrupted":
            payload["retry"] = self._retry_info()
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
        from . import tools as tools_module

        if not caps.get("supports_text_tools"):
            model = caps.get("name") or "this model"
            return (texttools.TextCall(error=(
                f"{model} does not use native tool calls and the text-call format "
                "is not enabled for it. Nothing was executed - enable "
                "'text tool calls' for this model in Settings, or ask it in plain "
                "words.")), None)
        available = tools_module.available_tool_names()
        schemas = tools_module.tool_schemas()
        return texttools.parse_with_narration(content, schemas=schemas, available=available)

    def _unrecognised_call(self, text):
        """Tell the user, plainly, that a call was not run - and why."""
        events.emit(events.NOTE, {
            "text": "The response looked like a tool call but was not one we could run, "
                    "so nothing was executed.",
            "detail": text,
            "kind": "unrecognised_tool_call",
        })

    def _corrective_note(self, call):
        """What the model is told when its text call could not be used."""
        available = ", ".join(__import__("harness.tools", fromlist=["x"]).available_tool_names())
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
        from . import tools as tools_module

        if not providers.active_model_capabilities().get("supports_tools", True):
            return
        if tools_module.available_tool_names():
            return
        why = tools_module.unavailable_summary() or "none of the tools could start"
        injection["content"] += (
            "\n<system-reminder>\n"
            "No tools are available in this session. Do not attempt tool calls; "
            f"answer in plain text. Reasons: {why}\n"
            "</system-reminder>"
        )
        events.emit(events.NOTE, {
            "text": f"no tools are available in this session: {why}",
            "kind": "tools_unavailable",
        })

    def _turn(self):
        self._announce_mode()
        # The user message that opened this turn, so a retry resumes from it
        # instead of appending a second copy.
        self._turn_index = len(self.messages) - 1
        self._text_retries = 0
        self._text_calls = set()
        self._review_log = {}
        self._stop_turn = False
        # Build's own budget, independent of the plan and review phases.
        self._build_budget = budget.Budget(tool_calls=BUILD_MAX_TOOLS)
        self._build_progress = budget.Progress(limit=4)
        for message in reversed(self.messages[:self._turn_index + 1]):
            if message.get("role") == "user" and not str(message.get("content") or "").startswith("<"):
                self._turn_text = str(message.get("content") or "")
                break
        while True:
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                return
            if self._stop_turn:
                # Oversight escalated and already told the coding agent to hand
                # the decision to the user. Stop the turn safely rather than
                # letting it grind through the same disagreement.
                events.emit(events.NOTE, {
                    "text": "the turn was stopped by Decision Oversight; the "
                            "disagreement is above for you to resolve",
                    "kind": "oversight_stopped"})
                break
            injection = reminder()
            if self.mode == "plan":
                injection["content"] += (
                    "\n<mode>\nPlan mode: you are read-only. Explore, explain, and "
                    "propose concrete changes, but do NOT call write_file or "
                    "str_replace - they are blocked and this turn will be wasted.\n</mode>"
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
            events.emit(events.INJECTION, {"content": injection["content"]})

            # Assemble the request separately from the stored history, so a
            # model with a smaller context window gets a trimmed copy and the
            # transcript on disk stays exactly as it was.
            request, trimmed = history.prepare(
                self.messages + [injection], providers.active_context_window())
            if trimmed:
                events.emit(events.NOTE, {"text": trimmed})

            events.emit(events.STATUS, {"active": active_form(), "role": "build"})
            self._step += 1
            request_id = f"{self._request_prefix}:{self._step}"
            try:
                on_delta = None
                if self.stream:
                    def on_delta(text):
                        if self.cancelled():
                            return
                        events.emit(events.MESSAGE_DELTA, {"text": text})
                message, usage = call_llm(request, on_delta=on_delta,
                                          cancel=self.cancel_event)
            except Exception as failure:  # noqa: BLE001 - report, don't kill the session
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    return
                self._fail(failure)
                return
            events.emit(events.STATUS, {"active": None})
            # A generation the user has moved on from (stop, model switch) is
            # abandoned here: its answer is not appended and its turn is over.
            if self.cancelled():
                events.emit(events.NOTE, {"text": "stopped"})
                return

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
                        events.emit(events.NOTE, {
                            "text": "The model sent the same text tool call again "
                                    "after receiving its result, so the turn was "
                                    "stopped instead of running it twice.",
                            "kind": "repeated_text_call",
                        })
                        self.messages.append({"role": "user", "content": (
                            "<system-reminder>\n"
                            "You already sent this exact tool call and its result is "
                            "in the conversation. Do not send it again; use the result "
                            "you have and answer the user.\n"
                            "</system-reminder>"
                        )})
                        session.save(self.messages)
                        break
                    self._text_calls.add(fingerprint)
                    # The narration is real output: show it, keep it in the
                    # transcript, then run the call exactly like a native one.
                    if narration:
                        events.emit(events.MESSAGE, {"role": "assistant", "content": narration})
                elif text_call is not None:
                    # Unusable markup. Say so plainly, and give the model one
                    # bounded chance to redo it as a real call. Never silently.
                    if narration:
                        events.emit(events.MESSAGE, {"role": "assistant", "content": narration})
                    self.messages.append(message.model_dump(exclude_none=True))
                    session.save(self.messages)
                    events.emit(events.USAGE, dict(usage))
                    self._unrecognised_call(text_call.reason())
                    if self._text_retries < 1:
                        self._text_retries += 1
                        self.messages.append({"role": "user",
                                              "content": self._corrective_note(text_call)})
                        session.save(self.messages)
                        continue
                    break

            if text_call is not None and text_call.ok:
                converted = SimpleNamespace(
                    content=narration or None,
                    tool_calls=[text_call.as_tool_call(f"text-{self._turn_index}-{self._step}")])
                converted.model_dump = lambda exclude_none=True: {
                    "role": "assistant",
                    **({"content": narration} if narration else {}),
                    "tool_calls": [
                        {"id": call.id, "type": "function",
                         "function": {"name": call.function.name,
                                      "arguments": call.function.arguments}}
                        for call in converted.tool_calls],
                }
                message = converted

            self.messages.append(message.model_dump(exclude_none=True))
            session.save(self.messages)
            events.emit(events.USAGE, dict(usage))

            if self.debug:
                events.emit(events.DEBUG, message.model_dump(exclude_none=True))

            if message.content:
                events.emit(events.MESSAGE, {"role": "assistant", "content": message.content, "agent": "build"})

            if not message.tool_calls:
                break

            for index, tool_call in enumerate(message.tool_calls):
                if self.cancelled():
                    events.emit(events.NOTE, {"text": "stopped"})
                    return
                name = tool_call.function.name
                call_id = getattr(tool_call, "id", None)
                try:
                    args = json.loads(tool_call.function.arguments)
                except ValueError:
                    args = {}
                # Build's independent budget and no-progress guard: a runaway or
                # stuck build stops with a clear status instead of grinding.
                over_budget = self._build_budget.tool()
                looping = self._build_progress.repeat(
                    f"{name}:{json.dumps(args, sort_keys=True, default=str)}")
                if over_budget or looping:
                    events.emit(events.NOTE, {
                        "text": ("Build stopped - it "
                                 + ("reached its tool-call budget" if over_budget
                                    else "kept repeating the same step")
                                 + " without finishing. Work so far is saved; refine "
                                   "the request to continue."),
                        "kind": "build_budget"})
                    events.emit(events.PHASE, {"phase": "blocked"})
                    self._stop_turn = True
                    break
                review_marker = None
                if self.mode == "plan" and name in ("write_file", "str_replace"):
                    result = f"Plan mode: read-only, no edits made. Describe the change instead of calling {name}."
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
                        args, result = execute(tool_call, note=(message.content or ""),
                                               call_key=(request_id, call_id or index))
                extra = {"reviewed": review_marker} if review_marker else {}
                events.emit(events.TOOL_CALL, {"call_id": call_id, "name": name, "args": args, "nested": False, "agent": "build", **extra})
                events.emit(events.TOOL_RESULT, {"call_id": call_id, "name": name, "args": args, "result": result, "nested": False, "agent": "build", **extra})
                if tool_call.function.name == "write_todos" and isinstance(args.get("todos"), list):
                    events.emit(events.TODOS, {"todos": args["todos"]})

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
                session.save(self.messages)
                if self._stop_turn:
                    # A escalation in this message stops the rest of the batch and
                    # the turn; the remaining calls were never run.
                    break

        history.sweep()   # the turn is over: bin its temp files
        history.strip(self.messages)  # ...and shrink the tool output it produced

        if compact.needed(usage, providers.active_context_window()):
            self.messages = commands.compact(self.messages, providers.active_context_window())
