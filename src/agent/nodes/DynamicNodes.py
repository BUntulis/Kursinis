"""Dynamic, model-routed agent — the model decides which tool to run each step.

This REPLACES the fixed Planner→Retriever→Coder→Executor→Critic pipeline. Instead of a
hard-coded edge sequence, a ``router`` LLM node decides the single most useful next action
each turn (ReAct), and dedicated tool nodes execute it:

    plan · retrieve · list · read · grep · write · edit · shell · test   (+ finish → END)

LangGraph conditional edges route ``router → <chosen tool> → router`` until the model calls
``finish`` (or the per-run step budget is exhausted). Because every tool step is its own graph
node, the dashboard streams one ``CommandLog`` row per action (live Read/Grep/Write/Test rows).

Mechanism is hybrid, like the previous tool coder:
  * native function-calling for tool-capable backends (``OllamaBackend.supports_tools``);
  * a universal text protocol (code blocks + READ/GREP/EDIT/RUN/…/DONE) for coder models
    without it — parsed by ``tools.parse_actions_ordered``.

The model self-routes freely (no scaffolding): the only non-negotiables are loop-termination
safety (the step budget and the "no actionable output ⇒ finish" fallback).
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from src.llm import LLMBackend

from ..AgentState import AgentState
from ..TelemetryAccumulator import TelemetryAccumulator
from ..tools import CANONICAL_TOOL, TEXT_PROTOCOL, TOOL_SCHEMAS, WorkspaceTools, normalize_tasks, parse_actions_ordered
from .AgentNode import AgentNode

#: Canonical tool names that map 1:1 to a graph node. ``finish`` is not a node — it routes
#: straight to END via the router's conditional edge.
TOOL_NODE_NAMES = ["ask", "tasks", "plan", "retrieve", "list", "read", "grep", "write", "edit", "shell", "test"]

#: Tools that, in interactive (chat) mode, require user approval before running.
DEFAULT_APPROVAL_TOOLS = {"shell"}


# --------------------------------------------------------------------- shared helpers
def _norm(path) -> str:
    return Path(str(path or "").strip().replace("\\", "/")).as_posix()


def _workspace(state: AgentState) -> Path:
    return Path(state.get("workspace_path") or tempfile.mkdtemp(prefix="agent_ws_"))


def _task_from_state(state: AgentState):
    from src.benchmark import BenchmarkTask

    return BenchmarkTask(
        id=state.get("task_id", "task"),
        title=state.get("task_title", ""),
        difficulty="",
        category="",
        description=state.get("task_description", ""),
        expected_files=state.get("expected_files", []),
        reference_tests=state.get("reference_tests", ""),
    )


def _append_observation(state: AgentState, observation: str, supports_tools: bool) -> list:
    """Append a tool result to the conversation in the format the backend expects.

    Native models get a ``role:"tool"`` turn (one per call); text-protocol models get the
    observation merged into a trailing ``TOOL RESULT`` user turn so consecutive results from
    one model turn stay grouped.
    """
    messages = list(state.get("messages") or [])
    text = observation if isinstance(observation, str) else str(observation)
    if supports_tools:
        messages.append({"role": "tool", "content": text})
        return messages
    block = "TOOL RESULT:\n" + text
    if messages and messages[-1].get("role") == "user" and str(messages[-1].get("content", "")).startswith("TOOL RESULT"):
        messages[-1] = {"role": "user", "content": str(messages[-1]["content"]) + "\n\n" + block}
    else:
        messages.append({"role": "user", "content": block})
    return messages


def _append_log(state: AgentState, tool: str, target: str, observation: str) -> str:
    head = (observation or "").splitlines()[0] if observation else ""
    line = f"{tool}({str(target)[:60]}) -> {head[:120]}"
    prev = (state.get("tool_log") or "").strip()
    combined = (prev + "\n" + line) if prev else line
    return "\n".join(combined.splitlines()[-40:])


def _safe_label(action: dict) -> str:
    """A safe, high-level 'what I'm doing now' label (never private reasoning)."""
    tool = action.get("tool") or ""
    args = action.get("args") or {}
    target = str(args.get("path") or args.get("pattern") or args.get("command") or args.get("query") or "")[:60]
    labels = {
        "plan": "planning",
        "retrieve": "looking up docs",
        "list": "listing files",
        "read": ("reading " + target) if target else "reading a file",
        "grep": ("searching for " + target) if target else "searching",
        "write": ("writing " + target) if target else "writing a file",
        "edit": ("editing " + target) if target else "editing a file",
        "shell": "running a command",
        "test": "running tests",
    }
    return labels.get(tool, "working")


# --------------------------------------------------------------------- router
class RouterNode(AgentNode):
    """The agent's brain: each visit decides (or dequeues) the next single action.

    When the pending-action queue is empty it calls the LLM to choose the next action(s);
    otherwise it dequeues the next one (no LLM call). It sets ``next_action`` and lets the
    conditional edge route to the matching tool node — or to END on ``finish`` / budget.
    """

    SYSTEM = (
        "You are an autonomous senior Django developer working in an ALREADY-INITIALISED Django "
        "project. You operate as a DYNAMIC agent: each turn you decide the single most useful next "
        "action, call ONE tool, observe the result, and continue until the task is complete.\n\n"
        "Tools available to you: plan, update_tasks, retrieve_docs, list_files, read_file, grep, "
        "write_file, edit_file, run_shell, run_tests, finish.\n"
        "- For multi-step work, call update_tasks to keep a short todo list: plan the steps, mark "
        "exactly one in_progress, and complete them as you go (resend the full list each time).\n"
        "- The project is already set up — do NOT scaffold, run startapp, or create directories. "
        "Write the app's source files directly.\n"
        "- File paths are SHORT relative paths like `blog/models.py` — never a sentence.\n"
        "- Import everything you use (e.g. `from django.core.exceptions import ValidationError`). "
        "Target Django 5.x.\n"
        "- Do NOT write your own test files — `run_tests` runs the benchmark's tests for you.\n"
        "- When the code is written and the tests pass, call `finish`."
    )
    USER_TEMPLATE = (
        "Task: {title}\n\n"
        "{description}\n\n"
        "{expected_section}"
        "Decide and take your first action."
    )

    TEMPERATURE = 0.2
    MAX_TOKENS = 3000
    DEFAULT_MAX_STEPS = 24

    #: Re-prompt shown to a refiner that tries to finish before asking anything — weak local models
    #: often skip the ask protocol and dump prose, so we force at least one clarifying round.
    _ASK_NUDGE = (
        "STOP — do not finish yet. You have not asked the user anything. Before writing the spec you "
        "MUST ask clarifying questions. Reply with ONE `ASK:` line (a single question) followed by "
        "2-4 `- option` lines (concrete A/B/C/D-style choices). Ask about the most important unclear "
        "decisions for this app (e.g. who manages departments/people, auth & roles, what 'remaining "
        "days' counts from). Do NOT write a summary, do NOT output DONE."
    )

    #: Re-prompt shown once a refiner has asked enough — weak models otherwise loop forever asking
    #: trivial near-duplicate questions. This forces it to stop asking and produce the spec.
    _FINISH_NUDGE = (
        "STOP asking — you have gathered enough. Do NOT ask any more questions. Now output the FULL "
        "STRUCTURED PROMPT (## Title, **Overview**, **Features**, **Data models** with fields, "
        "**Constraints**) based on the idea and the answers so far, then a final line `DONE`."
    )

    #: Re-prompt for a design-first build turn that tries to finish (or ask for design approval)
    #: BEFORE any prototype file exists — weak models otherwise mark tasks completed / declare DONE
    #: without writing a single file. Forces the actual front-end build to happen first.
    _DESIGN_NUDGE = (
        "STOP — you have NOT built the front-end prototype yet: the sandbox contains no .html file, "
        "so there is nothing to review, nothing to approve and nothing to finish. Do NOT call finish "
        "and do NOT ask for design approval. Build the prototype NOW: call write_file for "
        "`index.html` (a complete page with realistic placeholder content), then `styles.css` and "
        "`script.js`. Even if the request has no web UI (a pure API or script), still write a "
        "minimal index.html that demonstrates the result (sample output, endpoints, usage) so the "
        "user can review it. Only after those files exist may you ask the user to approve the "
        "design. A task may be marked completed ONLY when its files actually exist on disk."
    )

    #: Re-prompt for an agent that calls finish while its OWN task list still shows unfinished
    #: items — without this, weak models plan 16 tasks, do the work, never flip a single status,
    #: and the Tasks panel forever says "0/16 done" on a finished build.
    _TASKS_NUDGE = (
        "STOP — do not finish yet. Your task list still shows unfinished items. First bring it up "
        "to date: resend the FULL task list (update_tasks, or a `TASKS:` block) marking every task "
        "you actually completed as done (`- [x]`), with at most one `- [>]` if you are still "
        "working. If some tasks are genuinely NOT done, do that work now instead of finishing. "
        "Only when the list reflects reality, finish with your summary."
    )

    def __init__(self, llm: LLMBackend, *, system_prompt: str | None = None, max_steps: int | None = None,
                 interactive: bool = False, allowed_tools: set | None = None, require_ask: bool = False,
                 max_questions: int = 0, design_gate=None, finish_gate=None) -> None:
        self.llm = llm
        self.system_prompt = system_prompt or self.SYSTEM
        self.max_steps = max(1, int(max_steps or self.DEFAULT_MAX_STEPS))
        # In interactive (chat) mode the model may call ask_user; in benchmark mode that tool
        # is hidden so a model can't stall waiting for a user that isn't there.
        self.interactive = bool(interactive)
        # When set, restrict the agent to these canonical tool-node names (plus the always-allowed
        # `finish`). The refiner (prompt_writer) uses {"ask"} so it structurally CANNOT write code,
        # edit, or run shell — not offered in the schemas/protocol, and dropped if emitted anyway.
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        # Refiner gate: the model may not finish until it has asked at least one question, and may
        # not ask MORE than max_questions (0 = no cap) — after that it must produce the spec.
        self.require_ask = bool(require_ask)
        self.max_questions = int(max_questions or 0)
        # Design-first gate (build turns of a NEW project): a zero-arg callable returning True once
        # the sandbox has a front-end prototype. While it returns False the router structurally
        # blocks `finish` and design-approval asks — the agent must write the prototype first.
        self.design_gate = design_gate
        # Finish gate (chat build turns): a zero-arg callable returning a VETO MESSAGE (or "")
        # consulted when the model queues `finish` — e.g. "no page is reachable, wire urls.py
        # first". The callable itself bounds how often it vetoes, so runs always terminate.
        self.finish_gate = finish_gate

    def _allowed(self, tool: str) -> bool:
        if tool == "finish" or self.allowed_tools is None:
            return True
        return tool in self.allowed_tools

    @property
    def _schemas(self) -> list[dict]:
        out = []
        for s in TOOL_SCHEMAS:
            name = s.get("function", {}).get("name")
            if not self.interactive and name == "ask_user":
                continue
            node = CANONICAL_TOOL.get(name, name)
            if not self._allowed(node):
                continue
            out.append(s)
        return out

    @property
    def _supports(self) -> bool:
        return bool(getattr(self.llm, "supports_tools", False))

    def run(self, state: AgentState) -> dict:
        if state.get("finished"):
            return {"finished": True}

        step_count = int(state.get("step_count", 0) or 0)
        messages = list(state.get("messages") or self._init_messages(state))
        pending = list(state.get("pending_actions") or [])
        did_think = False
        out: dict = {}
        budget = {"finished": True, "did_think": did_think, "router_thought": "wrapping up",
                  "stop_reason": f"step budget ({self.max_steps}) reached"}

        if not pending:
            # Spend another LLM call only if we still have budget for at least one more action.
            if step_count >= self.max_steps:
                return {**budget, "messages": messages}
            pending, messages, think_out = self._think(state, messages)
            out.update(think_out)
            did_think = True
            if not pending:
                out.update({
                    "messages": messages, "finished": True, "did_think": True,
                    "router_thought": "wrapping up",
                    "stop_reason": "model produced no further action",
                })
                return out

        action = pending[0]
        remaining = pending[1:]

        # A queued `finish` always terminates cleanly — even if the budget is exhausted,
        # we honour the model's explicit completion signal rather than reporting a budget stop.
        if (action.get("tool") or "") == "finish":
            out.update({
                "messages": messages, "pending_actions": remaining, "finished": True,
                "did_think": did_think, "router_thought": "finishing",
                "stop_reason": "model called finish",
            })
            return out

        # Any other action must respect the step budget (drops the remaining queue once over).
        if step_count >= self.max_steps:
            out.update({**budget, "messages": messages, "did_think": did_think})
            return out

        out.update({
            "messages": messages,
            "pending_actions": remaining,
            "next_action": action,
            "did_think": did_think,
            "router_thought": _safe_label(action),
            "finished": False,
        })
        return out

    def _think(self, state: AgentState, messages: list) -> tuple[list, list, dict]:
        """One LLM decision → (pending actions, updated messages, telemetry delta).

        For the refiner (``require_ask``) this re-prompts a model that tries to finish before asking
        anything: it strips the premature finish, appends a corrective nudge, and queries again
        (bounded), so weak local models that ignore the ask protocol still produce a question. For
        every other agent the loop runs exactly once (unchanged behaviour)."""
        out: dict = {}
        acc = state
        attempts = 0
        asked = int(state.get("ask_count", 0) or 0)
        at_cap = bool(self.max_questions) and asked >= self.max_questions
        while True:
            resp = self.llm.chat(
                messages,
                tools=self._schemas if self._supports else None,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
            )
            delta = TelemetryAccumulator.increment(acc, resp)
            out.update(delta)
            acc = {**acc, **delta}  # accumulate token totals across forced retries
            pending, assistant_msg = self._decide(resp, acc)
            messages = messages + [assistant_msg]
            # DESIGN GATE: on a design-first build turn, finishing (or asking for design approval)
            # before any prototype file exists is structurally blocked — not just discouraged in the
            # prompt. Real work in the same batch (write/edit/… and CLARIFYING questions) survives;
            # only when the batch had NOTHING actionable left does the model get a corrective nudge
            # and a bounded re-query — a stubborn model then ends as "no further action", never a
            # confident fake DONE over an empty sandbox.
            if self.design_gate is not None and not self._design_ready():
                kept = self._filter_design_actions(pending)
                if not kept and pending and attempts < 2:
                    attempts += 1
                    messages = messages + [{"role": "user", "content": self._DESIGN_NUDGE}]
                    continue
                pending = kept
            # TASK GATE: the model may not finish while its own task list still shows unfinished
            # items — unless the same batch also resends the list (that IS the model bringing it up
            # to date). Real work queued alongside the finish survives (only the finish is dropped);
            # a bare premature finish gets ONE corrective nudge and a re-query. A stubborn model
            # still finishes on the give-up path, so this can never loop or brick a turn.
            tasks = list(state.get("tasks") or [])
            unfinished = [t for t in tasks if (t.get("status") or "") != "completed"]
            if (unfinished and any(a.get("tool") == "finish" for a in pending)
                    and not any(a.get("tool") == "tasks" for a in pending)):
                kept = [a for a in pending if a.get("tool") != "finish"]
                if not kept and attempts < 2:
                    attempts += 1
                    messages = messages + [{"role": "user", "content": self._TASKS_NUDGE}]
                    continue
                if kept:
                    pending = kept  # do the queued work; finish can come back on the next think
            # FINISH GATE (injected by the chat runner): an app-state veto on finishing — e.g.
            # "no page is reachable: wire your views into urls.py before finishing". Work queued
            # alongside the finish still runs (only the finish is dropped); a bare vetoed finish
            # gets the veto text as a corrective nudge + one re-query.
            if self.finish_gate is not None and any(a.get("tool") == "finish" for a in pending):
                try:
                    veto = str(self.finish_gate() or "")
                except Exception:
                    veto = ""  # a broken gate must never brick the agent
                if veto and attempts < 2:
                    attempts += 1
                    kept = [a for a in pending if a.get("tool") != "finish"]
                    if kept:
                        pending = kept  # run the queued work; finish is re-checked next think
                    else:
                        messages = messages + [{"role": "user", "content": veto}]
                        continue
            if not self.require_ask:
                break
            has_ask = any((a.get("tool") == "ask") for a in pending)
            # The model is wrapping up when it queued a finish (or produced nothing actionable).
            is_finishing = (not has_ask) and (any(a.get("tool") == "finish" for a in pending) or not pending)
            content = str(assistant_msg.get("content") or "")
            if attempts >= 2:  # don't loop forever / burn budget on a stubborn model
                if at_cap:
                    pending = [a for a in pending if a.get("tool") != "ask"]  # enforce the cap on give-up
                break
            # FLOOR: force at least one question before the refiner may finish.
            if asked == 0 and not has_ask:
                attempts += 1
                pending = [a for a in pending if a.get("tool") != "finish"]  # ignore the premature finish
                messages = messages + [{"role": "user", "content": self._ASK_NUDGE}]
                continue
            # CEILING: after enough questions, stop asking and produce the spec.
            if at_cap and has_ask:
                attempts += 1
                pending = [a for a in pending if a.get("tool") != "ask"]  # ignore the extra question
                messages = messages + [{"role": "user", "content": self._FINISH_NUDGE}]
                continue
            # FINISH-SPEC: the model is finishing but its message has no real structured prompt (it
            # just called finish / output "DONE") — force it to actually WRITE the spec first, else
            # the Prompt Writer stores an empty structured_prompt and the hand-off never appears.
            if is_finishing and asked >= 1 and not self._looks_like_spec(content):
                attempts += 1
                pending = [a for a in pending if a.get("tool") != "finish"]
                messages = messages + [{"role": "user", "content": self._FINISH_NUDGE}]
                continue
            break
        return pending, messages, out

    @staticmethod
    def _looks_like_spec(content: str) -> bool:
        """Heuristic: does the text contain a real structured prompt (not just an empty finish)?"""
        text = re.sub(r"(?im)^\s*DONE\s*$", "", content or "").strip()
        return len(text) >= 120 or "**" in text or "##" in text

    def _design_ready(self) -> bool:
        """The design gate's verdict — open (True) on any gate error, never brick the agent."""
        try:
            return bool(self.design_gate())
        except Exception:
            return True

    @classmethod
    def _is_design_approval(cls, text: str, options=None) -> bool:
        """A design SIGN-OFF ask — not a domain question that merely mentions approving things
        ("Who can approve time-off requests?" must pass). Matches the canonical Approve /
        Request-changes options, or 'approve' wording next to design/prototype wording."""
        for opt in options or []:
            if str(opt).strip().lower().startswith("approve"):
                return True
        t = (text or "").lower()
        return "approv" in t and bool(re.search(r"design|prototype|mock-?up|layout|front-?end", t))

    @classmethod
    def _filter_design_actions(cls, actions: list) -> list:
        """Strip what a design-first turn may NOT do before the prototype exists: finishing, and
        asking the user to APPROVE the (nonexistent) design. Clarifying questions survive — in a
        batched ask only the approval-flavoured questions are removed, never the whole batch."""
        kept = []
        for a in actions:
            tool = a.get("tool") or ""
            if tool == "finish":
                continue
            if tool == "ask":
                args = a.get("args") or {}
                if args.get("questions"):
                    qs = [q for q in args["questions"]
                          if not cls._is_design_approval(
                              str((q or {}).get("question") or (q or {}).get("prompt") or ""),
                              (q or {}).get("options"))]
                    if not qs:
                        continue
                    if len(qs) != len(args["questions"]):
                        a = {**a, "args": {**args, "questions": qs}}
                elif cls._is_design_approval(
                        str(args.get("question") or args.get("prompt") or ""), args.get("options")):
                    continue
            kept.append(a)
        return kept

    @staticmethod
    def _coalesce_asks(actions: list) -> list:
        """Merge ≥2 ``ask`` actions from ONE turn into a single batched ask (``questions:[...]``,
        capped 6) so the UI presents them together as one paginated card. Mirrors what
        parse_actions_ordered does for text models, applied here to the native tool-call path."""
        asks = [a for a in actions if a.get("tool") == "ask"]
        if len(asks) > 1:
            questions: list[dict] = []
            for a in asks:
                ar = a.get("args") or {}
                if ar.get("questions"):
                    questions.extend(ar["questions"])
                else:
                    questions.append({"question": (ar.get("question") or ar.get("prompt") or ""),
                                      "options": ar.get("options") or []})
            batched = {"tool": "ask", "args": {"questions": questions[:6]}}
            merged, inserted = [], False
            for a in actions:
                if a.get("tool") == "ask":
                    if not inserted:
                        merged.append(batched)
                        inserted = True
                else:
                    merged.append(a)
            actions = merged
        # an ask must block for its answer first → drop a co-emitted finish (mirror the text path).
        if any(a.get("tool") == "ask" for a in actions):
            actions = [a for a in actions if a.get("tool") != "finish"]
        return actions

    def _decide(self, resp, state: AgentState) -> tuple[list, dict]:
        """Turn one LLM response into (ordered actions, assistant message to record)."""
        if self._supports and getattr(resp, "tool_calls", None):
            actions: list[dict] = []
            for call in resp.tool_calls:
                tool = CANONICAL_TOOL.get(str(call.get("name") or "").lower())
                if tool and self._allowed(tool):
                    actions.append({"tool": tool, "args": call.get("arguments") or {}})
            actions = self._coalesce_asks(actions)  # ask everything up front (one paginated card)
            assistant_msg = {
                "role": "assistant",
                "content": resp.text or "",
                "tool_calls": [
                    {"function": {"name": c.get("name"), "arguments": c.get("arguments", {})}}
                    for c in resp.tool_calls
                ],
            }
            return actions, assistant_msg
        # text protocol (also the fallback when a tool-capable model replies with plain text)
        actions = parse_actions_ordered(resp.text, state.get("expected_files"), interactive=self.interactive)
        # Drop any action the current mode forbids (e.g. a refiner emitting a write directive).
        actions = [a for a in actions if self._allowed(a.get("tool") or "")]
        return actions, {"role": "assistant", "content": resp.text or ""}

    def _text_protocol(self) -> str:
        """The directive cheatsheet for non-tool models. Restricted modes get a minimal protocol so
        the model is never instructed to write files / run shell (which TEXT_PROTOCOL teaches)."""
        if self.allowed_tools is None:
            return TEXT_PROTOCOL
        lines = ["You ACT using TEXT DIRECTIVES (not prose). Emit ONLY these — do NOT write files, "
                 "edit files, or run shell commands:"]
        if "ask" in self.allowed_tools:
            lines.append(
                "  • Ask the user clarifying questions — emit ALL your questions AT ONCE (3-5 of them): "
                "a line `ASK: <question>` then 2-4 `- option` lines per question, EXACTLY like this:\n"
                "      ASK: Who should be able to create and assign tasks?\n"
                "      - Any logged-in user — anyone with an account can manage tasks\n"
                "      - Only department managers (Recommended) — managers own their team's tasks\n"
                "      - Only admins\n"
                "      ASK: How are tasks organized?\n"
                "      - By project (Recommended) — tasks grouped under projects\n"
                "      - A single flat list\n"
                "    Put each option's one-line description after an em dash (—) and mark the best default "
                "'(Recommended)'. Emit several ASK blocks in this one reply; the user answers them all together."
            )
        lines.append(
            "  • Finish — ONLY after you have asked at least one question and gathered enough answers: "
            "output the STRUCTURED PROMPT in full (## Title, **Overview**, **Features**, **Data models**, "
            "**Constraints**), then a final line `DONE`."
        )
        lines.append("Never write a chatty summary instead of an ASK. Never ask the user to write the "
                     "prompt for you. Your FIRST reply must be ASK blocks (ask everything up front).")
        return "\n".join(lines)

    def _init_messages(self, state: AgentState) -> list:
        expected = ", ".join(state.get("expected_files", []) or [])
        extra = (
            "\n\nUse the provided tools, one per turn. Call run_tests to verify before finish."
            if self._supports else "\n\n" + self._text_protocol()
        )
        user = self.USER_TEMPLATE.format(
            title=state.get("task_title", ""),
            description=state.get("task_description", ""),
            expected_section=(f"Expected files: {expected}\n\n" if expected else ""),
        )
        return [
            {"role": "system", "content": self.system_prompt + extra},
            {"role": "user", "content": user},
        ]


# --------------------------------------------------------------------- tool nodes
def _approval_ok(handler, tool: str, command: str) -> bool:
    """Ask the user to approve a risky command. Exceptions propagate (a cancelled/deleted chat
    aborts the turn) rather than silently bypassing the gate."""
    return bool(handler("approval", {"tool": tool, "command": command}))


def _run_tests(state: AgentState, sandbox_timeout: int | None) -> tuple[str, dict]:
    """Run the benchmark tests against the current workspace files; return (observation, domain)."""
    from src.eval.sandbox import DjangoSandbox

    task = _task_from_state(state)
    tools = WorkspaceTools(_workspace(state), task=task)
    files = tools.collected_files()
    if not files:
        return ("ERROR: no source files written yet — write code before running tests.", {})
    try:
        result = DjangoSandbox(timeout_sec=sandbox_timeout).run(task, files)
    except Exception as exc:  # pragma: no cover - sandbox setup failure
        return (f"ERROR: could not run tests: {exc}", {})
    verdict = "PASSED" if result.passed else "FAILED"
    body = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
    summary = f"{verdict} — passed={result.tests_passed} failed={result.tests_failed} (exit {result.returncode})"
    observation = f"{summary}\n{body}".strip()
    domain = {
        "sandbox_passed": result.passed,
        "sandbox_stdout": result.stdout,
        "sandbox_stderr": result.stderr,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "generated_files": files,
        # each verification run is one "iteration" in the dynamic agent
        "iteration": int(state.get("iteration", 0) or 0) + 1,
    }
    return observation, domain


def make_tool_node(
    tool_name: str,
    *,
    llm: LLMBackend | None = None,
    retriever=None,
    sandbox_timeout: int | None = None,
    shell_timeout: int | None = None,
    interaction_handler=None,
    approval_tools=None,
):
    """Build a LangGraph node that executes ``tool_name`` from ``state['next_action']``.

    Every node appends its observation to ``messages``, bumps the step/tool counters, records a
    one-line ``tool_log`` entry, and returns whatever domain state its tool produces (generated
    files for write/edit, sandbox results for test, plan/retrieved_context for plan/retrieve).

    ``interaction_handler(kind, payload)`` (chat mode) lets the ``ask`` node ask the user a
    question, and gates the tools in ``approval_tools`` (e.g. shell) behind Accept/Deny.
    """
    supports = bool(getattr(llm, "supports_tools", False)) if llm is not None else False
    approval = set(approval_tools or [])

    def node(state: AgentState) -> dict:
        action = state.get("next_action") or {}
        args = action.get("args") or {}
        observation = ""
        domain: dict = {}
        target = ""
        written_content = None  # exact content for write/edit (carried so the UI never re-looks-up by path)

        if tool_name == "ask":
            batch = args.get("questions")  # multiple questions asked at once (planning), else single
            if batch:
                questions = [{"prompt": (q.get("question") or q.get("prompt") or "").strip(),
                              "options": q.get("options") or []} for q in batch if q]
                questions = [q for q in questions if q["prompt"]]
                target = (questions[0]["prompt"][:60] + (" +" + str(len(questions) - 1) if len(questions) > 1 else "")) if questions else "questions"
                domain["asked"] = True
                domain["ask_count"] = int(state.get("ask_count", 0) or 0) + len(questions)
                if interaction_handler and questions:
                    try:
                        answers = interaction_handler("question", {"questions": questions})
                    except RuntimeError:
                        raise
                    except Exception as exc:  # pragma: no cover
                        # A user-initiated Stop surfaces as TurnCancelled from the interaction loop;
                        # it must abort the turn, not be swallowed into a benign "ERROR asking" card
                        # (matched by name so the agent layer stays decoupled from the dashboard).
                        if type(exc).__name__ == "TurnCancelled":
                            raise
                        answers = []
                        observation = f"ERROR asking the user: {exc}"
                    if not observation:
                        answers = list(answers or [])
                        lines = []
                        for k, q in enumerate(questions):
                            a = str(answers[k]).strip() if k < len(answers) else ""
                            lines.append(f"Q: {q['prompt']}\nA: {a or '(no answer)'}")
                        observation = "The user answered:\n" + "\n".join(lines)
                else:
                    observation = "No user is available to answer; proceed with reasonable assumptions."
            else:
                question = (args.get("question") or args.get("prompt") or "").strip()
                options = args.get("options") or []
                target = question[:80]
                domain["asked"] = True  # satisfies the refiner's require-ask-before-finish gate
                domain["ask_count"] = int(state.get("ask_count", 0) or 0) + 1  # ...and counts toward its cap
                if interaction_handler:
                    try:
                        answer = interaction_handler("question", {"prompt": question, "options": options})
                    except RuntimeError:
                        raise  # cancellation (chat/turn deleted) — abort the turn, don't swallow
                    except Exception as exc:  # pragma: no cover - a question failing shouldn't crash the build
                        # A user-initiated Stop surfaces as TurnCancelled — abort the turn rather than
                        # swallowing it as a benign ask error (matched by name to keep the agent layer
                        # decoupled from the dashboard's exception type).
                        if type(exc).__name__ == "TurnCancelled":
                            raise
                        answer = ""
                        observation = f"ERROR asking the user: {exc}"
                    if not observation:
                        answer = str(answer).strip()
                        observation = (f"The user answered: {answer}" if answer
                                       else "The user gave no answer; proceed with your best judgment.")
                else:
                    observation = "No user is available to answer; proceed with a reasonable assumption."

        elif tool_name == "tasks":
            tasks = normalize_tasks(args.get("tasks"))
            domain["tasks"] = tasks
            done = sum(1 for t in tasks if t["status"] == "completed")
            active = next((t["content"] for t in tasks if t["status"] == "in_progress"), "")
            target = (active or f"{len(tasks)} tasks")[:80]
            glyph = {"completed": "x", "in_progress": ">", "pending": " "}
            observation = "Task list updated:\n" + "\n".join(
                f"- [{glyph[t['status']]}] {t['content']}" for t in tasks
            ) if tasks else "Task list cleared."

        elif tool_name == "plan":
            from .PlannerNode import PlannerNode

            planned = PlannerNode(llm).run(state)
            plan = (planned.get("plan") or "").strip()
            assumptions = (planned.get("assumptions") or "").strip()
            observation = plan + (("\n\nASSUMPTIONS:\n" + assumptions) if assumptions else "")
            domain["plan"] = plan
            domain["assumptions"] = assumptions
            for key in ("total_prompt_tokens", "total_completion_tokens", "total_latency_sec", "llm_calls"):
                if key in planned:
                    domain[key] = planned[key]

        elif tool_name == "retrieve":
            query = (args.get("query") or "").strip() or \
                f"{state.get('task_title', '')}\n{state.get('task_description', '')}".strip()
            target = query[:80]
            if retriever is None:
                observation = "Django docs retrieval is not available in this run."
            else:
                try:
                    context = retriever.format_context(retriever.retrieve(query, k=5))
                except Exception as exc:  # pragma: no cover - retrieval failure
                    context = ""
                    observation = f"ERROR retrieving docs: {exc}"
                if context:
                    domain["retrieved_context"] = context
                    observation = context

        elif tool_name == "test":
            observation, domain = _run_tests(state, sandbox_timeout)

        else:
            tools = WorkspaceTools(
                _workspace(state), task=_task_from_state(state), shell_timeout=shell_timeout or 180
            )
            if tool_name == "read":
                target = args.get("path", "")
                observation = tools.read_file(target)
            elif tool_name == "grep":
                target = args.get("pattern", "")
                observation = tools.grep(target, args.get("path"))
            elif tool_name == "list":
                observation = tools.list_files()
            elif tool_name == "shell":
                target = args.get("command", "")
                if interaction_handler and tool_name in approval and not _approval_ok(interaction_handler, tool_name, target):
                    observation = f"Skipped — the user denied this command: {target}"
                else:
                    observation = tools.run_shell(target)
            elif tool_name == "write":
                target = _norm(args.get("path", ""))
                observation = tools.write_file(args.get("path", ""), args.get("content", ""))
                domain["generated_files"] = tools.collected_files()
                written_content = tools.written.get(target)  # None if the write errored
            elif tool_name == "edit":
                target = _norm(args.get("path", ""))
                observation = tools.edit_file(
                    args.get("path", ""), args.get("search", ""), args.get("replace", "")
                )
                domain["generated_files"] = tools.collected_files()
                written_content = tools.written.get(target)  # None if the edit errored
            else:  # pragma: no cover - defensive; route_next never sends an unknown tool
                observation = f"ERROR: unknown tool '{tool_name}'."

        last_action = {"tool": tool_name, "target": str(target)}
        if written_content is not None:
            last_action["content"] = written_content
        messages = _append_observation(state, observation, supports)
        return {
            "messages": messages,
            "last_observation": observation,
            "last_action": last_action,
            "next_action": None,
            "step_count": int(state.get("step_count", 0) or 0) + 1,
            "tool_calls_made": int(state.get("tool_calls_made", 0) or 0) + 1,
            "tool_log": _append_log(state, tool_name, target, observation),
            **domain,
        }

    node.__name__ = f"tool_{tool_name}"
    return node
