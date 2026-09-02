"""Server-rendered dashboard views for the benchmark platform."""
from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AIModelForm
from .models import AIModel, Chat, CommandLog, ModelRun, PendingInteraction, PreviewServer, Project, RunStatus
from .services.file_index import read_generated_file
from .services.previews import PreviewService


def dashboard_home(request: HttpRequest):
    """The home composer ("New chat") — start a new project. Only needs the active models picker."""
    models = AIModel.objects.filter(is_active=True).order_by("execution_order", "name")
    return render(request, "dashboard/home.html", {"models": models})


def _safe_next(request: HttpRequest) -> str | None:
    """The ``?next=`` target from the sign-in gate — only when it points back at this site.

    Guards against an open redirect: a hand-crafted ``?next=https://evil.example`` must not survive
    a successful sign-in / sign-up."""
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return None


@require_http_methods(["GET", "POST"])
def signin(request: HttpRequest):
    """Username/password sign-in (the dashboard is otherwise open — this just drives the avatar)."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.forms import AuthenticationForm

    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    form.fields["username"].label = "El. paštas"  # the username IS the email (set at sign-up)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.get_user())
        return redirect(_safe_next(request) or "dashboard")
    return render(request, "dashboard/auth.html", {"form": form, "mode": "signin"})


@require_http_methods(["GET", "POST"])
def signup(request: HttpRequest):
    """Create an account (name → email → password), then sign straight in."""
    from django.contrib.auth import login as auth_login

    from .forms import SignupForm

    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.save())
        # ?next= comes from the sign-in gate popup — land back on the page that raised it.
        return redirect(_safe_next(request) or "dashboard")
    return render(request, "dashboard/signup.html", {"form": form})


def signup_email_check(request: HttpRequest):
    """Is this email already registered? Lets the sign-up wizard say so on the email step instead of
    letting the visitor fill in two more steps and bounce off the final submit.

    Discloses no more than the form itself does (``SignupForm.clean_email`` reports the same thing)."""
    email = (request.GET.get("email") or "").strip().lower()
    User = get_user_model()
    taken = bool(email) and (
        User.objects.filter(username__iexact=email).exists()
        or User.objects.filter(email__iexact=email).exists()
    )
    return JsonResponse({"taken": taken})


@require_POST
def signout(request: HttpRequest):
    """Sign out (POST only)."""
    from django.contrib.auth import logout as auth_logout

    auth_logout(request)
    return redirect("dashboard")


def settings_page(request: HttpRequest):
    """Account surface (Profile / Preferences / Help) opened from the sidebar profile menu.

    The active tab is driven by ``?tab=`` so the menu's deep-links work without JS; settings.js
    adds instant client-side switching on top."""
    if not request.user.is_authenticated:
        return redirect("dashboard-signin")
    tab = request.GET.get("tab", "profile")
    if tab not in {"profile", "preferences", "help"}:
        tab = "profile"
    return render(request, "dashboard/settings.html", {"active_tab": tab})


def model_list(request: HttpRequest):
    """The Models page: browse/install Ollama models (Available + Installed). The benchmark
    AIModel registry is managed via Django admin / the model_* CRUD views, not this page."""
    return render(request, "dashboard/model_list.html", {})


@require_http_methods(["GET", "POST"])
def model_create(request: HttpRequest):
    """Create an AI model."""
    form = AIModelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        model = form.save()
        return redirect("dashboard-model-edit", pk=model.pk)
    return render(request, "dashboard/model_form.html", {"form": form, "model": None})


@require_http_methods(["GET", "POST"])
def model_edit(request: HttpRequest, pk: int):
    """Edit an AI model."""
    model = get_object_or_404(AIModel, pk=pk)
    form = AIModelForm(request.POST or None, instance=model)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("dashboard-models")
    return render(request, "dashboard/model_form.html", {"form": form, "model": model})


@require_POST
def model_delete(request: HttpRequest, pk: int):
    """Delete an AI model configuration."""
    model = get_object_or_404(AIModel, pk=pk)
    model.delete()
    return redirect("dashboard-models")


# --------------------------------------------------- Ollama library browser (Models page)
def ollama_library_api(request: HttpRequest) -> JsonResponse:
    """All models on ollama.com/library (cached scrape; ``?refresh=1`` forces a re-fetch).
    Each entry is annotated with whether any tag of it is installed locally."""
    from .services import ollama_library, ollama_models

    data = ollama_library.list_library(refresh=request.GET.get("refresh") == "1")
    try:
        installed = ollama_models.installed_models()
    except Exception:
        installed = set()
    bases = {t.split(":", 1)[0] for t in installed}
    for m in data["models"]:
        m["installed"] = m["name"] in bases
    return JsonResponse(data)


def _annotate_ollama_detail(name: str, refresh: bool = False) -> dict:
    """Fetch a library model's detail and annotate each tag with installed/pulling state.

    Returns the ``{"detail": …, "error": …}`` shape from ``ollama_library.model_detail``, with a
    ``full_tag``/``installed``/``pulling`` field added to every tag. Shared by the JSON API and the
    server-rendered detail page so the annotation logic lives in one place."""
    from .services import ollama_library, ollama_models

    data = ollama_library.model_detail(name, refresh=refresh)
    detail = data.get("detail")
    if detail:
        try:
            installed = ollama_models.installed_models()
        except Exception:
            installed = set()
        pulls = ollama_models.pull_states()
        for t in detail.get("tags") or []:
            full = t["tag"] if ":" in t["tag"] else f"{name}:{t['tag']}"
            t["full_tag"] = full
            t["installed"] = full in installed
            t["pulling"] = pulls.get(full, {}).get("status") == "pulling"
    return data


def ollama_model_detail_api(request: HttpRequest, name: str) -> JsonResponse:
    """JSON detail for one library model (programmatic; the UI uses the page below)."""
    return JsonResponse(_annotate_ollama_detail(name, refresh=request.GET.get("refresh") == "1"))


def ollama_model_page(request: HttpRequest, name: str):
    """Server-rendered detail page for one Ollama library model: README + images + pullable tags
    with an Install control. Reached from the Available grid and from each Installed model."""
    from django.http import Http404

    from .services import ollama_library

    if not ollama_library.MODEL_NAME_RE.match(name or ""):
        raise Http404("Unknown model")
    data = _annotate_ollama_detail(name)
    return render(request, "dashboard/model_detail.html", {
        "model_name": name,
        "detail": data.get("detail"),
        "error": data.get("error"),
    })


def ollama_installed_api(request: HttpRequest) -> JsonResponse:
    """Locally installed models (tag, size, modified) + any in-flight pulls."""
    from .services import ollama_models

    try:
        models = ollama_models.installed_models_detailed()
        error = None
    except Exception as exc:
        models, error = [], f"Could not list installed models: {exc}"
    return JsonResponse({"models": models, "pulls": ollama_models.pull_states(), "error": error})


@require_POST
def ollama_install_api(request: HttpRequest) -> JsonResponse:
    """Start downloading a model tag in the background (progress via the installed/status poll)."""
    from .services import ollama_library, ollama_models

    tag = (request.POST.get("tag") or "").strip()
    if not ollama_library.MODEL_TAG_RE.match(tag):
        return JsonResponse({"ok": False, "error": "Invalid model tag."}, status=400)
    started, message = ollama_models.start_background_pull(tag)
    return JsonResponse({"ok": started, "message": message}, status=200 if started else 409)


@require_POST
def ollama_remove_api(request: HttpRequest) -> JsonResponse:
    """Remove an installed model (``ollama rm``)."""
    from .services import ollama_library, ollama_models

    tag = (request.POST.get("tag") or "").strip()
    if not ollama_library.MODEL_TAG_RE.match(tag):
        return JsonResponse({"ok": False, "error": "Invalid model tag."}, status=400)
    ok, message = ollama_models.remove_model(tag)
    return JsonResponse({"ok": ok, "message": message}, status=200 if ok else 502)



def _after_id(request: HttpRequest) -> int | None:
    """Parse the ``?after=`` poll cursor as an int (None when absent/invalid) — never 500 on junk."""
    raw = request.GET.get("after")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _latest_tasks(logs_qs) -> list:
    """The current agent task list = the newest ``agent="tasks"`` CommandLog's JSON payload.

    The model resends the FULL list on every update, so the most recent one is authoritative.
    ``logs_qs`` is an unfiltered CommandLog queryset (a run's logs, or a chat's across turns).
    """
    import json

    log = logs_qs.filter(agent="tasks").order_by("-id").first()
    if not log or not log.stdout:
        return []
    try:
        data = json.loads(log.stdout)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []



def projects(request: HttpRequest):
    """List real projects (the primary landing surface). Loose projects (which back standalone
    "New chat" chats) are hidden — they appear only in the sidebar's Recent list."""
    return render(request, "dashboard/projects.html", {
        "projects": Project.objects.filter(is_loose=False),
        "models": AIModel.objects.filter(is_active=True).order_by("execution_order", "name"),
    })


def project_detail(request: HttpRequest, pk: int):
    """A project's own dashboard: header + stat tiles + its chats + its sandbox files.

    This is the landing page for a project (the Projektai grid links here), giving an overview
    before drilling into a specific chat. Loose projects have no dashboard — they back a single
    standalone chat and are opened directly."""
    project = get_object_or_404(Project, pk=pk)
    if project.is_loose:
        # A loose project is just a wrapper around one chat — skip the dashboard, open the chat.
        chat = project.chats.first()
        if chat:
            return redirect("dashboard-project-chat", pk=chat.id)

    chats = []
    total_tokens = 0
    total_turns = 0
    for chat in project.chats.all():
        runs = list(ModelRun.objects.filter(chat=chat).values(
            "status", "user_prompt", "prompt_tokens", "completion_tokens", "id"
        ))
        total_turns += len(runs)
        tokens = sum((r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0) for r in runs)
        total_tokens += tokens
        last = max(runs, key=lambda r: r["id"]) if runs else None
        status = last["status"] if last else chat.status
        chats.append({
            "obj": chat,
            "is_prompt_writer": (chat.settings or {}).get("mode") == "prompt_writer",
            "turns": len(runs),
            "tokens": tokens,
            "status": status,
            "state": _chat_state(status, len(runs)),
            "last_prompt": (last["user_prompt"] if last else "") or "",
        })
    # Newest activity first; an idle (no-turn) chat sorts by its own updated_at.
    chats.sort(key=lambda c: c["obj"].updated_at, reverse=True)

    files = _list_sandbox_files(Path(project.sandbox_path or ""))
    total_bytes = sum(f["size_bytes"] for f in files)
    shown_files = files[:200]
    for f in shown_files:  # English size strings (Django's filesizeformat localizes to lt)
        f["size_human"] = _human_size(f["size_bytes"])
    try:
        preview = project.preview
    except Exception:
        preview = None

    # Workflows tab: a project-wide log of build runs (each ModelRun = one agent turn) across
    # every chat — the closest thing to a "workflow history" the data model has today.
    runs = []
    for r in (ModelRun.objects.filter(chat__project=project)
              .select_related("chat").order_by("-id")[:100]):
        runs.append({
            "chat": r.chat,
            "prompt": (r.user_prompt or "").strip(),
            "model": r.model_name,
            "status": r.status,
            "state": _chat_state(r.status, 1),
            # English duration string (floatformat localizes the decimal separator to lt).
            "duration_h": f"{r.duration_seconds:.1f}s" if r.duration_seconds else "—",
            "tokens": (r.prompt_tokens or 0) + (r.completion_tokens or 0),
            "created_at": r.created_at,
        })

    # The Databases tab loads lazily — but only when a real SQLite db already exists in the
    # sandbox. (An empty sandbox_path would make db_inspector resolve "db.sqlite3" against the
    # cwd — the dashboard's OWN database — and list_tables would build a project as a side
    # effect; gating on the file mirrors the workbench's database_available check.)
    sandbox = Path(project.sandbox_path or "")
    db_exists = bool(project.sandbox_path) and (sandbox / "db.sqlite3").exists()

    return render(request, "dashboard/project_detail.html", {
        "project": project,
        "chats": chats,
        "stats": {
            "chats": len(chats),
            "turns": total_turns,
            "files": len(files),
            "tokens": total_tokens,
            "size_human": _human_size(total_bytes),
        },
        "files": shown_files,
        "files_truncated": max(0, len(files) - 200),
        "runs": runs,
        "preview": preview,
        "database_url": reverse("dashboard-project-database", args=[project.id]) if db_exists else "",
    })


def _human_size(n: int) -> str:
    """English human-readable byte size (Django's filesizeformat localizes to the lt locale)."""
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _chat_state(status: str, turns: int) -> str:
    """Coarse chat state for the dashboard pill: working / failed / idle."""
    if status in (RunStatus.RUNNING, RunStatus.QUEUED):
        return "working"
    if status in (RunStatus.FAILED, RunStatus.CANCELLED):
        return "failed"
    return "idle"


@require_http_methods(["GET", "POST"])
def project_new(request: HttpRequest):
    """Create a project (new or existing), open its first chat, and launch the agent."""
    active_models = AIModel.objects.filter(is_active=True).order_by("execution_order", "name")
    if request.method == "GET":
        return render(request, "dashboard/project_new.html", {
            "models": active_models,
            "initial_prompt": request.GET.get("q", ""),
        })

    from .services import project_sandbox
    from .services.chat_runner import start_turn

    prompt = (request.POST.get("prompt") or "").strip()
    if not prompt:
        # A whitespace-only / JS-disabled submit from the home "New chat" composer should fall back
        # to home (its own composer), not the full New-Project form it never saw.
        if request.POST.get("from") == "home":
            return redirect("dashboard")
        return render(request, "dashboard/project_new.html", {
            "models": active_models, "error": "Please enter a prompt describing what you want to build.",
        })

    origin = "existing" if request.POST.get("origin") == "existing" else "new"
    name = (request.POST.get("name") or "").strip()
    if not name:
        # Fast, no-LLM name — generate_project_name would block the POST on an Ollama model load.
        name = project_sandbox.quick_project_name(prompt) if origin == "new" else "Imported Project"

    # The home "New chat" composer (from=home) creates a LOOSE project: a hidden sandbox owner so the
    # chat can build fully without cluttering Projektai. The full New-Project form / imports stay real.
    is_loose = request.POST.get("from") == "home"

    project = Project.objects.create(
        name=name,
        slug=project_sandbox.unique_project_slug(name),
        origin=origin,
        is_loose=is_loose,
        settings={},
    )
    project_sandbox.create_sandbox(project)

    import_note = ""
    if origin == "existing":
        upload = request.FILES.get("project_zip")
        path = (request.POST.get("project_path") or "").strip()
        if upload is not None:
            result = project_sandbox.import_zip(project, upload)
        elif path:
            result = project_sandbox.import_path(project, path)
        else:
            result = {"ok": True, "files": 0}
        import_note = result.get("error") or f"Imported {result.get('files', 0)} file(s)."

    chat = Chat.objects.create(
        project=project,
        title=name,
        settings=_chat_settings_from_request(request),
        status=RunStatus.QUEUED,
    )
    turn = _create_turn(chat, prompt)
    start_turn(chat.id, turn.id)
    return redirect("dashboard-project-chat", pk=chat.id)


@require_POST
def project_new_chat(request: HttpRequest, pk: int):
    """Create a fresh, idle chat inside an existing project and open it.

    The chat starts with no turn (status=SUCCEEDED so it reads as *ready*, not running —
    which keeps the composer enabled). It gets its first ModelRun + title the moment the
    user sends a message via ``chat_send``.
    """
    project = get_object_or_404(Project, pk=pk)
    chat = Chat.objects.create(
        project=project,
        title="",
        settings={"model": "", "effort": "medium", "thinking": True, "pursue_goal": True, "auto_approve": False},
        status=RunStatus.SUCCEEDED,
    )
    return redirect("dashboard-project-chat", pk=chat.id)


def project_prompt_writer(request: HttpRequest, pk: int) -> JsonResponse:
    """Get-or-create the project's docked Prompt Writer chat (a ``mode=prompt_writer`` Chat).

    The in-project drawer drives this chat through the normal chat endpoints (logs/send/
    interaction-respond), so the refiner's clarifying-question flow is reused as-is. Returns
    the chat's ids/urls plus the structured prompt once the refiner has produced one.
    """
    project = get_object_or_404(Project, pk=pk)
    # Filter in Python (not a JSON ORM lookup) so we don't depend on the SQLite JSON1 extension.
    chat = next(
        (c for c in Chat.objects.filter(project=project) if (c.settings or {}).get("mode") == "prompt_writer"),
        None,
    )
    if chat is None:
        chat = Chat.objects.create(
            project=project,
            title="Prompt writer",
            settings={"model": "", "effort": "medium", "thinking": True, "pursue_goal": True,
                      "auto_approve": False, "mode": "prompt_writer"},
            status=RunStatus.SUCCEEDED,  # idle until the user sends their idea into the drawer
        )
    return JsonResponse({
        "chat_id": chat.id,
        "logs_url": reverse("dashboard-chat-logs", args=[chat.id]),
        "send_url": reverse("dashboard-chat-send", args=[chat.id]),
        "structured_prompt": (chat.settings or {}).get("structured_prompt") or "",
        "ready": bool((chat.settings or {}).get("structured_prompt")),
    })


def project_chat(request: HttpRequest, pk: int):
    """The project chat page (reuses the chat timeline + workbench)."""
    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    return render(request, "dashboard/project_chat.html", {
        "chat": chat,
        "project": chat.project,
        "chats": chat.project.chats.all(),
        # `move_targets` (real projects for the "Move to project…" picker) is supplied globally by the
        # dashboard.context_processors.recent_chats context processor.
        "models": AIModel.objects.filter(is_active=True).order_by("execution_order", "name"),
        # Only the currently-chosen tag is rendered up front (it's on chat.settings — no network).
        # The full installed-model list is fetched lazily by the retry bar (chat_models_api) so a
        # slow/hung Ollama never blocks this DB-only page render.
        "current_model_tag": (chat.settings or {}).get("ollama_tag") or "",
    })


def chat_models_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Installed Ollama tags for the retry bar's model picker (fetched lazily, only on failure)."""
    from src.llm.OllamaBackend import OllamaBackend

    chat = get_object_or_404(Chat, pk=pk)
    return JsonResponse({
        "models": OllamaBackend.list_installed(),
        "current": (chat.settings or {}).get("ollama_tag") or "",
    })


@require_POST
def chat_retry(request: HttpRequest, pk: int) -> JsonResponse:
    """Re-run the chat's last turn — optionally with an edited prompt and/or a different model.

    Drives the failure-recovery bar (Retry / Edit prompt / Change model). The chosen model is a
    raw installed Ollama tag persisted on the chat so it sticks for subsequent turns too.
    """
    from .services.chat_runner import start_turn

    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    with _CHAT_SEND_LOCKS[chat.id]:
        from .services.chat_runner import heal_stale_turns
        heal_stale_turns(chat)  # an orphaned run (server restart) must not block retrying forever
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)

        # Persist the model override (raw Ollama tag); empty string clears it back to Auto.
        model = (request.POST.get("model") or "").strip()
        settings = dict(chat.settings or {})
        if model:
            settings["ollama_tag"] = model
        else:
            settings.pop("ollama_tag", None)
        Chat.objects.filter(id=chat.id).update(settings=settings, updated_at=timezone.now())
        chat.settings = settings

        prompt = (request.POST.get("prompt") or "").strip()
        if not prompt:  # plain retry → reuse the most recent turn's prompt
            last = ModelRun.objects.filter(chat=chat).order_by("-id").first()
            prompt = (last.user_prompt if last else "") or ""
        if not prompt:
            return JsonResponse({"ok": False, "error": "Nothing to retry — type a message first."}, status=400)

        turn = _create_turn(chat, prompt)
        start_turn(chat.id, turn.id)
    return JsonResponse({"ok": True, "turn_id": turn.id})


def _spawn_project_chat(name: str, prompt: str, chat_settings: dict, origin: str = "new",
                        is_loose: bool = False) -> Chat:
    """Create a Project + its sandbox + a first Chat turn and launch the agent. Returns the Chat.

    ``is_loose=True`` backs a standalone chat (hidden from Projektai) — see ``Project.is_loose``.
    """
    from .services import project_sandbox
    from .services.chat_runner import start_turn

    project = Project.objects.create(
        name=name,
        slug=project_sandbox.unique_project_slug(name),
        origin=origin,
        is_loose=is_loose,
        settings={},
    )
    project_sandbox.create_sandbox(project)
    chat = Chat.objects.create(project=project, title=name, settings=chat_settings, status=RunStatus.QUEUED)
    turn = _create_turn(chat, prompt)
    start_turn(chat.id, turn.id)
    return chat


def _draft_title(prompt: str, fallback: str = "New project") -> str:
    """Pull a short title from a structured prompt's first ``## Heading`` (or its first line)."""
    for line in (prompt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        return line.lstrip("#").strip()[:80] or fallback
    return fallback


@require_http_methods(["GET", "POST"])
def prompt_writer(request: HttpRequest):
    """Write a rough idea; the model refines it (questions → structured prompt) in a chat."""
    active_models = AIModel.objects.filter(is_active=True).order_by("execution_order", "name")
    if request.method == "GET":
        return render(request, "dashboard/prompt_writer.html", {
            "models": active_models,
            "initial_prompt": request.GET.get("q", ""),
        })

    idea = (request.POST.get("prompt") or "").strip()
    if not idea:
        return render(request, "dashboard/prompt_writer.html", {
            "models": active_models, "error": "Describe your web idea to get started.",
        })

    settings = _chat_settings_from_request(request)
    settings["mode"] = "prompt_writer"
    name = f"Idea: {idea[:60]}"
    # The idea/refiner project is loose (hidden from Projektai); promoting a draft to a real build
    # via create_from_draft spins up a normal (non-loose) project.
    chat = _spawn_project_chat(name, idea, settings, is_loose=True)
    return redirect("dashboard-project-chat", pk=chat.id)


@require_POST
def create_from_draft(request: HttpRequest, pk: int, kind: str):
    """Hand a Prompt-Writer chat's structured prompt off to a new Project (design-first build)."""
    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    structured = (chat.settings or {}).get("structured_prompt") or ""
    if not structured.strip():
        # Not refined yet — fall back to the original idea so the action is never a dead end.
        first = ModelRun.objects.filter(chat=chat).order_by("id").first()
        structured = (first.user_prompt if first else "") or ""
    title = _draft_title(structured)

    # Carry the model/effort/thinking choices forward, but drop the prompt-writer mode.
    carry = {k: v for k, v in (chat.settings or {}).items() if k != "mode" and k != "structured_prompt"}
    new_chat = _spawn_project_chat(title or "New project", structured, carry)
    return redirect("dashboard-project-chat", pk=new_chat.id)


#: Per-chat locks serialising the check-then-create of a turn. The agent runs in-process daemon
#: threads, so an in-process lock (not select_for_update, which is a no-op on SQLite) closes the
#: TOCTOU window where two concurrent POSTs both see "no active turn" and both launch an agent.
_CHAT_SEND_LOCKS: dict[int, threading.Lock] = defaultdict(threading.Lock)


@require_POST
def chat_send(request: HttpRequest, pk: int) -> JsonResponse:
    """Send a follow-up message: create a new turn + launch the agent."""
    from .services.chat_runner import start_turn

    chat = get_object_or_404(Chat, pk=pk)
    prompt = (request.POST.get("prompt") or "").strip()
    if not prompt:
        return JsonResponse({"ok": False, "error": "Empty message."}, status=400)
    # One turn at a time per chat: concurrent turns would write the same sandbox in parallel.
    # Serialise the guard so two near-simultaneous sends can't both pass the existence check.
    with _CHAT_SEND_LOCKS[chat.id]:
        from .services.chat_runner import heal_stale_turns
        heal_stale_turns(chat)  # an orphaned run (server restart) must not block sending forever
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        # First message in a freshly-created ("New chat") chat: title it from the prompt.
        if not chat.title:
            chat.title = prompt[:60]
            chat.save(update_fields=["title"])
        turn = _create_turn(chat, prompt)
        start_turn(chat.id, turn.id)
    return JsonResponse({"ok": True, "turn_id": turn.id})


@require_POST
def chat_compact(request: HttpRequest, pk: int) -> JsonResponse:
    """Manually compact the conversation into memory ("Compact now"). Runs in the background (one
    LLM call) so the request returns immediately; the context meter reflects ``compacting`` and then
    the freed-up window once it finishes."""
    import threading

    from django.db import close_old_connections

    from .services.chat_runner import ChatTurnRunner

    chat = get_object_or_404(Chat, pk=pk)
    with _CHAT_SEND_LOCKS[chat.id]:
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        if (chat.settings or {}).get("_compacting"):
            return JsonResponse({"ok": False, "error": "Already compacting."}, status=409)
        s = dict(chat.settings or {})
        s["_compacting"] = True
        chat.settings = s
        chat.save(update_fields=["settings"])

    def _run():
        close_old_connections()
        try:
            fresh = Chat.objects.get(id=chat.id)
            ok = ChatTurnRunner().compact_chat(fresh)
            if not ok:  # nothing to compact / failed — clear the flag so the meter doesn't hang
                s2 = dict(Chat.objects.filter(id=chat.id).values_list("settings", flat=True).first() or {})
                s2.pop("_compacting", None)
                Chat.objects.filter(id=chat.id).update(settings=s2)
        except Exception:
            s2 = dict(Chat.objects.filter(id=chat.id).values_list("settings", flat=True).first() or {})
            s2.pop("_compacting", None)
            Chat.objects.filter(id=chat.id).update(settings=s2)
        finally:
            close_old_connections()

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"ok": True})


@require_POST
def chat_plan_update(request: HttpRequest, pk: int) -> JsonResponse:
    """Save an edited plan ('Edit plan → Edit text directly'). Re-opens the decision (un-approves)."""
    chat = get_object_or_404(Chat, pk=pk)
    text = (request.POST.get("plan") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "Empty plan."}, status=400)
    # Serialise against the agent thread's settings write (which rewrites the whole JSON blob) so an
    # edit during/after a turn can't be lost to a last-writer-wins race.
    with _CHAT_SEND_LOCKS[chat.id]:
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        s = dict(chat.settings or {})
        s["plan"] = text
        s.pop("plan_approved", None)
        chat.settings = s
        chat.save(update_fields=["settings"])
    return JsonResponse({"ok": True})


@require_POST
def chat_plan_refine(request: HttpRequest, pk: int) -> JsonResponse:
    """Refine the plan with the model ('Edit plan → Refine with AI'): carry the user's edits and start
    a ONE-SHOT plan-only turn from their feedback. The refiner re-presents an updated plan. Uses a
    transient `_refine_pending` flag (not a sticky `planning`) so the chat isn't trapped in planning."""
    from .services.chat_runner import start_turn

    chat = get_object_or_404(Chat, pk=pk)
    feedback = (request.POST.get("feedback") or request.POST.get("prompt") or "").strip()
    edited = (request.POST.get("plan") or "").strip()
    with _CHAT_SEND_LOCKS[chat.id]:
        from .services.chat_runner import heal_stale_turns
        heal_stale_turns(chat)
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        s = dict(chat.settings or {})
        s["_refine_pending"] = True   # one-shot: plan-only for this turn, consumed in _post_turn
        s.pop("plan_approved", None)
        if edited:
            s["plan"] = edited        # carry manual edits into the refinement
        chat.settings = s
        chat.save(update_fields=["settings"])
        turn = _create_turn(chat, feedback or "Refine the plan above based on my edits.")
        start_turn(chat.id, turn.id)
    return JsonResponse({"ok": True, "turn_id": turn.id})


@require_POST
def chat_implement(request: HttpRequest, pk: int) -> JsonResponse:
    """Approve the plan and implement it in THIS chat: switch planning → build, mark the plan
    approved, and start a build turn that follows the plan-driven design-first workflow."""
    from .services.chat_runner import start_turn

    chat = get_object_or_404(Chat, pk=pk)
    if not (chat.settings or {}).get("plan"):
        return JsonResponse({"ok": False, "error": "No plan to implement."}, status=400)
    with _CHAT_SEND_LOCKS[chat.id]:
        from .services.chat_runner import heal_stale_turns
        heal_stale_turns(chat)
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        # Already implemented? Don't start a second build (it would re-run the prototype/startproject).
        if (chat.settings or {}).get("plan_approved"):
            return JsonResponse({"ok": False, "error": "This plan is already being implemented."}, status=409)
        s = dict(chat.settings or {})
        s["plan_approved"] = True
        s["planning"] = False         # switch from planning to build mode
        s.pop("_refine_pending", None)
        chat.settings = s
        chat.save(update_fields=["settings"])
        turn = _create_turn(chat, "Implement the approved plan.")
        start_turn(chat.id, turn.id)
    return JsonResponse({"ok": True, "turn_id": turn.id})


@require_POST
def project_delete(request: HttpRequest, pk: int):
    """Delete a project and wipe its sandbox directory."""
    project = get_object_or_404(Project, pk=pk)
    project.delete()  # model.delete() removes the sandbox dir (guarded)
    return redirect("dashboard-projects")


@require_POST
def project_update(request: HttpRequest, pk: int):
    """Update a project's editable settings (currently its name) from the dashboard Settings tab."""
    project = get_object_or_404(Project, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if name and name != project.name:
        project.name = name[:180]
        project.save(update_fields=["name", "updated_at"])
    return redirect("dashboard-project-detail", pk=project.id)


@require_POST
def chat_delete(request: HttpRequest, pk: int):
    """Delete a single chat (its turns/logs cascade).

    A LOOSE chat owns its backing project + sandbox (it's a standalone "New chat"), so deleting it
    also deletes that project and wipes its sandbox dir, then returns home. A project chat keeps the
    project + sandbox and falls back to a sibling chat (or Projektai).
    """
    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    project = chat.project
    was_loose = bool(project and project.is_loose)
    chat.delete()
    if was_loose:
        project.delete()  # model.delete() removes the loose sandbox dir (guarded)
        return redirect("dashboard")
    # Fall back to a sibling WORK chat (skip the docked prompt-writer chat — filtered in Python so we
    # don't depend on the SQLite JSON1 extension, matching project_prompt_writer / the context processor).
    other = next(
        (c for c in Chat.objects.filter(project_id=project.id)
         if (c.settings or {}).get("mode") != "prompt_writer"),
        None,
    ) if project else None
    if other:
        return redirect("dashboard-project-chat", pk=other.id)
    return redirect("dashboard-projects")


@require_POST
def chat_rename(request: HttpRequest, pk: int) -> JsonResponse:
    """Rename a chat (sets ``Chat.title``). Works for both loose and project chats."""
    chat = get_object_or_404(Chat, pk=pk)
    title = (request.POST.get("title") or "").strip()
    if not title:
        return JsonResponse({"ok": False, "error": "Title can't be empty."}, status=400)
    chat.title = title[:200]
    chat.save(update_fields=["title", "updated_at"])
    return JsonResponse({"ok": True, "title": chat.title})


@require_POST
def chat_move(request: HttpRequest, pk: int) -> JsonResponse:
    """Move a standalone (loose) chat into a real project, merging its sandbox files in.

    The loose chat's files are folded into the target project's shared sandbox (never overwriting the
    project's scaffold or existing files), the chat is reassigned to the target, and the now-empty
    loose project + its sandbox are removed. Reassignment happens BEFORE the loose project is deleted
    so the ``Chat.project`` CASCADE doesn't take the chat down with it.
    """
    from .services.project_sandbox import merge_sandbox

    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    loose = chat.project
    if not (loose and loose.is_loose):
        return JsonResponse({"ok": False, "error": "Only standalone chats can be moved into a project."}, status=400)
    target_id = (request.POST.get("project") or "").strip()
    target = Project.objects.filter(id=int(target_id)).first() if target_id.isdigit() else None
    if target is None or target.is_loose:
        return JsonResponse({"ok": False, "error": "Pick a project to move this chat into."}, status=400)
    if target.id == loose.id:
        return JsonResponse({"ok": False, "error": "That's the same workspace."}, status=400)

    with _CHAT_SEND_LOCKS[chat.id]:
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        result = merge_sandbox(loose.sandbox_path, target.sandbox_path)
        if not result.get("ok"):
            return JsonResponse({"ok": False, "error": result.get("error") or "Could not merge files."}, status=400)
        with transaction.atomic():
            Chat.objects.filter(id=chat.id).update(project=target, updated_at=timezone.now())
            loose.delete()  # reassigned above, so CASCADE no longer reaches the chat; wipes loose sandbox
    return JsonResponse({
        "ok": True,
        "chat_url": reverse("dashboard-project-chat", args=[chat.id]),
        "files_copied": result.get("files_copied", 0),
        "skipped": len(result.get("skipped", [])),
    })


# --------------------------------------------------------------- chat APIs
def chat_logs_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Aggregate the chat's CommandLogs (across all turns) + sandbox preview/db status.

    Mirrors ``model_run_logs_api`` so chat.js / workbench.js consume it unchanged.
    """
    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    project = chat.project
    sandbox = Path(project.sandbox_path or "")

    # Self-heal orphaned turns (heartbeat gone stale after a server restart / dead agent thread)
    # so the chat never sits on "running" forever with the composer locked.
    from .services.chat_runner import heal_stale_turns
    heal_stale_turns(chat)

    logs = CommandLog.objects.filter(model_run__chat=chat).order_by("id")
    after_id = _after_id(request)
    if after_id is not None:
        logs = logs.filter(id__gte=after_id)

    latest = ModelRun.objects.filter(chat=chat).order_by("-id").first()
    started = latest.started_at if latest else None
    if started:
        end = latest.finished_at or timezone.now()
        elapsed_seconds = max(0.0, (end - started).total_seconds())
    else:
        elapsed_seconds = 0.0
    total_tokens = 0
    for t in ModelRun.objects.filter(chat=chat).values("prompt_tokens", "completion_tokens"):
        total_tokens += int(t["prompt_tokens"] or 0) + int(t["completion_tokens"] or 0)

    generated_files = _list_sandbox_files(sandbox)
    try:
        preview_obj = project.preview
    except Exception:
        preview_obj = None
    # A runserver that crashed after launch leaves a stale RUNNING row; demote it here so the
    # panel shows the failure + Retry instead of a dead iframe (there are no manual controls).
    preview_obj = PreviewService().reconcile(preview_obj)
    preview = None
    if preview_obj:
        from .services.route_discovery import cached_routes

        preview = {
            "status": preview_obj.status,
            "status_label": preview_obj.get_status_display(),
            "url": preview_obj.url,
            "error_message": preview_obj.error_message,
            "links": cached_routes(sandbox) if project.sandbox_path else [],
        }
    # Preview is offered only when there is actually something to SERVE: a front-end page
    # (.html → static http.server) or a real/scaffolded Django project (manage.py → runserver) —
    # or a preview server that is already running. A chat that only produced text or stray
    # non-web files keeps the Preview tab hidden.
    previewable = any(
        f["path"].endswith((".html", ".htm")) or f["path"].rsplit("/", 1)[-1] == "manage.py"
        for f in generated_files
    )
    preview_available = previewable or bool(preview and preview.get("status") == PreviewServer.Status.RUNNING)

    pending = [
        {
            "id": pi.id,
            "kind": pi.kind,
            "prompt": pi.prompt,
            "options": pi.options,
            "command": pi.command,
            "respond_url": reverse("dashboard-interaction-respond", args=[pi.id]),
        }
        for pi in PendingInteraction.objects.filter(
            model_run__chat=chat, status=PendingInteraction.Status.PENDING
        ).order_by("id")
    ]

    draft = None
    if (chat.settings or {}).get("mode") == "prompt_writer":
        # Reveal the Create Project / Create Benchmark hand-off once the refiner is DONE — even if it
        # produced a thin/empty spec, since create_from_draft falls back to the original idea. So the
        # user is never stuck after the interview finishes.
        finished = bool(latest and latest.status not in (RunStatus.QUEUED, RunStatus.RUNNING))
        ready = bool((chat.settings or {}).get("structured_prompt")) or finished
        draft = {
            "mode": "prompt_writer",
            "ready": ready,
            "structured_prompt": (chat.settings or {}).get("structured_prompt") or "",
        }

    # Composer "Planning" toggle: once a plan is produced, the frontend opens the plan modal
    # (Implement / Edit). `ready` gates the auto-open — it stays open-able via the topbar chip even
    # after approval, but only auto-opens while the plan is fresh (settled + not yet approved).
    plan = None
    plan_text = (chat.settings or {}).get("plan")
    if plan_text:
        approved = bool((chat.settings or {}).get("plan_approved"))
        # `ready` (auto-open the modal + offer Implement) only when the latest turn SUCCEEDED — so a
        # FAILED/CANCELLED refine doesn't leave a stale plan looking actionable.
        settled_ok = bool(latest and latest.status == RunStatus.SUCCEEDED)
        plan = {"text": plan_text, "approved": approved, "ready": settled_ok and not approved}

    from .services.chat_runner import ChatTurnRunner

    context = ChatTurnRunner.estimate_context(chat)
    context["compact_url"] = reverse("dashboard-chat-compact", args=[chat.id])

    return JsonResponse({
        "pending": pending,
        "draft": draft,
        "plan": plan,
        "context": context,
        "tasks": _latest_tasks(CommandLog.objects.filter(model_run__chat=chat)),
        "model_run": {
            "id": latest.id if latest else None,
            "status": (latest.status if latest else chat.status),
            "iteration": ModelRun.objects.filter(chat=chat).count(),  # number of turns
            "started_at": started.isoformat() if started else None,
            "elapsed_seconds": elapsed_seconds,
            "total_tokens": total_tokens,
            # "Done." renders ONLY when the turn really finished with a final summary (a `finish`
            # card saying what was done) — a turn that just stopped shows no Done tag at all.
            "has_summary": bool(latest) and CommandLog.objects.filter(model_run=latest, agent="finish").exists(),
        },
        "generated_files": generated_files,
        "preview_available": preview_available,
        # The Database tab appears only once a real SQLite db exists in the sandbox (built by
        # ensure_runnable_project after a turn writes files) — not for any stray source file.
        "database_available": bool(project.sandbox_path) and (sandbox / "db.sqlite3").exists(),
        "preview": preview,
        "logs": [
            {
                "id": log.id,
                "kind": log.kind,
                "agent": log.agent,
                "command": " ".join(str(part) for part in log.command),
                "stdout": log.display_stdout,
                "stderr": log.display_stderr,
                "exit_code": log.exit_code,
                "duration_seconds": log.duration_seconds,
                "created_at": log.created_at.isoformat(),
                "thinking_trace": "",
            }
            for log in logs[:300]
        ],
    })


def chat_resources_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Minimal per-turn stats + project-wide totals (project chats don't run the GPU monitor)."""
    from django.db.models import Sum

    chat = get_object_or_404(Chat.objects.select_related("project"), pk=pk)
    latest = ModelRun.objects.filter(chat=chat).order_by("-id").first()
    model_stats = {}
    if latest:
        model_stats = {
            "prompt_tokens": latest.prompt_tokens,
            "completion_tokens": latest.completion_tokens,
            "tokens_per_second": latest.tokens_per_second,
        }
    # Project totals: aggregate across every "work" chat in the project (the docked Prompt
    # Writer chat's refiner Q&A turns aren't part of the build, so they're excluded).
    project = chat.project
    work_chat_ids = [c.id for c in project.chats.all() if (c.settings or {}).get("mode") != "prompt_writer"]
    turns = ModelRun.objects.filter(chat_id__in=work_chat_ids)
    agg = turns.aggregate(p=Sum("prompt_tokens"), c=Sum("completion_tokens"), d=Sum("duration_seconds"))
    files = _list_sandbox_files(Path(project.sandbox_path or ""))
    project_totals = {
        "chats": len(work_chat_ids),
        "messages": turns.count(),
        "prompt_tokens": int(agg["p"] or 0),
        "completion_tokens": int(agg["c"] or 0),
        "agent_seconds": float(agg["d"] or 0.0),
        "files": len(files),
        "bytes": sum(int(f.get("size_bytes") or 0) for f in files),
    }
    return JsonResponse({"model_run": model_stats, "samples": [], "project_totals": project_totals})


# --------------------------------------------------- composer / and + menu actions
@require_POST
def project_upload_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Save an uploaded file into the project sandbox (composer 'Attach file' / 'Upload from computer').

    Files land under ``uploads/`` (never the sandbox root) so they can't shadow/overwrite the
    scaffold (manage.py / settings.py / db.sqlite3) — which the sandbox subprocess executes — and
    are capped at the same size limit as project imports.
    """
    from django.utils.text import get_valid_filename
    from .services.workspaces import safe_workspace_path
    from .services.project_sandbox import MAX_IMPORT_BYTES

    project = get_object_or_404(Project, pk=pk)
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"ok": False, "error": "No file provided."}, status=400)
    if upload.size and upload.size > MAX_IMPORT_BYTES:
        return JsonResponse({"ok": False, "error": "File too large (max 50 MB)."}, status=400)
    sandbox = Path(project.sandbox_path or "")
    if not sandbox.exists():
        return JsonResponse({"ok": False, "error": "Project sandbox is not ready yet."}, status=400)
    name = get_valid_filename(upload.name) or "upload.bin"
    try:
        target = safe_workspace_path(sandbox, "uploads/" + name)
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid file name."}, status=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(target, "wb") as fh:
        for chunk in upload.chunks():
            written += len(chunk)
            if written > MAX_IMPORT_BYTES:  # defends against a streamed/under-reported size
                fh.close()
                target.unlink(missing_ok=True)
                return JsonResponse({"ok": False, "error": "File too large (max 50 MB)."}, status=400)
            fh.write(chunk)
    rel = target.resolve().relative_to(sandbox.resolve()).as_posix()
    return JsonResponse({"ok": True, "path": rel})


def project_files_api(request: HttpRequest, pk: int) -> JsonResponse:
    """List sandbox file paths for the composer 'Mention file from this project' picker."""
    project = get_object_or_404(Project, pk=pk)
    files = _list_sandbox_files(Path(project.sandbox_path or ""))
    return JsonResponse({"files": [f["path"] for f in files]})


@require_POST
def chat_stop(request: HttpRequest, pk: int) -> JsonResponse:
    """Stop the running model: flag the active turn CANCELLED so the agent aborts between steps."""
    chat = get_object_or_404(Chat, pk=pk)
    stopped = ModelRun.objects.filter(
        chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]
    ).update(status=RunStatus.CANCELLED, finished_at=timezone.now())
    if stopped:
        Chat.objects.filter(id=chat.id).update(status=RunStatus.CANCELLED, updated_at=timezone.now())
    return JsonResponse({"ok": True, "stopped": stopped})


@require_POST
def chat_clear(request: HttpRequest, pk: int) -> JsonResponse:
    """Clear a conversation: delete the chat's turns (cascades logs/interactions); keep the sandbox."""
    chat = get_object_or_404(Chat, pk=pk)
    # Serialise the check-then-delete against chat_send/chat_retry (same per-chat lock) so a
    # concurrent send can't slip a fresh turn + agent thread in between our check and the delete.
    with _CHAT_SEND_LOCKS[chat.id]:
        if ModelRun.objects.filter(chat=chat, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).exists():
            return JsonResponse({"ok": False, "error": "The agent is still working — wait for it to finish."}, status=409)
        ModelRun.objects.filter(chat=chat).delete()
        chat.title = ""
        chat.status = RunStatus.SUCCEEDED  # idle/ready, so the composer stays enabled
        chat.save(update_fields=["title", "status"])
    return JsonResponse({"ok": True})


@require_POST
def chat_settings_update(request: HttpRequest, pk: int) -> JsonResponse:
    """Live-update a chat's model / effort / thinking / planning (the / palette Model section)."""
    chat = get_object_or_404(Chat, pk=pk)
    s = dict(chat.settings or {})
    if "model" in request.POST:
        val = (request.POST.get("model") or "").strip()
        # Only persist a real, active AIModel pk; anything else (junk, deactivated, deleted) → Auto.
        # Storing a stale pk would later make _create_turn write a dangling FK → IntegrityError 500.
        if val and not (val.isdigit() and AIModel.objects.filter(id=int(val), is_active=True).exists()):
            val = ""
        s["model"] = val
        s.pop("ollama_tag", None)  # a palette model choice supersedes a stale raw tag from the retry bar
    if "effort" in request.POST:
        eff = request.POST.get("effort")
        if eff in {"low", "medium", "high"}:
            s["effort"] = eff
    if "thinking" in request.POST:
        s["thinking"] = request.POST.get("thinking") == "on"
    if "planning" in request.POST:
        s["planning"] = request.POST.get("planning") == "on"
    if "pursue_goal" in request.POST:
        s["pursue_goal"] = request.POST.get("pursue_goal") == "on"
    if "auto_approve" in request.POST:
        s["auto_approve"] = request.POST.get("auto_approve") == "on"
    chat.settings = s
    chat.save(update_fields=["settings"])
    return JsonResponse({"ok": True, "settings": {
        "model": s.get("model", ""),
        "effort": s.get("effort", "medium"),
        "thinking": bool(s.get("thinking", False)),
        "planning": bool(s.get("planning", False)),
        "pursue_goal": bool(s.get("pursue_goal", False)),
        "auto_approve": bool(s.get("auto_approve", False)),
    }})


def project_file_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Return the content of one file inside the project sandbox (for the Files viewer)."""
    from .services.workspaces import safe_workspace_path

    project = get_object_or_404(Project, pk=pk)
    path = request.GET.get("path", "")
    if not path:
        return JsonResponse({"error": "Missing `path`."}, status=400)
    try:
        target = safe_workspace_path(Path(project.sandbox_path), path)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if not target.exists() or not target.is_file():
        return JsonResponse({"error": "File not found."}, status=404)
    try:
        content = target.read_text(encoding="utf-8")
        return JsonResponse({"path": path, "content": content, "binary": False})
    except (UnicodeDecodeError, OSError):
        return JsonResponse({"path": path, "content": "", "binary": True})


@require_POST
def project_preview_api(request: HttpRequest, pk: int, action: str) -> JsonResponse:
    """Start / stop / restart the project sandbox preview server."""
    project = get_object_or_404(Project, pk=pk)
    service = PreviewService()
    if action == "start":
        preview = service.start_project(project)
    elif action == "stop":
        preview = service.stop_project(project)
    elif action == "restart":
        preview = service.restart_project(project)
    else:
        return JsonResponse({"error": "Unknown preview action."}, status=400)
    from .services.route_discovery import cached_routes

    return JsonResponse({
        "status": preview.status,
        "status_label": preview.get_status_display(),
        "url": preview.url,
        "port": preview.port,
        "error_message": preview.error_message,
        "links": cached_routes(project.sandbox_path) if project.sandbox_path else [],
    })


@require_http_methods(["GET", "POST"])
def project_database_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Inspect (GET) / insert into (POST) the project sandbox's SQLite database."""
    import json

    from .services import db_inspector
    from .services.project_sandbox import SandboxRef

    project = get_object_or_404(Project, pk=pk)
    ref = SandboxRef(project.sandbox_path)
    if request.method == "POST":
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)
        table = str(payload.get("table") or "")
        values = payload.get("values")
        if not table or not isinstance(values, dict):
            return JsonResponse({"ok": False, "error": "`table` and a `values` object are required."}, status=400)
        return JsonResponse(db_inspector.insert_row(ref, table, values))
    table = request.GET.get("table")
    if table:
        try:
            limit = int(request.GET.get("limit", 200))
            offset = int(request.GET.get("offset", 0))
        except ValueError:
            limit, offset = 200, 0
        return JsonResponse(db_inspector.table_rows(ref, table, limit, offset))
    return JsonResponse(db_inspector.list_tables(ref))


@require_POST
def interaction_respond_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Record the user's answer to a pending question / approval, unblocking the agent thread."""
    pi = get_object_or_404(PendingInteraction, pk=pk)
    if pi.status != PendingInteraction.Status.PENDING:
        return JsonResponse({"ok": True, "already": True})
    if pi.kind == PendingInteraction.Kind.APPROVAL:
        decision = request.POST.get("decision", "")
        pi.status = PendingInteraction.Status.ANSWERED if decision == "approve" else PendingInteraction.Status.DENIED
        pi.answer = decision or "deny"
    else:
        pi.answer = (request.POST.get("answer") or "").strip()
        pi.status = PendingInteraction.Status.ANSWERED
    pi.answered_at = timezone.now()
    pi.save(update_fields=["status", "answer", "answered_at"])
    return JsonResponse({"ok": True})


# --------------------------------------------------------------- chat helpers
def _create_turn(chat: Chat, prompt: str) -> ModelRun:
    """Create a ModelRun "turn" for a chat message (workspace = the project sandbox)."""
    model_name = "agent"
    model_ref = (chat.settings or {}).get("model")
    tag = ((chat.settings or {}).get("ollama_tag") or "").strip()
    # Resolve the AIModel once; a stale pk (e.g. the configured model was later deleted) must
    # degrade to None rather than writing a dangling FK that raises IntegrityError on create.
    ai = AIModel.objects.filter(id=int(model_ref)).first() if (model_ref and str(model_ref).isdigit()) else None
    if tag:
        model_name = tag  # a raw Ollama tag chosen via the retry bar
    elif ai:
        model_name = ai.name
    return ModelRun.objects.create(
        chat=chat,
        ai_model_id=ai.id if ai else None,
        model_name=model_name,
        workspace_path=chat.project.sandbox_path,
        user_prompt=prompt,
        status=RunStatus.QUEUED,
    )


def _chat_settings_from_request(request: HttpRequest) -> dict:
    effort = request.POST.get("effort", "medium")
    return {
        "model": request.POST.get("model") or "",
        "effort": effort if effort in {"low", "medium", "high"} else "medium",
        "thinking": request.POST.get("thinking") == "on",
        "planning": request.POST.get("planning") == "on",
        "pursue_goal": request.POST.get("pursue_goal") == "on",
        "auto_approve": request.POST.get("auto_approve") == "on",
    }


#: Dirs skipped when listing a project sandbox's files (heavy / machine-specific) — note `migrations`
#: is intentionally NOT skipped so generated migration files show in the Files tab.
_SANDBOX_SKIP_DIRS = {".venv", "venv", "env", "__pycache__", ".git", ".hg", ".svn",
                      ".pytest_cache", ".mypy_cache", "node_modules", ".idea", ".vscode"}
#: File suffixes surfaced in the Files tab.
_SANDBOX_SUFFIXES = {".py", ".html", ".htm", ".css", ".js", ".json", ".txt", ".md",
                     ".cfg", ".ini", ".yaml", ".yml", ".toml"}


def _list_sandbox_files(sandbox: Path) -> list[dict]:
    """List source files in the project sandbox (skips scaffold/db/heavy dirs)."""
    if not sandbox or not sandbox.exists():
        return []
    root = sandbox.resolve()
    files = []
    for item in sorted(sandbox.rglob("*")):
        if not item.is_file() or any(part in _SANDBOX_SKIP_DIRS for part in item.parts):
            continue
        if item.suffix.lower() not in _SANDBOX_SUFFIXES:
            continue
        # Dashboard-owned dot-files (.kursinis_routes.json etc.) are infrastructure, not the
        # agent's work — keep them out of the Files tab.
        if item.name.startswith("."):
            continue
        try:
            rel = item.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        files.append({"path": rel, "file_type": item.suffix.lstrip("."), "size_bytes": item.stat().st_size})
        if len(files) >= 500:
            break
    return files
