"""Run one project-chat turn: drive the dynamic agent against the project sandbox.

A "turn" is a ``ModelRun`` (chat set, benchmark null) whose ``workspace_path`` is the
project sandbox. We REUSE the proven streaming machinery in
:class:`dashboard.services.agent_runner.AgentModelRunner` (``_stream`` → ``_handle_node`` →
``_command`` / ``_persist_files``) so the chat timeline + Files/Preview/Database tabs work
exactly like benchmark runs — we just feed it a chat task instead of a benchmark and finalize
the turn ourselves (no benchmark ranking). Runs in a daemon thread, like ``JobRunner``.

Phase 1: autonomous (no clarifying questions / approvals — that is Phase 2 via an
``interaction_handler``). The agent reads/writes/edits/greps/shells in the sandbox to fulfil
the user's request, then finishes.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from django.db import close_old_connections
from django.utils import timezone

from dashboard.models import Chat, CommandLog, ModelRun, PendingInteraction, RunStatus
from dashboard.models.JobLog import JobLog

#: Total tool-step budget per turn by effort level.
_EFFORT_STEPS = {"low": 15, "medium": 30, "high": 60}
#: How long the agent thread will block waiting for a user answer, and the poll interval.
_INTERACTION_TIMEOUT_SEC = 1800
_INTERACTION_POLL_SEC = 0.6


def _make_cancel_check(model_run_id: int):
    """A throttled (~1s) DB probe the Ollama backend calls between streamed chunks, so Stop
    aborts the CURRENT generation instead of waiting out the whole 80s+ blocking call."""
    state = {"last": 0.0, "hit": False}

    def check() -> bool:
        if state["hit"]:
            return True
        now = time.monotonic()
        if now - state["last"] < 1.0:
            return False
        state["last"] = now
        state["hit"] = ModelRun.objects.filter(id=model_run_id, status=RunStatus.CANCELLED).exists()
        return state["hit"]

    return check

#: Refiner (Prompt Writer) question cap — after this many clarifying questions the agent is forced
#: to produce the structured prompt, so a weak model can't loop forever asking trivial near-dupes.
_REFINER_MAX_QUESTIONS = 6

#: Splits an answer option "Label — description" into label + tooltip description. Requires an
#: em/en dash or a double hyphen with surrounding spaces so labels like "Bug-fix" don't split.
_OPT_DESC_SPLIT = re.compile(r"\s+(?:—|–|--)\s+")
#: Markdown emphasis a weak model wraps option text in (`**bold**`, `_em_`, backticks).
_OPT_MD_STRIP = re.compile(r"(\*\*|__|[*_`])")
#: The "(Recommended)" marker — flagged separately, never shown inside the label text.
_OPT_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*", re.IGNORECASE)
#: A label that is just the prompt template's placeholder ("Option A", "B", "3"), copied verbatim.
_OPT_PLACEHOLDER = re.compile(r"^(?:option\s+)?[a-f0-9]$", re.IGNORECASE)


def _clean_opt_text(s: str) -> str:
    """Strip markdown emphasis, template angle brackets and stray whitespace from option text."""
    s = _OPT_MD_STRIP.sub("", str(s or "")).strip()
    return s.strip("<>").strip()


def _build_opts(options: list) -> list[dict]:
    """Turn raw option strings into rich option dicts for the question card.

    Supports the Codex-style format "Label (Recommended) — short description": the part after the
    dash becomes the ⓘ tooltip; "(Recommended)" anywhere in the option flags the recommended choice.
    Weak models copy the prompt template literally ("**Option A (Recommended)** — real text"), so
    markdown is stripped, the marker is removed from the visible label, and a placeholder label is
    replaced by its description (the real content).
    """
    opts = []
    for i, raw in enumerate((options or [])[:8]):
        s = str(raw).strip()
        recommended = "(recommended)" in s.lower()  # check the whole option, not just the label
        label, desc = s, ""
        m = _OPT_DESC_SPLIT.search(s)
        if m:
            label, desc = s[:m.start()].strip(), s[m.end():].strip()
        label = _OPT_RECOMMENDED.sub(" ", _clean_opt_text(label)).strip()
        desc = _OPT_RECOMMENDED.sub(" ", _clean_opt_text(desc)).strip()
        if desc and _OPT_PLACEHOLDER.match(label or ""):
            label, desc = desc, ""  # the description IS the option — promote it
        if not label:
            label = desc or _clean_opt_text(s) or s
        opts.append({
            "key": chr(65 + i) if i < 26 else str(i + 1),
            "label": label,
            "description": desc,
            "recommended": recommended,
        })
    return opts

CHAT_SYSTEM = (
    "You are an expert senior Django developer working directly in the user's PROJECT SANDBOX "
    "(a real directory on disk that can be run). You operate as a dynamic agent: each turn you "
    "choose the single most useful next action with ONE tool, observe the result, and continue "
    "until the user's request is done.\n\n"
    "Tools: ask_user, update_tasks, plan, retrieve_docs, list_files, read_file, grep, write_file, "
    "edit_file, run_shell, run_tests, finish.\n"
    "- JUST CHATTING? If the user's message is conversational (a greeting, a thank-you, a general "
    "question that needs no project changes), simply call `finish` with a helpful answer — write "
    "no files, run nothing, ask nothing.\n"
    "- CLARIFY FIRST: if the request is ambiguous (missing fields, choices, scope), call ask_user "
    "with 2-4 concrete options BEFORE building. Keep asking until the task is clear — but do NOT "
    "ask about things you can reasonably decide yourself. If nothing is genuinely unclear, ask "
    "NO questions at all.\n"
    "- PLAN VISIBLY: for any multi-step request, call update_tasks to lay out the steps as a todo "
    "list, mark one in_progress, and complete them as you go (resend the full list each update).\n"
    "- Then ORIENT: if the project already has files, use list_files / read_file / grep to "
    "understand it before changing anything.\n"
    "- Implement the request with real, correct, complete code. Prefer edit_file for small changes "
    "and write_file for new files. Import everything you use; target Django 5.x.\n"
    "- File paths are SHORT relative paths like `blog/models.py`.\n"
    "- This project has its OWN virtualenv (already created with django, djangorestframework, "
    "pillow, pytest, pytest-django). Run `pip install <pkg>` via run_shell to add any other "
    "dependency it needs — it installs into the project venv.\n"
    "- You may run shell commands (e.g. `python manage.py ...`) to set up or verify the project.\n"
    "- NEVER ask the user about the PROJECT'S OWN STATE (whether files exist, whether a command "
    "ran, whether the project was created) — inspect it yourself with list_files/read_file/"
    "run_shell and FIX problems directly. ask_user is ONLY for requirements and design decisions.\n"
    "- STYLING IS MANDATORY: every page you build must actually load real CSS — put stylesheets in "
    "the project's `static/` dir and link them from templates with "
    "`<link rel=\"stylesheet\" href=\"/static/styles.css\">` (or embed a complete `<style>` block). "
    "If a design prototype was approved earlier, PORT its styles.css/script.js into `static/` "
    "verbatim and keep the pages looking IDENTICAL to the approved design — never replace it with "
    "a bare-bones version.\n"
    "- When the request is complete, call `finish` with a SHORT plain-language summary of what you "
    "changed and how the user can run/verify it."
)

#: Prepended to the FIRST turn of a brand-new project — design-first, with a user approval gate.
_DESIGN_FIRST = (
    "Follow a DESIGN-FIRST workflow for this new project:\n"
    "1) FIRST call update_tasks with the checklist for the whole build (design prototype, design "
    "approval, Django backend, tests), marking the first item in_progress.\n"
    "2) If anything important is ambiguous, use ask_user (with 2-4 concrete options) until the goal "
    "is clear.\n"
    "3) Build the FRONT-END DESIGN first: plain HTML page(s) + CSS + JS with realistic placeholder "
    "content. Do NOT build the models / business logic / Django project yet.\n"
    "4) Then call ask_user: \"Approve this design, or tell me what to change?\" with options "
    "[\"Approve\", \"Request changes\"]. If the user asks for changes, revise the design and ask again.\n"
    "5) ONLY AFTER the user approves, build the Django BACKEND: models, views, forms, urls, admin; "
    "port the approved design into templates/static and wire it to real data; run migrations. "
    "Porting means COPYING the approved index.html into the template and styles.css/script.js into "
    "`static/` (linked as `/static/styles.css`), so the final page looks IDENTICAL to the approved "
    "design — never ship an unstyled or simplified version.\n"
    "6) Finish with a short summary.\n"
    "HARD RULE: a task may be marked completed ONLY when its files actually exist (you called "
    "write_file for them). Never claim the design is done, ask for approval, or finish before "
    "`index.html` exists in the sandbox."
)

#: System prompt for an APPROVED-plan implementation turn (the "Implement this plan" button). Enforces
#: a strict, task-tracked, design-first ORDER: standalone prototype → preview → approve → real
#: `django-admin startproject` → port into Django → tests. The approved plan + the live task list are
#: injected into the task description by ``_build_task``.
BUILD_WORKFLOW_SYSTEM = (
    "You are an expert senior Django developer implementing an APPROVED PLAN (it is in the task) in the "
    "user's PROJECT SANDBOX — a real directory you can write to and run. Work as a dynamic agent: each "
    "step, choose ONE tool, observe the result, continue. Tools: ask_user, update_tasks, list_files, "
    "read_file, grep, write_file, edit_file, run_shell, run_tests, finish.\n\n"
    "ALWAYS follow the approved plan and the active task list. Before each major change, briefly say "
    "what you are about to do; after each step, verify it worked before moving on.\n\n"
    "FIRST, call update_tasks with the full checklist for this build (the steps below, broken down), "
    "marking one in_progress; update it (resend the full list) as you complete each item.\n\n"
    "Then follow this ORDER — do NOT skip or reorder:\n"
    "1. FRONT-END PROTOTYPE FIRST — write a standalone prototype with plain HTML, CSS and JavaScript at "
    "the sandbox ROOT (an `index.html` plus `.css`/`.js` files). NO Django yet. Use realistic content "
    "so the user can judge the look. It is served live in the Preview tab automatically. Then call "
    "ask_user \"Approve this prototype, or what should change?\" with options [\"Approve\", \"Request "
    "changes\"]. If anything in the design is ambiguous or looks wrong, ask focused DESIGN questions "
    "(with options) before continuing. Revise + re-ask until approved. Do NOT run startproject yet.\n"
    "2. START THE DJANGO PROJECT — only AFTER the prototype is approved: run_shell "
    "`django-admin startproject <name> .` in the sandbox (note the trailing dot → files land in the "
    "sandbox root). Then list_files to VERIFY the structure (manage.py + <name>/settings.py exist).\n"
    "3. IMPLEMENT THE APPROVED FRONT-END INTO DJANGO — move the prototype into Django templates + "
    "static files: copy the approved HTML into templates and its styles.css/script.js into `static/` "
    "(linked as `/static/styles.css`), keeping the pages IDENTICAL to the approved design — never an "
    "unstyled or simplified version. Create views, URL routes, models (if needed) and any helper "
    "modules/services. Keep the structure clean, scalable and maintainable. Run migrations.\n"
    "4. TESTS — write tests for views, URLs, templates and core logic (writing tests first is fine), "
    "then run_tests until they pass; make sure the app runs.\n"
    "5. finish with a short summary of what you built and how to run it.\n\n"
    "Paths are SHORT relative paths (e.g. `index.html`, `core/views.py`). This project has its OWN "
    "virtualenv (django + drf + pillow + pytest + pytest-django preinstalled); `pip install <pkg>` via "
    "run_shell adds anything else. Import everything you use; target Django 5.x."
)

#: System prompt for the Prompt Writer (a Chat in mode="prompt_writer"): refine an idea into a
#: structured, buildable spec. It writes NO code — only ask_user + finish.
REFINER_SYSTEM = (
    "You are a senior product analyst who turns a rough web-app idea into a clear, buildable spec by "
    "interviewing the user. You do NOT write code — your only tools are ask_user and finish.\n\n"
    "WORKFLOW (follow in order):\n"
    "1. ASK ALL of your clarifying questions AT ONCE, in your FIRST message — emit a SEPARATE ask_user "
    "call (or `ASK:` block) for EACH question, about 3-5 of them and AT MOST 6. Do NOT ask one at a "
    "time across turns; ask everything you need up front (they are shown to the user together). Cover "
    "the genuinely unclear, BUILD-CRITICAL decisions: who manages the data, auth/roles, the core "
    "workflow, ambiguous field meanings (e.g. what 'remaining days' counts from). GROUP related "
    "decisions into one question; do NOT ask a separate question per field or trivial per-field "
    "permission.\n"
    "2. Give each question 2-4 concrete options. Format every option as "
    "`Label (Recommended) — short description`: a one-line description after an em dash (—), and add "
    "\"(Recommended)\" to the single best default. The UI also offers a free-text 'Other'.\n"
    "3. After the answers come back, only if something critical is still unclear ask ONE more focused "
    "round — otherwise go straight to finish. Then call finish and put the STRUCTURED PROMPT — which "
    "YOU write yourself — in the finish summary, using this markdown shape:\n"
    "   ## <Project title>\n   **Overview:** <2-3 sentences>\n   **Features:**\n   - <feature>\n"
    "   **Data models:**\n   - <Model>: <fields>\n   **Constraints:** <any>\n\n"
    "HARD RULES: Ask everything you need in the FIRST message (all questions at once). Never ask the "
    "user to write or provide a prompt — generating the structured prompt is YOUR job. Never finish "
    "before asking at least one question. Don't ask more than ~6 questions. Never write code."
)

#: Appended to the refiner prompts ONLY for the composer "Planning" toggle (never for the
#: standalone Prompt Writer, whose spec also seeds autonomous benchmarks where a "wait for user
#: approval" phase would stall the run). The produced plan drives the design-first build
#: (BUILD_WORKFLOW_SYSTEM), so the plan itself must spell those phases out for the user.
_PLAN_WORKFLOW_SECTION = (
    "\n\nIMPORTANT — the plan you write drives a DESIGN-FIRST build, so it MUST also contain a "
    "'**Build workflow:**' section (after Constraints) with these phases, tailored to THIS project "
    "(name its actual pages and models — don't copy this text verbatim):\n"
    "1. Front-end design prototype — plain HTML/CSS/JS pages with realistic placeholder content, "
    "served live in the Preview tab for review.\n"
    "2. Design approval — the user approves the design or requests changes; revise and re-present "
    "until approved.\n"
    "3. Django implementation — start the real Django project and port the APPROVED design into "
    "templates/static; build the models, views, forms, urls and admin; run migrations.\n"
    "4. Tests & verification — tests for views, urls and core logic; everything runs."
)

#: System prompt for the SECOND half of planning, after the whole question batch has already been
#: asked + answered up front (see ``run_turn``/``_plan_questions``). The clarifying round is DONE, so
#: this variant tells the model to write the spec now and never ask again — paired with a seeded
#: ask-count at the cap so the RouterNode CEILING structurally drops any stray question.
REFINER_PLAN_SYSTEM = (
    "You are a senior product analyst. The user has ALREADY answered your clarifying questions "
    "(their answers are in the task description). The interview is OVER — do NOT ask anything else. "
    "Your only job now is to WRITE the final STRUCTURED PROMPT and call finish with it.\n\n"
    "Put the WHOLE spec in your finish message, in this markdown shape:\n"
    "   ## <Project title>\n   **Overview:** <2-3 sentences>\n   **Features:**\n   - <feature>\n"
    "   **Data models:**\n   - <Model>: <fields>\n   **Constraints:** <any>\n\n"
    "Write it yourself from the user's answers. You do NOT write code and you do NOT ask questions."
)


def start_turn(chat_id: int, model_run_id: int) -> None:
    """Launch a chat turn in a background daemon thread (returns immediately)."""
    thread = threading.Thread(target=_run_turn_thread, args=(chat_id, model_run_id), daemon=True)
    thread.start()


#: Heartbeat cadence while a turn runs, and how long a QUEUED/RUNNING turn may go without a
#: beat before it is declared dead. Daemon threads die WITH the dev server process, so a server
#: restart mid-turn used to leave the run "running" forever (frozen chat, composer locked).
#: The heartbeat thread touches ``updated_at`` every few seconds; ``heal_stale_turns`` (called
#: from the chat polls) fails any active run whose beat has gone stale. The thresholds comfortably
#: exceed one beat (so a busy box never false-positives) while staying far below the old
#: "stuck forever": detection within ~a minute of opening the chat.
_HEARTBEAT_SEC = 5
_STALE_RUNNING_SEC = 60
_STALE_QUEUED_SEC = 120
_STALE_ERROR = (
    "The server restarted (or the agent thread died) while this turn was running. "
    "Use Retry to run it again."
)


def _prior_tasks(chat: Chat) -> list:
    """The chat's CURRENT task list (newest ``agent="tasks"`` CommandLog across ALL turns).

    Seeded into each new turn's agent state: the Tasks chip persists across turns, but the agent
    state starts empty — without this seed the router's task gate can't see tasks created in an
    earlier turn, and the model happily finishes with the chip stuck at 0/N."""
    import json as _json

    log = CommandLog.objects.filter(model_run__chat=chat, agent="tasks").order_by("-id").first()
    if not log or not log.stdout:
        return []
    try:
        data = _json.loads(log.stdout)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _routes_gate(sandbox) -> "callable":
    """A finish-gate predicate for build turns: veto finishing while the Django project has NO
    reachable app page (URLconf wires only admin/defaults → Preview shows Django's default page).
    The agent must wire its views into urls.py ITSELF instead of leaving that to the user.
    Bounded: after 2 vetoes it stops vetoing, so a stubborn model still terminates."""
    root = Path(sandbox or "")
    state = {"vetoes": 0}

    def check() -> str:
        if state["vetoes"] >= 2 or not (root / "manage.py").exists():
            return ""  # static/prototype phase (design gate owns it) or gate exhausted
        try:
            from .route_discovery import discover_routes

            if discover_routes(root):
                return ""
        except Exception:
            return ""  # discovery failure must never block finishing
        state["vetoes"] += 1
        return (
            "STOP — do not finish yet. The app has NO reachable pages: the project's URLconf wires "
            "only admin/default routes, so the Preview shows Django's default placeholder page. "
            "Wire your views into the project's urls.py (the module named by ROOT_URLCONF) with a "
            "route at '' for the main page, verify with `python manage.py check`, and only then "
            "finish."
        )

    return check


def _frontend_gate(sandbox) -> "callable":
    """A design-gate predicate: True once the sandbox contains a USER-authored .html file
    (vendored templates in the project venv never count — see previews._has_frontend). Caches
    the first True (the gate never closes again within a turn — no repeated scans after that)."""
    from .previews import _has_frontend

    root = Path(sandbox or "")
    state = {"ready": False}

    def ready() -> bool:
        if not state["ready"]:
            state["ready"] = _has_frontend(root)
        return state["ready"]

    return ready


def _heartbeat_loop(model_run_id: int, stop: threading.Event) -> None:
    """Touch the run's ``updated_at`` every _HEARTBEAT_SEC while the turn is active.

    Runs in its own daemon thread so beats continue through long LLM calls and interaction
    waits. The filtered UPDATE goes quiet on its own once the turn reaches a final status."""
    while not stop.wait(_HEARTBEAT_SEC):
        try:
            close_old_connections()
            changed = ModelRun.objects.filter(
                id=model_run_id, status__in=[RunStatus.QUEUED, RunStatus.RUNNING],
            ).update(updated_at=timezone.now())
            if not changed:  # turn reached a final state (or was deleted) — stop beating
                return
        except Exception:  # never let a beat failure kill the beat loop
            pass


def heal_stale_turns(chat: Chat) -> bool:
    """Fail any QUEUED/RUNNING turn of this chat whose heartbeat went stale (orphaned by a
    server restart or a dead agent thread). Returns True when something was healed.

    Called from the chat polls/actions — multi-process safe: a turn owned by ANOTHER server
    process keeps beating through the shared DB, so it reads as alive here and is left alone.
    """
    now = timezone.now()
    healed = False
    stale = list(ModelRun.objects.filter(
        chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING],
    ))
    for run in stale:
        limit = _STALE_RUNNING_SEC if run.status == RunStatus.RUNNING else _STALE_QUEUED_SEC
        if (now - run.updated_at).total_seconds() <= limit:
            continue
        # Re-check with a guarded UPDATE so two concurrent polls can't both "heal" it.
        rows = ModelRun.objects.filter(id=run.id, status=run.status, updated_at=run.updated_at).update(
            status=RunStatus.FAILED, error_summary=_STALE_ERROR, return_code=-1,
            finished_at=now, updated_at=now,
        )
        if rows:
            healed = True
            CommandLog.objects.create(
                model_run=run, kind=CommandLog.Kind.SYSTEM, agent="error",
                command=[], stdout=_STALE_ERROR, exit_code=-1,
            )
            # The dead thread can never consume an answer: resolve its pending questions /
            # approvals too, or the ghost wizard re-renders on every poll forever.
            PendingInteraction.objects.filter(
                model_run=run, status=PendingInteraction.Status.PENDING,
            ).update(status=PendingInteraction.Status.DENIED,
                     answer="(turn ended: the server restarted)", answered_at=now)
    if healed:
        Chat.objects.filter(id=chat.id).update(status=RunStatus.FAILED, updated_at=now)
    return healed


def _run_turn_thread(chat_id: int, model_run_id: int) -> None:
    close_old_connections()
    stop_beat = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(model_run_id, stop_beat), daemon=True).start()
    try:
        ChatTurnRunner().run_turn(chat_id, model_run_id)
    except Exception as exc:  # pragma: no cover - surfaces to the UI
        try:
            mr = ModelRun.objects.get(id=model_run_id)
            mr.mark_finished(RunStatus.FAILED, -1, str(exc)[:500])
            Chat.objects.filter(id=chat_id).update(status=RunStatus.FAILED, updated_at=timezone.now())
        except Exception:
            pass
    finally:
        stop_beat.set()
        close_old_connections()


class _ChatOrchestratorStub:
    """Minimal stand-in for BenchmarkOrchestrator so we can reuse AgentModelRunner's methods."""

    def __init__(self, log_cb=None) -> None:
        self._active_monitor = None
        self._log_cb = log_cb

    def _job_log(self, message: str) -> None:
        if self._log_cb:
            self._log_cb(message)

    def _ranking_score(self, parsed, model_run) -> float:  # unused in chat finalize
        return 0.0


class ChatTurnRunner:
    """Execute a single chat turn end-to-end against the project sandbox."""

    def run_turn(self, chat_id: int, model_run_id: int) -> dict:
        from src.agent.graph import build_agent
        from src.agent.state import TelemetryAccumulator
        from src.llm.OllamaBackend import OllamaBackend
        from .agent_runner import AgentModelRunner, TurnCancelled

        chat = Chat.objects.select_related("project").get(id=chat_id)
        model_run = ModelRun.objects.get(id=model_run_id)
        project = chat.project
        sandbox = project.sandbox_path

        # Stopped while still queued (before the thread got here): don't start. mark_running is a
        # GUARDED transition (only QUEUED→RUNNING), so a Stop that lands in the race window is
        # respected — the refresh below then reads CANCELLED and we bail before doing any work.
        if model_run.status == RunStatus.CANCELLED:
            return {}

        model_run.mark_running()
        if model_run.status != RunStatus.RUNNING:
            return {}
        Chat.objects.filter(id=chat.id).update(status=RunStatus.RUNNING, updated_at=timezone.now())

        mode = (chat.settings or {}).get("mode")
        # "Planning mode" = the refiner behaviour (ask → produce a plan, write no code). The
        # standalone Prompt Writer set mode="prompt_writer"; the composer toggle sets
        # settings["planning"]; a "Refine with AI" click sets a ONE-SHOT settings["_refine_pending"]
        # (so refine is plan-only for this turn without permanently trapping the chat in planning).
        planning = (mode == "prompt_writer" or bool((chat.settings or {}).get("planning"))
                    or bool((chat.settings or {}).get("_refine_pending")))
        config = self._config(chat)
        tag = self._resolve_tag(chat)
        backend = OllamaBackend(model=tag)
        # Stop button responsiveness: the backend streams and checks this between chunks.
        backend.cancel_check = _make_cancel_check(model_run.id)

        runner = AgentModelRunner(_ChatOrchestratorStub(lambda m: self._job_log(chat, m)))
        # Surface the user's message as the first card of the turn.
        runner._command(model_run, CommandLog.Kind.SYSTEM, "user", command="", stdout=(model_run.user_prompt or ""))

        # Attribute the turn to the model that ACTUALLY runs it. Auto turns are created with the
        # placeholder name "agent" (the tag is resolved only here); and OllamaBackend may have
        # substituted a missing tag with a same-family one — record both, or per-model stats and
        # the timeline silently misattribute this turn's output.
        resolved = str(getattr(backend, "model", "") or "")
        if resolved and model_run.model_name in ("", "agent"):
            model_run.model_name = resolved
            ModelRun.objects.filter(id=model_run.id).update(model_name=resolved, updated_at=timezone.now())
        if resolved and tag and resolved != tag:
            runner._command(model_run, CommandLog.Kind.SYSTEM, "router", command="",
                            stdout=f"Model '{tag}' is not installed — running '{resolved}' instead.")

        # CONVERSATIONAL turns: if the message is just chat (a greeting, small talk, a general
        # question — not a request to build/change/run anything), answer it directly with ONE LLM
        # call: no venv setup, no agent loop, no clarifying questions, no files, no preview. The
        # classifier errs toward BUILD, so a real request never gets a chat-only answer. Skipped
        # while planning and during an approved-plan build (those are explicit workflows).
        intent_usage: dict = {}
        if not planning and not (chat.settings or {}).get("plan_approved"):
            chat_start = time.perf_counter()
            chat_only, intent_usage = self._classify_intent(backend, chat, model_run)
            if chat_only:
                state = dict(TelemetryAccumulator.initial())
                for k, v in intent_usage.items():
                    state[k] = state.get(k, 0) + v
                exc = None
                try:
                    reply, reply_usage = self._chat_reply(backend, chat, model_run)
                    for k, v in reply_usage.items():
                        state[k] = state.get(k, 0) + v
                    runner._command(model_run, CommandLog.Kind.SYSTEM, "finish", command="", stdout=reply)
                except Exception as e:  # surfaces as a failed turn + retry bar, like agent errors
                    exc = e
                self._finalize(chat, model_run, state, exc, time.perf_counter() - chat_start)
                return state

        # Per-project virtualenv: prepared in the BACKGROUND with no timeline noise — the visible
        # flow starts with the agent's tasks + design work, not "Creating the virtualenv…" cards.
        # The design-first workflow needs python only AFTER the prototype + user approval (human
        # minutes), by which time the install has finished; until then every venv consumer
        # (migrate / runserver / agent shell) falls back to sys.executable. Failures DO surface
        # as a card. (Idempotent + locked; skipped while planning, which never touches code.)
        if not planning:
            self._ensure_venv_async(runner, model_run, sandbox)

        auto_approve = bool((chat.settings or {}).get("auto_approve"))

        def interaction_handler(kind, payload):
            return self._handle_interaction(runner, model_run, kind, payload, auto_approve)

        start = time.perf_counter()

        # PLANNING: ask EVERY clarifying question in ONE generation, not one-by-one across turns.
        # Weak local models emit a single ask per ReAct step, so leaving the question round to the
        # agent loop makes the refiner dribble questions out (ask → think → ask → think …). On the
        # first planning round we instead make one dedicated call that LISTS every question, present
        # them together as a single paginated batch, then let the agent write the plan with the
        # answers already in hand. If the model produces no parseable questions we fall straight
        # through to the agent's own ask loop (unchanged).
        planning_qa = ""
        asked_upfront = 0
        plan_usage: dict = {}
        # A "Refine with AI" turn revises the EXISTING plan — never re-open the clarifying-question
        # batch for it (that would re-interview instead of applying the edits).
        is_refine_turn = bool((chat.settings or {}).get("_refine_pending"))
        if planning and not is_refine_turn and self._prior_question_count(chat) == 0:
            try:
                runner._command(model_run, CommandLog.Kind.SYSTEM, "router", command="",
                                stdout="Analyzing your idea to prepare all the questions at once…")
                questions, plan_usage = self._plan_questions(backend, chat, model_run)
                if questions:
                    answers = self._handle_question_batch(
                        runner, model_run,
                        [{"prompt": q["question"], "options": q.get("options") or []} for q in questions],
                    )
                    asked_upfront = len(questions)
                    planning_qa = "\n".join(
                        f"Q: {q['question']}\nA: {a or '(no answer)'}" for q, a in zip(questions, answers)
                    )
            except TurnCancelled as exc:
                # User pressed Stop while the question batch was waiting → finish quietly as cancelled.
                self._finalize(chat, model_run, {**TelemetryAccumulator.initial(), **plan_usage},
                               exc, time.perf_counter() - start)
                return {}
            # A RuntimeError here means the chat/turn was deleted while waiting; let it propagate to
            # _run_turn_thread (which already no-ops when the row is gone) rather than swallowing it
            # and leaving the run stuck in RUNNING.

        # Pick the system prompt:
        #   planning → refiner (ask first; or plan-only when the questions are already done: the
        #              upfront batch ran, OR this is a refine of an existing plan);
        #   approved-plan build, prototype phase (no real Django yet) → the strict design-first
        #              workflow (prototype → preview → approve → startproject → implement → tests);
        #   approved-plan build, Django already exists (a follow-up) → the normal build assistant —
        #              do NOT redo the prototype/startproject, which would conflict with the project;
        #   otherwise → the normal build assistant.
        # An approved plan is always injected (with the live task list) in _build_task either way.
        approved_plan = (chat.settings or {}).get("plan") if (chat.settings or {}).get("plan_approved") else None
        is_refine = planning and not asked_upfront and bool((chat.settings or {}).get("plan"))
        workflow_phase = bool(approved_plan) and not self._has_real_django(sandbox)
        if planning:
            coder_system = REFINER_PLAN_SYSTEM if (asked_upfront or is_refine) else REFINER_SYSTEM
            # The composer "Planning" toggle produces a plan that drives the design-first build —
            # require the Build-workflow phases in it (NOT for the Prompt Writer: its spec also
            # seeds autonomous benchmark runs, where an approval gate would stall).
            if mode != "prompt_writer":
                coder_system += _PLAN_WORKFLOW_SECTION
        elif workflow_phase:
            coder_system = BUILD_WORKFLOW_SYSTEM
        else:
            coder_system = CHAT_SYSTEM

        # Auto-compact: before a build turn, if the running history is over the window threshold,
        # summarize the older turns into chat memory so the context stays bounded (mirrors the UI
        # meter's "auto-compact"). Skipped for planning (refiner keeps its own spec) and chat-only.
        if not planning:
            try:
                self._maybe_auto_compact(chat, backend, runner, model_run)
            except Exception as exc:  # never let compaction failure abort the turn
                self._job_log(chat, f"auto-compact skipped: {exc}")

        # DESIGN GATE: on design-first turns the router structurally blocks `finish` and design-
        # approval asks until the sandbox actually has a front-end file — a weak model can no longer
        # mark "Design prototype" done / declare DONE without writing anything. The trigger is
        # SANDBOX STATE, not "first turn": any build turn of a NEW-origin project that still has
        # neither a real Django project nor a prototype is gated — so a greeting first message, a
        # failed-and-retried first turn, or a "continue" after a give-up all stay enforced. The gate
        # opens (and stays open) the moment a user-authored .html exists, making it a no-op for
        # every later design/build turn.
        design_gate = None
        if not planning and (workflow_phase or (
                project.origin == project.Origin.NEW and not approved_plan
                and not self._has_real_django(sandbox))):
            design_gate = _frontend_gate(sandbox)
        # FINISH GATE: a build turn may not end while the app has no reachable page — the agent
        # wires its views into urls.py itself instead of leaving Django's default page plus an
        # "ask the agent to fix it" card to the user. Planning turns write no code → no gate.
        finish_gate = None if planning else _routes_gate(sandbox)

        agent = build_agent(
            llm=backend,
            use_rag=False,
            prompt_overrides={"coder_system": coder_system},
            sandbox_timeout=config["sandbox_timeout_sec"],
            enable_tools=True,
            max_tool_steps=config["max_tool_steps"],
            shell_timeout=config["shell_timeout"],
            design_gate=design_gate,
            finish_gate=finish_gate,
            interaction_handler=interaction_handler,
            # Planning mode: structurally restrict the agent to ask_user + finish so it cannot write
            # code, edit files, or run shell into the sandbox — enforced, not just asked.
            allowed_tools={"ask"} if planning else None,
            # Keep the gates ON: require_ask drives the FLOOR (force a first question), the CEILING
            # (drop questions past the cap) AND the FINISH-SPEC guard (re-prompt an empty/thin finish)
            # in RouterNode — all three live behind this flag. We never flip it off for the batch path
            # (that would silently disable the cap + spec guard); instead, when the whole batch was
            # asked up front we seed ask_count to the cap below so the CEILING drops any stray ask and
            # the agent goes straight to writing the spec.
            require_ask=planning,
            max_questions=_REFINER_MAX_QUESTIONS if planning else 0,
        )

        prior_tasks = [] if planning else _prior_tasks(chat)
        base_state = {
            "task_id": f"project_{project.id}",
            "task_title": project.name,
            "task_description": self._build_task(chat, model_run, project, answered_qa=planning_qa, refine=is_refine),
            # Carry the chat's task list into the new turn's state: the task gate then works
            # ACROSS turns, and the model resumes its checklist instead of abandoning it at 0/N.
            "tasks": prior_tasks,
            "expected_files": [],
            "reference_tests": "",
            "workspace_path": str(sandbox),
            "iteration": 0,
            "max_iterations": config["max_iterations"],
            # Seed the refiner's question count from the WHOLE conversation so the ask floor/cap are
            # chat-level, not per-turn: a follow-up ("continue") won't force fresh questions, and the
            # 6-question cap counts cumulatively across turns. When we asked the whole batch up front,
            # seed to the cap so the CEILING suppresses any further questions and the agent writes the
            # plan from the answers (no second blocking round).
            "ask_count": (_REFINER_MAX_QUESTIONS if asked_upfront
                          else self._prior_question_count(chat)) if planning else 0,
            **TelemetryAccumulator.initial(),
        }
        if prior_tasks:
            glyph = {"completed": "x", "in_progress": ">", "pending": " "}
            base_state["task_description"] += (
                "\n\nCURRENT TASK LIST (carried over from earlier turns — RESUME it, do not start a "
                "new one; resend the FULL list via update_tasks, flipping items to completed as you "
                "finish them):\n"
                + "\n".join(f"- [{glyph.get(t.get('status'), ' ')}] {t.get('content', '')}" for t in prior_tasks)
            )
        # Fold the dedicated planning-question + intent-classifier calls' tokens/latency into the
        # run telemetry (they run outside the agent graph, so _finalize would under-report).
        for usage in (plan_usage, intent_usage):
            for k, v in usage.items():
                base_state[k] = base_state.get(k, 0) + v

        final_state, exc = runner._stream(agent, dict(base_state), model_run)
        if exc is None:
            self._post_turn(chat, model_run, runner, final_state)
        self._finalize(chat, model_run, final_state, exc, time.perf_counter() - start)
        return final_state

    # ------------------------------------------------------------------ post-turn (build + summary)
    def _post_turn(self, chat: Chat, model_run: ModelRun, runner, final_state: dict) -> None:
        """After a successful turn: show the agent's final answer + build the project so the
        Preview runs and the Database tab shows any tables the agent created (automatic)."""
        project = chat.project
        summary = self._final_summary(final_state)
        if summary:
            runner._command(model_run, CommandLog.Kind.SYSTEM, "finish", command="", stdout=summary)

        # Planning produced a PLAN (the finish summary). Persist the FULL text (no 1500-char timeline
        # cap) so long plans keep every section. The standalone Prompt Writer stores it as
        # `structured_prompt` (drives the existing Create-Project/Benchmark hand-off); the composer
        # "Planning" toggle stores it as `plan` (drives the new plan modal with Implement / Edit).
        # `plan_approved` stays unset until the user clicks Implement. Planning writes no code → return.
        settings = chat.settings or {}
        if settings.get("mode") == "prompt_writer" or settings.get("planning") or settings.get("_refine_pending"):
            full = self._final_summary(final_state, max_len=0)
            s = dict(chat.settings or {})
            s.pop("_refine_pending", None)  # one-shot refine flag is consumed by this turn
            if full:
                if settings.get("mode") == "prompt_writer":
                    s["structured_prompt"] = full
                else:
                    s["plan"] = full
            Chat.objects.filter(id=chat.id).update(settings=s, updated_at=timezone.now())
            return

        # Only build when the turn actually produced source files (skip read-only/Q&A turns).
        if not final_state.get("generated_files") or not project.sandbox_path:
            return
        try:
            from . import db_inspector
            from .project_builder import ensure_runnable_project
            from .project_sandbox import SandboxRef

            ref = SandboxRef(project.sandbox_path)
            build = ensure_runnable_project(ref, force=True)
            if not build.get("ok"):
                return
            tables = db_inspector.list_tables(ref).get("tables", [])
            user_tables = [
                t for t in tables
                if not (t["name"].startswith(("django_", "auth_", "sqlite_")))
            ]
            if user_tables:
                listing = ", ".join(f"{t['name']} ({t['count']} rows)" for t in user_tables)
                runner._command(
                    model_run, CommandLog.Kind.SYSTEM, "database", command="migrate",
                    stdout=f"Database built. Tables created: {listing}. Open the Database tab to view/add data.",
                )
        except Exception as exc:  # pragma: no cover - best-effort
            self._job_log(chat, f"post-turn build failed: {exc}")

    #: Keys a JSON finish/tool-call dump may carry its human text under.
    _FINISH_TEXT_KEYS = ("spec", "summary", "plan", "answer", "message", "text", "content")

    @classmethod
    def _unwrap_tool_json(cls, text: str) -> str:
        """Unwrap a raw JSON tool-call dump into its human text.

        Weak / native-tool models sometimes emit the `finish` call AS TEXT — optionally
        ```json-fenced: ``{"name": "finish", "arguments": {"spec": "# Task Manager\\n…"}}``.
        Stored verbatim, that renders as an unreadable JSON blob (with literal \\n) in the answer
        bubble AND becomes the saved plan. Returns the inner text when the payload parses, else
        the original text unchanged."""
        t = (text or "").strip()
        fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*(?:```)?\s*$", t, re.S)
        if fence:
            t = fence.group(1).strip()
        if not t.startswith("{"):
            return text
        try:
            data = json.loads(t)
        except ValueError:
            return text
        if not isinstance(data, dict):
            return text
        args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
        if isinstance(args, str):  # arguments occasionally double-encoded
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        candidates: list[str] = []
        for source in (args if isinstance(args, dict) else {}, data):
            for key in cls._FINISH_TEXT_KEYS:
                v = source.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.append(v)
        if not candidates and isinstance(args, dict):  # any string argument beats raw JSON
            candidates = [v for v in args.values() if isinstance(v, str) and v.strip()]
        return max(candidates, key=len).strip() if candidates else text

    @classmethod
    def _final_summary(cls, final_state: dict, max_len: int = 1500) -> str:
        """The agent's last assistant message (its finish summary), cleaned of tool directives.

        ``max_len`` caps the timeline card; pass 0 to keep the full text (the hand-off spec must
        not lose its Data models / Constraints sections to truncation).
        """
        for msg in reversed(final_state.get("messages") or []):
            if msg.get("role") == "assistant":
                text = re.sub(r"(?im)^\s*DONE\s*$", "", str(msg.get("content") or "")).strip()
                if text:
                    text = cls._unwrap_tool_json(text)  # BEFORE the cap — never truncate mid-JSON
                    return text[:max_len] if max_len else text
        return ""

    def _maybe_start_preview(self, model_run) -> None:
        """Best-effort: if the sandbox has a front-end, (re)build + (re)start the preview in the
        background so the user reviews the CURRENT on-disk design when answering an approval.

        The design-first loop (build → approve? → revise → approve?) runs inside one turn, so the
        first approval starts the preview and every later one RESTARTS it — otherwise the second
        gate would serve the pre-revision app (runserver runs --noreload) and the user would
        approve a design they cannot actually see."""
        from .previews import _has_frontend

        sandbox = Path(model_run.workspace_path or "")
        if not _has_frontend(sandbox):
            return
        refresh = bool(getattr(self, "_preview_started", False))
        self._preview_started = True
        project_id = model_run.chat.project_id

        def _run():
            close_old_connections()
            try:
                from dashboard.models import Project
                from .previews import PreviewService

                project = Project.objects.filter(id=project_id).first()
                if project:
                    service = PreviewService()
                    # restart_project rebuilds + re-spawns so the revised design is served.
                    if refresh:
                        service.restart_project(project)
                    else:
                        service.start_project(project)  # builds the project + runs the server
            except Exception:
                pass
            finally:
                close_old_connections()

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------ interaction (pause/resume)
    @staticmethod
    def _abort_if_cancelled(model_run) -> None:
        """Raise ``TurnCancelled`` if the user pressed Stop (the run flipped to CANCELLED).

        Called inside the interaction poll loops: Stop only flips ``ModelRun.status`` (it deletes no
        rows and resolves no pending interactions), so without this check a turn blocked on a
        question/approval would spin until the 30-minute interaction timeout — holding the sandbox +
        Ollama connection, and (because the run is no longer RUNNING) letting a new turn start on the
        same chat concurrently. This mirrors the cancellation check ``_stream`` already does between
        agent steps."""
        from .agent_runner import TurnCancelled

        if ModelRun.objects.filter(id=model_run.id, status=RunStatus.CANCELLED).exists():
            raise TurnCancelled()

    def _handle_interaction(self, runner, model_run, kind: str, payload: dict, auto_approve: bool):
        """Create a PendingInteraction, BLOCK the agent thread until the user answers, return it.

        ``kind="question"`` → returns the user's answer text. ``kind="approval"`` → returns a bool
        (True = approved). With auto-approve on, approvals resolve instantly without blocking.
        """
        if auto_approve:
            if kind == "approval":
                return True
            # Fully hands-free builds: with Auto mode on, the DESIGN SIGN-OFF question ("Approve
            # this prototype?" with an Approve/Request-changes option) auto-approves too — it is a
            # gate, not information the agent lacks. Real clarifying questions still ask the user.
            if kind == "question" and not payload.get("questions"):
                approve = next(
                    (str(o) for o in (payload.get("options") or [])
                     if str(o).strip().lower().startswith("approve")),
                    None,
                )
                if approve is not None:
                    runner._command(
                        model_run, CommandLog.Kind.SYSTEM, "question", command="",
                        stdout=f"Q: {payload.get('prompt')}\nAuto mode: {approve}",
                    )
                    return approve

        # Planning asks several questions at once → create them as one batch and block for all.
        questions = payload.get("questions")
        if kind == "question" and questions:
            return self._handle_question_batch(runner, model_run, questions)

        prompt = str(payload.get("prompt") or "")
        options = payload.get("options") or []
        command = str(payload.get("command") or "")

        # LOOP BREAKER: a confused model can re-ask the IDENTICAL question over and over (seen
        # live: "manage.py is missing?" × 11, costing 15 minutes). After two identical answered
        # asks in this turn, repeat the previous answer instead of blocking the user again.
        if kind == "question" and prompt:
            prior = list(PendingInteraction.objects.filter(
                model_run=model_run, kind=PendingInteraction.Kind.QUESTION,
                prompt=prompt, status=PendingInteraction.Status.ANSWERED,
            ).order_by("-id")[:2])
            if len(prior) >= 2:
                answer = (prior[0].answer or "").strip()
                runner._command(
                    model_run, CommandLog.Kind.SYSTEM, "question", command="",
                    stdout=(f"Q: {prompt}\n(repeated question — reusing your previous answer): "
                            f"{answer or '(no answer)'}"),
                )
                return answer

        opts = _build_opts(options)
        pi = PendingInteraction.objects.create(
            model_run=model_run,
            kind=PendingInteraction.Kind.APPROVAL if kind == "approval" else PendingInteraction.Kind.QUESTION,
            prompt=prompt,
            options=opts,
            command=command,
            status=PendingInteraction.Status.PENDING,
        )

        # If the agent is asking the user something and there's already a front-end in the sandbox
        # (e.g. the design-approval gate), spin up the live preview so the user can review it.
        if kind == "question":
            self._maybe_start_preview(model_run)

        deadline = time.monotonic() + _INTERACTION_TIMEOUT_SEC
        while True:
            time.sleep(_INTERACTION_POLL_SEC)
            close_old_connections()
            self._abort_if_cancelled(model_run)  # user pressed Stop → abort promptly
            try:
                pi.refresh_from_db()
            except PendingInteraction.DoesNotExist:
                # The chat/turn was deleted while waiting — abort the agent.
                raise RuntimeError("interaction cancelled (chat or turn deleted)")
            if pi.status != PendingInteraction.Status.PENDING:
                break
            if time.monotonic() > deadline:
                pi.status = PendingInteraction.Status.DENIED
                pi.answer = "(timed out)"
                pi.answered_at = timezone.now()
                pi.save(update_fields=["status", "answer", "answered_at"])
                break

        # Record the resolved Q&A as a normal timeline step (the live card came from `pending`).
        if kind == "approval":
            approved = pi.status == PendingInteraction.Status.ANSWERED
            runner._command(
                model_run, CommandLog.Kind.SYSTEM, "approval", command=command,
                stdout=("Approved" if approved else "Denied") + (f": {command}" if command else ""),
            )
            return approved
        answer = (pi.answer or "").strip()
        runner._command(
            model_run, CommandLog.Kind.SYSTEM, "question", command="",
            stdout=f"Q: {prompt}\nYou: {answer or '(no answer)'}",
        )
        return answer

    def _handle_question_batch(self, runner, model_run, questions):
        """Create N pending questions AT ONCE, block until ALL are answered, return their answers
        in creation order. Powers the paginated 'plan everything up front' question card."""
        pis = []
        for q in (questions or [])[:_REFINER_MAX_QUESTIONS]:
            pis.append(PendingInteraction.objects.create(
                model_run=model_run,
                kind=PendingInteraction.Kind.QUESTION,
                prompt=str((q or {}).get("prompt") or ""),
                options=_build_opts((q or {}).get("options") or []),
                status=PendingInteraction.Status.PENDING,
            ))
        ids = [p.id for p in pis]
        self._maybe_start_preview(model_run)

        deadline = time.monotonic() + _INTERACTION_TIMEOUT_SEC
        while True:
            time.sleep(_INTERACTION_POLL_SEC)
            close_old_connections()
            self._abort_if_cancelled(model_run)  # user pressed Stop → abort promptly
            fresh = list(PendingInteraction.objects.filter(id__in=ids))
            if len(fresh) < len(ids):  # a row vanished → chat/turn deleted; abort the agent
                raise RuntimeError("interaction cancelled (chat or turn deleted)")
            if all(p.status != PendingInteraction.Status.PENDING for p in fresh):
                break
            if time.monotonic() > deadline:
                PendingInteraction.objects.filter(id__in=ids, status=PendingInteraction.Status.PENDING).update(
                    status=PendingInteraction.Status.DENIED, answer="(timed out)", answered_at=timezone.now())
                break

        by_id = {p.id: p for p in PendingInteraction.objects.filter(id__in=ids)}
        answers, qa = [], []
        for p in pis:
            row = by_id.get(p.id)
            a = (row.answer or "").strip() if row else ""
            answers.append(a)
            qa.append(f"Q: {p.prompt}\nYou: {a or '(no answer)'}")
        runner._command(model_run, CommandLog.Kind.SYSTEM, "question", command="", stdout="\n\n".join(qa))
        return answers

    def _plan_questions(self, backend, chat: Chat, model_run: ModelRun) -> tuple[list[dict], dict]:
        """Planning's first round: ONE LLM call that lists EVERY clarifying question at once.

        Weak local models emit a single ask per ReAct step, so relying on the agent loop makes the
        refiner dribble questions out one at a time (ask → think → ask → …). Asking the model to
        LIST all of its questions in a single completion reliably yields the whole set, which the
        caller then presents as one paginated batch.

        Returns ``(questions, usage)`` where ``questions`` is ``[{"question", "options"}]`` (empty →
        the caller falls back to the agent's own ask loop) and ``usage`` carries this call's
        token/latency totals so the turn telemetry counts it (the call runs outside the agent graph).
        """
        from src.agent.tools import _parse_ask

        history = [h for h in ModelRun.objects.filter(chat=chat).exclude(id=model_run.id)
                   .order_by("id").values_list("user_prompt", flat=True) if (h or "").strip()][-4:]
        idea = []
        prior_spec = (chat.settings or {}).get("structured_prompt") or (chat.settings or {}).get("plan")
        if prior_spec:
            idea.append("Current working spec:\n" + prior_spec)
        if history:
            idea.append("Earlier the user said:\n" + "\n".join(f"- {h}" for h in history))
        idea.append("The user's request:\n" + (model_run.user_prompt or "").strip())

        prompt = (
            "You are about to plan a software build. BEFORE any work, list EVERY clarifying question "
            "you need answered — about 3-5, AT MOST 6 — covering the genuinely unclear, "
            "BUILD-CRITICAL decisions (who owns/manages the data, auth & roles, the core workflow, "
            "ambiguous field meanings). Group related decisions into ONE question; never ask a "
            "separate question per field. Output EVERY question NOW, each in EXACTLY this shape:\n\n"
            "ASK: <the question>\n"
            "- First concrete choice (Recommended) — one-line description\n"
            "- Second concrete choice — one-line description\n"
            "- Third concrete choice — one-line description\n\n"
            "Every option line must START with the actual choice text (2-6 words), e.g. "
            "'- Admins only (Recommended) — staff manage all recipes'. NEVER write a placeholder "
            "like 'Option A' and NEVER use markdown (**bold**, backticks) — plain text only. "
            "Give each question 2-4 options, mark the single best default '(Recommended)', and add a "
            "one-line ' — description' after every option. Output ONLY the ASK blocks — no preamble, "
            "no code, no closing remarks.\n\n" + "\n\n".join(idea)
        )
        try:
            resp = backend.complete(
                prompt,
                system="You ask sharp, non-redundant, build-critical clarifying questions.",
                temperature=0.2,
                max_tokens=900,
            )
        except Exception:
            return [], {}
        usage = {
            "total_prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
            "total_completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
            "total_latency_sec": float(getattr(resp, "latency_sec", 0.0) or 0.0),
            "llm_calls": 1,
        }
        return _parse_ask(resp.text or "")[:_REFINER_MAX_QUESTIONS], usage

    @staticmethod
    def _ensure_venv_async(runner, model_run: ModelRun, sandbox) -> None:
        """Provision the project venv in a background daemon thread, silently.

        Success emits NO timeline cards (the user's visible flow should start with the agent's
        tasks/design, not environment plumbing); only a failure surfaces as a `venv` card so the
        user knows why later python steps fall back to the system interpreter."""
        from .project_venv import ensure_venv

        def _run():
            close_old_connections()
            try:
                res = ensure_venv(sandbox)
                if isinstance(res, dict) and not res.get("ok", True):
                    runner._command(
                        model_run, CommandLog.Kind.SYSTEM, "venv", command="",
                        stdout="Could not prepare the project environment: "
                               + str(res.get("error") or "unknown error")[:300],
                    )
            except Exception:
                pass  # best-effort: every consumer falls back to sys.executable
            finally:
                close_old_connections()

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------ conversational turns
    @staticmethod
    def _usage_of(resp) -> dict:
        """Token/latency telemetry of one out-of-graph LLM call, in TelemetryAccumulator keys."""
        return {
            "total_prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
            "total_completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
            "total_latency_sec": float(getattr(resp, "latency_sec", 0.0) or 0.0),
            "llm_calls": 1,
        }

    def _classify_intent(self, backend, chat: Chat, model_run: ModelRun) -> tuple[bool, dict]:
        """One quick LLM call: is the message just CHAT, or a BUILD request?

        CHAT = a greeting, small talk, or a general question answerable WITHOUT touching the
        project's files. Anything that creates/changes/runs/debugs the project is BUILD. Returns
        ``(is_chat, usage)`` and errs toward BUILD on any error or ambiguity, so a real build
        request can never be lost to a chat-only answer.
        """
        history = [h for h in ModelRun.objects.filter(chat=chat).exclude(id=model_run.id)
                   .order_by("id").values_list("user_prompt", flat=True) if (h or "").strip()][-3:]
        ctx = ("Earlier messages from the user:\n" + "\n".join(f"- {h}" for h in history) + "\n\n") if history else ""
        prompt = (
            ctx + "The user's new message:\n" + (model_run.user_prompt or "").strip() + "\n\n"
            "Classify the NEW message. Answer BUILD if it asks to create, modify, fix, run, test, "
            "install or inspect anything in the software project (including follow-ups like 'make "
            "the header blue' or 'add login'). Answer CHAT only if it is a greeting, small talk, or "
            "a general question that can be answered without touching the project's files or code. "
            "If unsure, answer BUILD.\n"
            "Reply with exactly one word: BUILD or CHAT."
        )
        try:
            resp = backend.complete(prompt, system="You classify intent precisely.", temperature=0.0, max_tokens=6)
        except Exception:
            return False, {}
        text = (resp.text or "").strip().upper()
        return ("CHAT" in text and "BUILD" not in text), self._usage_of(resp)

    def _chat_reply(self, backend, chat: Chat, model_run: ModelRun) -> tuple[str, dict]:
        """Direct conversational answer for a chat-only turn (no agent loop, no files)."""
        logs = list(
            CommandLog.objects.filter(model_run__chat=chat, agent__in=["user", "finish"])
            .exclude(model_run=model_run).order_by("-id")[:8]
        )[::-1]
        convo = "\n\n".join(
            ("User: " if log.agent == "user" else "Assistant: ") + (log.stdout or "").strip()
            for log in logs if (log.stdout or "").strip()
        )
        prompt = ((convo + "\n\n") if convo else "") + "User: " + (model_run.user_prompt or "").strip()
        resp = backend.complete(
            prompt,
            system=(
                "You are the assistant inside the user's web-project workspace. The user is just "
                "chatting (greeting / general question) — answer helpfully and concisely in markdown. "
                "Do NOT pretend to have built or changed anything, and do NOT ask clarifying "
                "questions unless the message itself requires one. If they actually want something "
                "built or changed, briefly say they can just describe it and you will build it."
            ),
            temperature=0.4,
            max_tokens=700,
        )
        reply = (resp.text or "").strip() or "I'm here — tell me what you'd like to build or change."
        return reply, self._usage_of(resp)

    # ------------------------------------------------------------------ helpers
    def _config(self, chat: Chat) -> dict:
        from config import settings as cfg

        s = chat.settings or {}
        effort = str(s.get("effort", "medium")).lower()
        return {
            "max_tool_steps": int(s.get("max_tool_steps") or _EFFORT_STEPS.get(effort, 30)),
            "max_iterations": int(s.get("max_iterations") or cfg.agent_max_iterations),
            "sandbox_timeout_sec": int(cfg.autonomous_sandbox_timeout_sec),
            "shell_timeout": int(cfg.agent_shell_timeout_sec),
        }

    def _resolve_tag(self, chat: Chat) -> str | None:
        """Resolve the ollama model tag from the chat's chosen AIModel (or fall back)."""
        from dashboard.models import AIModel
        from src.llm.OllamaBackend import OllamaBackend
        from .agent_runner import extract_ollama_model

        # A raw Ollama tag chosen via the retry bar ("change model") wins — it points straight at an
        # installed model (e.g. codellama:latest) the user picked because the default didn't fit.
        direct = ((chat.settings or {}).get("ollama_tag") or "").strip()
        if direct:
            return direct

        model_ref = (chat.settings or {}).get("model")
        if model_ref:
            try:
                ai = AIModel.objects.filter(id=int(model_ref)).first() if str(model_ref).isdigit() else \
                    AIModel.objects.filter(name=model_ref).first()
            except (ValueError, TypeError):
                ai = None
            if ai:
                tag = extract_ollama_model(ai.execution_command or "")
                if tag:
                    return tag
        # AUTO: always the BEST installed model (preferred coder family, largest size) — not the
        # static env default, which may be a small tag while a bigger coder model is available.
        # getattr-guarded: tests swap OllamaBackend for fakes without best_installed (→ old Auto).
        best = getattr(OllamaBackend, "best_installed", None)
        try:
            return (best() if callable(best) else "") or None
        except Exception:
            return None

    @staticmethod
    def _prior_question_count(chat: Chat) -> int:
        """How many clarifying questions the refiner already asked in this chat (across all turns)."""
        return PendingInteraction.objects.filter(
            model_run__chat=chat, kind=PendingInteraction.Kind.QUESTION,
        ).count()

    @staticmethod
    def _has_real_django(sandbox) -> bool:
        """True once a REAL ``django-admin startproject`` has run in the sandbox (a `manage.py` that
        our scaffolder did NOT author — i.e. no ``.kursinis_scaffolded`` marker). Used to stop the
        plan-build workflow from redoing the prototype/startproject on follow-up turns."""
        ws = Path(sandbox or "")
        return (ws / "manage.py").exists() and not (ws / ".kursinis_scaffolded").exists()

    @staticmethod
    def _latest_tasks(chat: Chat) -> list:
        """The current agent task list = the newest ``agent="tasks"`` CommandLog's JSON payload (the
        model resends the FULL list each update, so latest is authoritative). Mirrors views._latest_tasks."""
        import json

        log = CommandLog.objects.filter(model_run__chat=chat, agent="tasks").order_by("-id").first()
        if not log or not log.stdout:
            return []
        try:
            data = json.loads(log.stdout)
        except (ValueError, TypeError):
            return []
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ context window + memory
    #: Rough chars-per-token for the meter (English code/prose ≈ 4). Good enough for a budget gauge.
    _CHARS_PER_TOKEN = 4
    #: Fixed overhead (system prompt + task scaffolding) that rides every turn, in chars.
    _CONTEXT_BASE_CHARS = 4000

    @staticmethod
    def _context_window(chat: Chat) -> int:
        from config import settings as cfg

        try:
            override = int((chat.settings or {}).get("context_window") or 0)
        except (TypeError, ValueError):
            override = 0
        return override or int(getattr(cfg, "chat_context_window_tokens", 8000))

    @classmethod
    def _history_footprint_chars(cls, chat: Chat) -> int:
        """Chars of what the NEXT turn re-sends: memory + post-boundary user prompts + finish answers."""
        s = chat.settings or {}
        boundary = int(s.get("compacted_through_run_id", 0) or 0)
        total = len(s.get("memory") or "")
        runs = ModelRun.objects.filter(chat=chat, id__gt=boundary).order_by("id")
        for r in runs:
            total += len(r.user_prompt or "")
        finishes = CommandLog.objects.filter(
            model_run__chat=chat, model_run__id__gt=boundary, agent="finish",
        ).values_list("stdout", flat=True)
        for f in finishes:
            total += len(f or "")
        return total

    @classmethod
    def estimate_context(cls, chat: Chat) -> dict:
        """Context-meter payload: used/window tokens + remaining %, mirrored to chat_logs_api."""
        window = cls._context_window(chat)
        used = (cls._history_footprint_chars(chat) + cls._CONTEXT_BASE_CHARS) // cls._CHARS_PER_TOKEN
        used = max(0, used)
        remaining_pct = 100 if window <= 0 else max(0, min(100, round((1 - used / window) * 100)))
        from config import settings as cfg

        auto_at = float(getattr(cfg, "chat_auto_compact_at", 0.8))
        return {
            "used_tokens": used,
            "window_tokens": window,
            "remaining_pct": remaining_pct,
            "auto_compact_pct": max(0, round((1 - auto_at) * 100)),  # remaining-% at which auto-compact fires
            "should_compact": window > 0 and used >= auto_at * window,
            "compacting": bool((chat.settings or {}).get("_compacting")),
            "has_memory": bool((chat.settings or {}).get("memory")),
        }

    def _maybe_auto_compact(self, chat: Chat, backend, runner, model_run: ModelRun) -> None:
        """If the history is over the auto-compact threshold, compact it into memory before the turn."""
        if not self.estimate_context(chat).get("should_compact"):
            return
        ok = self.compact_chat(chat, backend)
        if ok:
            runner._command(
                model_run, CommandLog.Kind.SYSTEM, "memory", command="",
                stdout="Context was getting full — summarized the earlier conversation into memory "
                       "to free up room. The build continues with that summary in mind.",
            )

    def compact_chat(self, chat: Chat, backend=None) -> bool:
        """Summarize the post-boundary conversation (+ any existing memory) into a fresh memory blob
        and advance the compaction boundary to the latest turn. Returns True on success."""
        s = dict(chat.settings or {})
        boundary = int(s.get("compacted_through_run_id", 0) or 0)
        runs = list(ModelRun.objects.filter(chat=chat, id__gt=boundary).order_by("id"))
        if not runs:
            return False
        latest_id = runs[-1].id

        transcript = []
        if s.get("memory"):
            transcript.append("Existing summary:\n" + s["memory"])
        finish_by_run = {}
        for log in CommandLog.objects.filter(
            model_run__chat=chat, model_run__id__gt=boundary, agent="finish",
        ).order_by("id").values("model_run_id", "stdout"):
            finish_by_run[log["model_run_id"]] = log["stdout"]
        for r in runs:
            if (r.user_prompt or "").strip():
                transcript.append("User: " + r.user_prompt.strip())
            ans = (finish_by_run.get(r.id) or "").strip()
            if ans:
                transcript.append("Assistant: " + ans[:1200])
        convo = "\n\n".join(transcript)[:8000]

        if backend is None:
            from src.llm.OllamaBackend import OllamaBackend

            backend = OllamaBackend(model=self._resolve_tag(chat))
        prompt = (
            "Summarize this assistant/user software-building conversation into a COMPACT memory the "
            "assistant can use to continue the work with full continuity. Keep: the project goal, key "
            "decisions, the data model / features agreed, the current build state, and any open "
            "to-dos. Drop chit-chat. Use terse bullet points, max ~250 words.\n\n" + convo
        )
        try:
            resp = backend.complete(prompt, system="You write concise, faithful project memory.",
                                    temperature=0.2, max_tokens=600)
            memory = (resp.text or "").strip()
        except Exception:
            return False
        if not memory:
            return False

        # Re-read settings under the lock so we don't clobber a concurrent settings write.
        fresh = Chat.objects.filter(id=chat.id).values_list("settings", flat=True).first() or {}
        fresh = dict(fresh)
        fresh["memory"] = memory[:6000]
        fresh["compacted_through_run_id"] = latest_id
        fresh.pop("_compacting", None)
        Chat.objects.filter(id=chat.id).update(settings=fresh, updated_at=timezone.now())
        chat.settings = fresh
        return True

    def _build_task(self, chat: Chat, model_run: ModelRun, project, answered_qa: str = "",
                    refine: bool = False) -> str:
        """Compose the agent's task: prior turns (context) + the current request.

        ``answered_qa`` is set when planning already asked its whole question batch up front (see
        ``run_turn``); it carries the user's answers so the agent writes the plan instead of asking
        again. ``refine`` is set for a "Refine with AI" turn — the interview is done, so the model
        should fold the user's edits into a revised plan rather than re-interviewing."""
        # History begins AFTER the last compaction boundary — older turns live in `memory` instead.
        boundary = int((chat.settings or {}).get("compacted_through_run_id", 0) or 0)
        history = list(
            ModelRun.objects.filter(chat=chat, id__gt=boundary).exclude(id=model_run.id)
            .order_by("id").values_list("user_prompt", flat=True)
        )
        history = [h for h in history if (h or "").strip()][-4:]
        current = (model_run.user_prompt or "").strip()
        memory = (chat.settings or {}).get("memory") or ""

        settings = chat.settings or {}
        if settings.get("mode") == "prompt_writer" or settings.get("planning") or settings.get("_refine_pending"):
            # Refine the EXISTING idea/spec across turns — don't reset to the latest fragment.
            if answered_qa:
                parts = [
                    "Turn this idea into a structured, buildable prompt. You have ALREADY asked the "
                    "user your clarifying questions and they answered below — do NOT ask anything "
                    "else. Use the answers to WRITE the final structured prompt now (title, overview, "
                    "features, data models, constraints). Write it yourself; never ask the user to "
                    "write it."
                ]
            elif refine:
                parts = [
                    "Revise the plan below using the user's edits/feedback. The interview is OVER — do "
                    "NOT ask any questions. Produce the full UPDATED plan now (title, overview, "
                    "features, data models, constraints), keeping everything still valid and applying "
                    "the requested changes."
                ]
            else:
                parts = [
                    "Interview the user to turn this idea into a structured, buildable prompt. Your "
                    "FIRST action MUST be to ASK a clarifying question (with A/B/C/D options) about "
                    "the unclear, build-critical decisions — do not summarise or finish yet. Keep "
                    "asking until the idea is clear, then finish with the structured prompt that YOU "
                    "write (never ask the user to write it)."
                ]
            # Planning-toggle plans drive the design-first build → the plan must spell the phases out.
            if settings.get("mode") != "prompt_writer":
                parts.append(
                    "End the plan with a '**Build workflow:**' section listing the phases: "
                    "1) front-end design prototype (plain HTML/CSS/JS, shown live in the Preview tab), "
                    "2) user approval of the design (revise until approved), "
                    "3) Django implementation that integrates the approved design (models, views, "
                    "forms, urls, admin, migrations), 4) tests & verification."
                )
            prior_spec = (chat.settings or {}).get("structured_prompt") or (chat.settings or {}).get("plan")
            if prior_spec:
                parts.append("The current working spec is:\n" + prior_spec)
            if history:
                parts.append("Earlier the user said:\n" + "\n".join(f"- {h}" for h in history))
            parts.append(("Refinement:\n" if (prior_spec or history) else "Idea:\n") + current)
            if answered_qa:
                parts.append("The user's answers to your questions:\n" + answered_qa)
            return "\n\n".join(parts)

        parts = []
        # Compacted older conversation (memory) is prepended so the agent keeps continuity even
        # after the raw turns were summarized away to stay within the context window.
        if memory:
            parts.append("Summary of the conversation so far (compacted memory):\n" + memory)
        # An APPROVED plan ("Implement this plan") drives the build: inject the plan + the live task
        # list every turn so the agent always follows them. BUILD_WORKFLOW_SYSTEM owns the ordering,
        # so the one-shot _DESIGN_FIRST block is skipped here.
        s = chat.settings or {}
        approved_plan = s.get("plan") if s.get("plan_approved") else None
        if approved_plan:
            parts.append("You are implementing this APPROVED PLAN — follow it exactly:\n\n" + approved_plan)
            tasks = self._latest_tasks(chat)
            if tasks:
                parts.append(
                    "The active task list (keep it current with update_tasks as you work):\n"
                    + "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in tasks)
                )
        if project.origin == project.Origin.EXISTING:
            parts.append(
                "This is an EXISTING project already present in the sandbox — inspect it "
                "(list_files / read_file / grep) before making changes."
            )
        # Design-first guidance for a brand-new project that has NOT produced its prototype yet —
        # keyed on SANDBOX STATE (no front-end, no real Django), not "first turn", so a greeting
        # first message or a failed-and-retried first turn keeps the workflow. Never under an
        # approved plan, whose workflow prompt already enforces prototype → approve → startproject.
        if project.origin == project.Origin.NEW and not approved_plan:
            from .previews import _has_frontend

            ws = Path(project.sandbox_path or "")
            if not _has_frontend(ws) and not self._has_real_django(ws):
                parts.append(_DESIGN_FIRST)
        if history:
            parts.append("Earlier in this conversation the user asked:\n" + "\n".join(f"- {h}" for h in history))
        parts.append("Current request:\n" + current)
        s = chat.settings or {}
        if s.get("thinking"):
            parts.append("Think it through: make a brief plan before acting.")
        if s.get("pursue_goal"):
            parts.append("Pursue the goal: after your changes, verify the project still works "
                         "(e.g. `python manage.py check`) and fix any problems before finishing.")
        return "\n\n".join(parts)

    def _finalize(self, chat: Chat, model_run: ModelRun, final_state: dict, exc, wall: float) -> None:
        from .file_index import index_generated_files

        try:
            index_generated_files(model_run)
        except Exception:
            pass
        latency = float(final_state.get("total_latency_sec", 0.0) or 0.0)
        completion = int(final_state.get("total_completion_tokens", 0) or 0)
        model_run.prompt_tokens = int(final_state.get("total_prompt_tokens", 0) or 0)
        model_run.completion_tokens = completion
        model_run.tokens_per_second = (completion / latency) if latency else None
        model_run.generation_duration_seconds = latency
        model_run.save(update_fields=[
            "prompt_tokens", "completion_tokens", "tokens_per_second",
            "generation_duration_seconds", "updated_at",
        ])
        if exc is not None:
            from .agent_runner import TurnCancelled

            if isinstance(exc, TurnCancelled):
                # User pressed Stop — finish quietly as cancelled, not failed.
                try:
                    CommandLog.objects.create(
                        model_run=model_run, kind=CommandLog.Kind.SYSTEM, agent="system",
                        command="", stdout="Stopped by user.",
                    )
                except Exception:
                    pass
                model_run.mark_finished(RunStatus.CANCELLED, -1, "Stopped by user")
                Chat.objects.filter(id=chat.id).update(status=RunStatus.CANCELLED, updated_at=timezone.now())
                return
            self._job_log(chat, f"Chat turn crashed: {exc}")
            # Surface the reason in the timeline — otherwise the user just sees a bare "Failed".
            try:
                CommandLog.objects.create(
                    model_run=model_run, kind=CommandLog.Kind.SYSTEM, agent="error",
                    command="", stdout=self._explain_error(exc)[:1000], exit_code=-1,
                )
            except Exception:
                pass
            model_run.mark_finished(RunStatus.FAILED, -1, str(exc)[:500])
            Chat.objects.filter(id=chat.id).update(status=RunStatus.FAILED, updated_at=timezone.now())
            return
        self._reconcile_tasks(chat, model_run, final_state)
        model_run.mark_finished(RunStatus.SUCCEEDED, 0, "")
        Chat.objects.filter(id=chat.id).update(status=RunStatus.SUCCEEDED, updated_at=timezone.now())
        self._emit_page_links(chat, model_run)

    def _reconcile_tasks(self, chat: Chat, model_run: ModelRun, final_state: dict) -> None:
        """After a successful build turn, verify the task list against REALITY with one LLM call.

        Weak models do the work but forget to flip statuses (the chip ends at 2/6). The router's
        task gate already nudges once; this is the safety net: given the files that now exist and
        the model's own finish summary, ask which tasks are truly complete, flip ONLY those, and
        publish the corrected list as the authoritative `tasks` card. Never un-completes anything;
        any failure (no backend, bad parse) silently keeps the model's own list."""
        try:
            tasks = [dict(t) for t in (final_state.get("tasks") or []) if isinstance(t, dict)]
            unfinished = [t for t in tasks if (t.get("status") or "") != "completed"]
            if not tasks or not unfinished or (chat.settings or {}).get("planning"):
                return
            sandbox = Path(chat.project.sandbox_path or "")
            skip = {".venv", "venv", "node_modules", "__pycache__", ".git", "staticfiles"}
            files = []
            if sandbox.exists():
                for f in sorted(sandbox.rglob("*")):
                    if not f.is_file() or f.name.startswith(".") or any(p in skip for p in f.parts):
                        continue
                    files.append(f.relative_to(sandbox).as_posix())
                    if len(files) >= 80:
                        break
            finish = CommandLog.objects.filter(model_run=model_run, agent="finish").order_by("-id").first()
            task_lines = "\n".join(
                f"{i + 1}. [{t.get('status', 'pending')}] {t.get('content', '')}" for i, t in enumerate(tasks)
            )
            prompt = (
                "A software build just finished. Decide which TASKS are ACTUALLY complete, using the "
                "evidence below.\n\nTasks:\n" + task_lines
                + "\n\nFiles that now exist in the project:\n" + ("\n".join(files) or "(none)")
                + "\n\nBuild finish summary:\n" + ((finish.stdout if finish else "") or "(none)")[:1500]
                + "\n\nReply with EXACTLY one line — the numbers of the tasks that are truly "
                  "complete:\nCOMPLETED: 1, 2, 3\nInclude a number ONLY if the files/summary show "
                  "that task was done. No other text."
            )
            from src.llm.OllamaBackend import OllamaBackend

            resp = OllamaBackend(model=self._resolve_tag(chat)).complete(
                prompt, system="You audit task lists against evidence. Be strict and terse.",
                temperature=0.0, max_tokens=120,
            )
            m = re.search(r"COMPLETED:\s*([0-9,\s]+)", resp.text or "")
            if not m:
                return
            picked = {int(n) for n in re.findall(r"\d+", m.group(1))}
            changed = False
            for i, t in enumerate(tasks, start=1):
                if i in picked and t.get("status") != "completed":
                    t["status"] = "completed"
                    changed = True
            if not changed:
                return
            done = sum(1 for t in tasks if t.get("status") == "completed")
            CommandLog.objects.create(
                model_run=model_run, kind=CommandLog.Kind.SYSTEM, agent="tasks",
                command=f"{done}/{len(tasks)} done", stdout=json.dumps(tasks),
            )
        except Exception:
            pass  # best-effort: reconciliation must never break a successful finish

    @staticmethod
    def _emit_page_links(chat: Chat, model_run: ModelRun) -> None:
        """After a successful build turn, record WHICH pages the app now answers on — and warn
        when none are wired (that is exactly when the Preview shows Django's default page)."""
        try:
            sandbox = (chat.project.sandbox_path or "") if chat.project_id else ""
            if not sandbox:
                return
            if (chat.settings or {}).get("planning"):
                return  # planning writes no code — nothing new to link
            from pathlib import Path as _Path

            from .route_discovery import refresh_routes

            has_django = (_Path(sandbox) / "manage.py").exists()
            routes = refresh_routes(sandbox)
            if routes:
                lines = "\n".join(
                    f"  {r['path']}" + (f"  — {r['name']}" if r.get("name") else "") for r in routes
                )
                stdout = "Pages your app answers on:\n" + lines + "\n\nOpen them in the Preview tab."
            elif has_django:
                stdout = (
                    "No app pages are wired to a URL yet — only admin/default routes exist, so the "
                    "Preview shows Django's default page. Ask the agent to hook the views into urls.py."
                )
            else:
                return  # nothing servable (e.g. a prompt-writer chat) — no card
            CommandLog.objects.create(
                model_run=model_run, kind=CommandLog.Kind.SYSTEM, agent="links",
                command="", stdout=stdout,
            )
        except Exception:
            pass

    @staticmethod
    def _explain_error(exc) -> str:
        """A short, user-readable reason for a failed turn (with a hint for common causes)."""
        msg = str(exc).strip() or exc.__class__.__name__
        low = msg.lower()
        if "not found" in low and ("model" in low or "pull" in low):
            return (f"The agent stopped: {msg}\n\nThe selected Ollama model isn't installed. "
                    "Pull it (e.g. `ollama pull qwen2.5-coder:7b-instruct`) or pick an installed "
                    "model in the chat's Model selector, then try again.")
        if "connection" in low or "refused" in low or "max retries" in low:
            return (f"The agent stopped: {msg}\n\nCould not reach Ollama — make sure it's running "
                    "(`ollama serve`) and reachable at the configured host.")
        return f"The agent stopped: {msg}"

    def _job_log(self, chat: Chat, message: str) -> None:
        # Project chats are not tied to a Job; route agent log lines to the most recent Job if
        # one exists, else swallow (the chat timeline is the user-facing surface anyway).
        try:
            job = getattr(getattr(chat, "project", None), "job", None)
            if job is not None:
                JobLog.objects.create(job=job, stream="stdout", text=message)
        except Exception:
            pass
