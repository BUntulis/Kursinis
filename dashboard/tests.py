"""Tests for the database-backed benchmark dashboard MVP."""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase, TransactionTestCase

from .forms import AIModelForm
from .models import AIModel, CommandLog, ModelRun, PreviewServer, ResourceSample, RunStatus
from .services.previews import PreviewService

# Warm platform.uname()'s cache at import time (before any test patches
# subprocess.run). previews.py calls platform.system(), and on Windows the first
# uncached call shells out via platform._syscmd_ver; a broad subprocess.run mock
# would otherwise feed it a MagicMock. Pre-existing test fragility, not part of
# the PromptTemplate refactor.
import platform as _platform

_platform.uname()


from src.llm import LLMBackend  # noqa: E402


# chat_runner._classify_intent's prompt ends with this line; scripted fakes answer it out-of-band
# (BUILD) so the classifier call doesn't consume/shift their canned agent script.
_INTENT_PROMPT_MARKER = "Reply with exactly one word: BUILD or CHAT."


def _intent_build_response():
    from src.llm.LLMResponse import LLMResponse

    return LLMResponse(text="BUILD", prompt_tokens=2, completion_tokens=1, latency_sec=0.0, model="fake")


class _FakeChatLLM(LLMBackend):
    """A no-Ollama text-protocol model: writes a file on the first turn, then DONE.

    Inherits the base ``chat()`` (merges messages → ``complete()``) so the router works.
    """

    name = "fake"
    supports_tools = False

    def __init__(self, model=None, host=None):
        self.model = model or "fake"
        self.calls = 0

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        from src.llm.LLMResponse import LLMResponse

        if _INTENT_PROMPT_MARKER in prompt:
            return _intent_build_response()
        self.calls += 1
        if self.calls == 1:
            text = ("```python\n# blog/models.py\nfrom django.db import models\n\n"
                    "class Post(models.Model):\n    title = models.CharField(max_length=200)\n```")
        else:
            text = "Done — created the Post model.\nDONE"
        return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")


class ProjectChatTurnTests(TestCase):
    """End-to-end: a project-chat turn runs the dynamic agent against the sandbox."""

    def test_chat_turn_runs_agent_and_writes_files(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(
            name="Smoke", slug=project_sandbox.unique_project_slug("Smoke"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)  # removes the sandbox dir
        chat = Chat.objects.create(project=project, title="Smoke")
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="Create a Post model with a title.", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _FakeChatLLM), \
                patch("dashboard.services.project_venv.ensure_venv", lambda *a, **k: {"ok": True, "built": False}):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        self.assertEqual(chat.status, RunStatus.SUCCEEDED)
        # the user message + a write tool step were logged
        self.assertTrue(CommandLog.objects.filter(model_run=turn, agent="user").exists())
        self.assertTrue(CommandLog.objects.filter(model_run=turn, agent="write").exists())
        # the agent actually wrote the file into the project sandbox
        self.assertTrue((Path(project.sandbox_path) / "blog" / "models.py").exists())

    def test_chat_logs_api_aggregates_turns(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="Agg", slug="agg-proj", origin=Project.Origin.NEW, sandbox_path="")
        chat = Chat.objects.create(project=project, title="Agg")
        turn = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="hi", status=RunStatus.SUCCEEDED)
        CommandLog.objects.create(model_run=turn, kind=CommandLog.Kind.SYSTEM, agent="user", stdout="hi")
        resp = self.client.get(f"/api/chats/{chat.id}/logs/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["agent"], "user")
        self.assertEqual(data["model_run"]["iteration"], 1)

    def test_conversational_turn_answers_directly_without_building(self):
        """A 'just chatting' message gets one direct answer: no venv, no agent loop, no files,
        no clarifying questions — classified by the small intent call."""
        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _SmallTalkLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                if _INTENT_PROMPT_MARKER in prompt:
                    return LLMResponse(text="CHAT", prompt_tokens=2, completion_tokens=1,
                                       latency_sec=0.0, model="fake")
                return LLMResponse(text="**Hi!** I can build Django apps for you.",
                                   prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Talk", slug=project_sandbox.unique_project_slug("Talk"), origin=Project.Origin.NEW)
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Talk")
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="hello, what can you do?", status=RunStatus.QUEUED)

        venv_calls = []
        with patch("src.llm.OllamaBackend.OllamaBackend", _SmallTalkLLM), \
                patch("dashboard.services.project_venv.ensure_venv",
                      lambda *a, **k: venv_calls.append(1)):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        self.assertEqual(venv_calls, [])  # no venv setup for small talk
        finish = CommandLog.objects.filter(model_run=turn, agent="finish").first()
        self.assertIsNotNone(finish)
        self.assertIn("build Django apps", finish.stdout)
        self.assertFalse(CommandLog.objects.filter(model_run=turn, agent="write").exists())
        self.assertFalse(PendingInteraction.objects.filter(model_run=turn).exists())

    def test_final_summary_unwraps_json_tool_call_dump(self):
        """A native-tool model that emits its finish call AS TEXT (a ```json dump) must surface
        the human spec, not the raw JSON blob — in the answer bubble AND the saved plan."""
        from dashboard.services.chat_runner import ChatTurnRunner

        spec = "# Task Manager\n**Overview:** A task system.\n\n**Build workflow:**\n1. Prototype"
        fenced = "```json\n" + json.dumps({"name": "finish", "arguments": {"spec": spec}}) + "\n```"
        self.assertEqual(ChatTurnRunner._unwrap_tool_json(fenced), spec)
        # bare JSON + double-encoded arguments
        bare = json.dumps({"name": "finish", "arguments": json.dumps({"summary": "All done."})})
        self.assertEqual(ChatTurnRunner._unwrap_tool_json(bare), "All done.")
        # normal prose and broken JSON pass through untouched
        prose = "Done — created the Post model."
        self.assertEqual(ChatTurnRunner._unwrap_tool_json(prose), prose)
        broken = '```json\n{"name": "finish", "arguments": {"spec": "trunc'
        self.assertEqual(ChatTurnRunner._unwrap_tool_json(broken), broken)
        # and the full path: the last assistant message is the dump → summary is the spec
        state = {"messages": [{"role": "assistant", "content": fenced + "\nDONE"}]}
        self.assertEqual(ChatTurnRunner._final_summary(state, max_len=0), spec)

    def test_preview_available_requires_previewable_content(self):
        """The Preview tab is offered only when something can actually be served: an .html page
        or a manage.py — not for arbitrary source files."""
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Pv", slug=project_sandbox.unique_project_slug("Pv"), origin=Project.Origin.NEW)
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Pv")
        sandbox = Path(project.sandbox_path)

        (sandbox / "notes.py").write_text("x = 1\n", encoding="utf-8")
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertFalse(data["preview_available"])  # a stray .py file is not previewable

        (sandbox / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertTrue(data["preview_available"])

    def test_project_pages_render(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="Page", slug="page-proj", origin=Project.Origin.NEW, sandbox_path="")
        chat = Chat.objects.create(project=project, title="Page")
        for url in ("/projects/", "/projects/new/", f"/chats/{chat.id}/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class ProjectHubChatTests(TestCase):
    """Phase 1: multiple chats per project — new-chat route, idle state, auto-title, switcher."""

    def _project(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Hub", slug=project_sandbox.unique_project_slug("Hub"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)  # removes the sandbox dir
        return project

    def test_new_chat_route_creates_idle_chat(self):
        from dashboard.models import Chat

        project = self._project()
        resp = self.client.post(f"/projects/{project.id}/chats/new/")
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.filter(project=project).order_by("-id").first()
        self.assertIn(f"/chats/{chat.id}/", resp["Location"])
        # idle: no turn yet; marked succeeded so the composer stays enabled; untitled
        self.assertEqual(chat.turns.count(), 0)
        self.assertEqual(chat.status, RunStatus.SUCCEEDED)
        self.assertEqual(chat.title, "")
        # the logs API reports it as a 0-turn ("ready") chat
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertEqual(data["model_run"]["iteration"], 0)
        self.assertEqual(data["model_run"]["status"], RunStatus.SUCCEEDED)

    def test_new_chat_rejects_get(self):
        project = self._project()
        self.assertEqual(self.client.get(f"/projects/{project.id}/chats/new/").status_code, 405)

    def test_first_message_titles_the_chat(self):
        from dashboard.models import Chat

        project = self._project()
        chat = Chat.objects.create(project=project, title="", status=RunStatus.SUCCEEDED, settings={})
        prompt = "Build a todo app with due dates and labels for filtering tasks quickly"
        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{chat.id}/send/", {"prompt": prompt})
        self.assertEqual(resp.status_code, 200)
        start_turn.assert_called_once()
        chat.refresh_from_db()
        self.assertEqual(chat.turns.count(), 1)
        self.assertEqual(chat.title, prompt[:60])

    def test_switcher_lists_sibling_chats(self):
        from dashboard.models import Chat

        project = self._project()
        Chat.objects.create(project=project, title="Alpha chat", status=RunStatus.SUCCEEDED, settings={})
        current = Chat.objects.create(project=project, title="Beta chat", status=RunStatus.SUCCEEDED, settings={})
        html = self.client.get(f"/chats/{current.id}/").content.decode()
        self.assertIn("chat-switch-btn", html)
        self.assertIn("Alpha chat", html)
        self.assertIn(f"/projects/{project.id}/chats/new/", html)


class ProjectPromptWriterDrawerTests(TestCase):
    """Phase 2: the docked Prompt Writer drawer (a mode=prompt_writer chat on the project)."""

    def _project(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="PW", slug=project_sandbox.unique_project_slug("PW"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        return project

    def test_endpoint_get_or_creates_single_writer_chat(self):
        from dashboard.models import Chat

        project = self._project()
        a = self.client.get(f"/projects/{project.id}/prompt-writer/").json()
        b = self.client.get(f"/projects/{project.id}/prompt-writer/").json()
        self.assertEqual(a["chat_id"], b["chat_id"])  # idempotent — no duplicate writer chats
        writers = [c for c in Chat.objects.filter(project=project) if (c.settings or {}).get("mode") == "prompt_writer"]
        self.assertEqual(len(writers), 1)
        w = writers[0]
        self.assertEqual(w.status, RunStatus.SUCCEEDED)  # idle until the user sends an idea
        self.assertEqual(w.turns.count(), 0)
        self.assertEqual(a["logs_url"], f"/api/chats/{w.id}/logs/")
        self.assertEqual(a["send_url"], f"/chats/{w.id}/send/")

    def test_writer_chat_excluded_from_switcher_but_drawer_present(self):
        from dashboard.models import Chat

        project = self._project()
        main = Chat.objects.create(project=project, title="Main work", status=RunStatus.SUCCEEDED, settings={})
        self.client.get(f"/projects/{project.id}/prompt-writer/")  # creates the "Prompt writer" chat
        html = self.client.get(f"/chats/{main.id}/").content.decode()
        self.assertIn("pw-drawer", html)
        switch = html.split("chat-switch-list")[1].split("</div>")[0]
        self.assertIn("Main work", switch)
        self.assertNotIn("Prompt writer", switch)  # the writer chat is not a switchable sibling

    def test_sending_into_writer_runs_a_turn(self):
        from dashboard.models import Chat

        project = self._project()
        wid = self.client.get(f"/projects/{project.id}/prompt-writer/").json()["chat_id"]
        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{wid}/send/", {"prompt": "a blog with posts and comments"})
        self.assertEqual(resp.status_code, 200)
        start_turn.assert_called_once()
        self.assertEqual(Chat.objects.get(id=wid).turns.count(), 1)


class ProjectResourcesTotalsTests(TestCase):
    """Phase 3: chat_resources_api aggregates project-wide totals (excl. the Prompt Writer chat)."""

    def test_totals_aggregate_across_chats_excluding_prompt_writer(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="Tot", slug="tot-proj", origin=Project.Origin.NEW, sandbox_path="")
        a = Chat.objects.create(project=project, title="A", status=RunStatus.SUCCEEDED, settings={})
        b = Chat.objects.create(project=project, title="B", status=RunStatus.SUCCEEDED, settings={})
        pw = Chat.objects.create(project=project, title="Prompt writer", status=RunStatus.SUCCEEDED,
                                 settings={"mode": "prompt_writer"})
        ModelRun.objects.create(chat=a, model_name="agent", status=RunStatus.SUCCEEDED,
                                prompt_tokens=100, completion_tokens=200, duration_seconds=12.5)
        ModelRun.objects.create(chat=b, model_name="agent", status=RunStatus.SUCCEEDED,
                                prompt_tokens=50, completion_tokens=70, duration_seconds=7.5)
        ModelRun.objects.create(chat=pw, model_name="agent", status=RunStatus.SUCCEEDED,
                                prompt_tokens=999, completion_tokens=999, duration_seconds=99)  # excluded
        t = self.client.get(f"/api/chats/{a.id}/resources/").json()["project_totals"]
        self.assertEqual(t["chats"], 2)       # the prompt-writer chat is excluded
        self.assertEqual(t["messages"], 2)    # its refiner turn is excluded
        self.assertEqual(t["prompt_tokens"], 150)
        self.assertEqual(t["completion_tokens"], 270)
        self.assertAlmostEqual(t["agent_seconds"], 20.0, places=2)


class NewChatComposerTests(TestCase):
    """The New-chat (home) landing: chat-composer with model/effort/thinking selectors."""

    def test_home_renders_composer_with_selectors(self):
        AIModel.objects.create(name="coder-x", is_active=True, execution_order=1)
        html = self.client.get("/").content.decode()
        self.assertIn('id="home-composer"', html)
        self.assertIn('name="model"', html)
        self.assertIn("coder-x", html)           # active models populate the model picker
        self.assertIn('name="effort"', html)
        self.assertIn('name="thinking"', html)

    def test_composer_post_creates_chat_with_selected_settings(self):
        from dashboard.models import AIModel, Chat

        m = AIModel.objects.create(name="coder-y", is_active=True, execution_order=1)
        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post("/projects/new/", {
                "prompt": "Build a wiki", "model": str(m.id), "effort": "high",
                "thinking": "on", "pursue_goal": "on",
            })
        self.assertEqual(resp.status_code, 302)
        start_turn.assert_called_once()
        chat = Chat.objects.select_related("project").order_by("-id").first()
        self.addCleanup(chat.project.delete)  # remove the created sandbox dir
        self.assertEqual(chat.settings.get("model"), str(m.id))
        self.assertEqual(chat.settings.get("effort"), "high")
        self.assertTrue(chat.settings.get("thinking"))

    def test_new_project_does_not_block_on_the_llm_namer(self):
        # Regression: naming a new project must NOT call generate_project_name (a blocking Ollama
        # call) in the request thread — that stalled the POST on a model load ("stuck on main screen").
        from dashboard.models import Chat

        with patch("dashboard.services.project_sandbox.generate_project_name",
                   side_effect=AssertionError("must not LLM-name a project in the request path")), \
                patch("dashboard.services.chat_runner.start_turn"):
            resp = self.client.post("/projects/new/", {"prompt": "Build a task reminder website", "from": "home"})
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.select_related("project").order_by("-id").first()
        self.addCleanup(chat.project.delete)
        self.assertTrue(chat.project.name)               # named instantly, no LLM
        self.assertIn("Build", chat.project.name)         # derived from the prompt

    def test_empty_prompt_from_home_redirects_home(self):
        # whitespace-only / JS-disabled submit from the home composer falls back to home, not the full form
        resp = self.client.post("/projects/new/", {"prompt": "   ", "from": "home"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")

    def test_empty_prompt_from_full_form_rerenders_with_error(self):
        # the standalone New-Project form keeps its inline-error behaviour (no `from=home` marker)
        resp = self.client.post("/projects/new/", {"prompt": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Please enter a prompt", resp.content.decode())


class ComposerMenusTests(TestCase):
    """Composer + (Upload/Add context) menu + / actions palette and their backing endpoints."""

    def _project_chat(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="CM", slug=project_sandbox.unique_project_slug("CM"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Work", status=RunStatus.SUCCEEDED,
                                   settings={"effort": "high", "thinking": True})
        return project, chat

    def test_chat_settings_update_persists(self):
        _, chat = self._project_chat()
        resp = self.client.post(f"/chats/{chat.id}/settings/", {"effort": "low", "thinking": "", "model": ""},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertEqual(chat.settings.get("effort"), "low")
        self.assertFalse(chat.settings.get("thinking"))

    def test_chat_clear_deletes_turns(self):
        _, chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.SUCCEEDED, user_prompt="x")
        resp = self.client.post(f"/chats/{chat.id}/clear/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ModelRun.objects.filter(chat=chat).count(), 0)

    def test_clear_blocked_while_running(self):
        _, chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")
        resp = self.client.post(f"/chats/{chat.id}/clear/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(ModelRun.objects.filter(chat=chat).count(), 1)

    def test_upload_namespaced_under_uploads(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        project, _ = self._project_chat()
        up = SimpleUploadedFile("note.txt", b"hello", content_type="text/plain")
        resp = self.client.post(f"/api/projects/{project.id}/upload/", {"file": up},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["path"], "uploads/note.txt")
        self.assertTrue((Path(project.sandbox_path) / "uploads" / "note.txt").exists())

    def test_upload_cannot_overwrite_scaffold(self):
        # a file literally named manage.py must NOT land at the sandbox root (it would be executed)
        from django.core.files.uploadedfile import SimpleUploadedFile

        project, _ = self._project_chat()
        (Path(project.sandbox_path) / "manage.py").write_text("# scaffold\n", encoding="utf-8")
        up = SimpleUploadedFile("manage.py", b"import os; os.system('evil')", content_type="text/x-python")
        self.client.post(f"/api/projects/{project.id}/upload/", {"file": up}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual((Path(project.sandbox_path) / "manage.py").read_text(encoding="utf-8"), "# scaffold\n")
        self.assertTrue((Path(project.sandbox_path) / "uploads" / "manage.py").exists())

    def test_settings_rejects_unknown_model(self):
        _, chat = self._project_chat()
        self.client.post(f"/chats/{chat.id}/settings/", {"model": "999999"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        chat.refresh_from_db()
        self.assertEqual(chat.settings.get("model"), "")   # unknown pk coerced to Auto, not stored

    def test_create_turn_tolerates_stale_model_pk(self):
        # a chat whose configured model was later deleted must not 500 on the next turn
        from dashboard.views import _create_turn

        _, chat = self._project_chat()
        chat.settings = {"model": "88888"}
        chat.save()
        turn = _create_turn(chat, "hi")  # would raise IntegrityError if it wrote a dangling FK
        self.assertIsNone(turn.ai_model_id)
        self.assertEqual(turn.model_name, "agent")

    def test_files_api_lists_sandbox(self):
        project, _ = self._project_chat()
        (Path(project.sandbox_path) / "app.py").write_text("print(1)\n", encoding="utf-8")
        resp = self.client.get(f"/api/projects/{project.id}/files/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("app.py", resp.json()["files"])

    def test_chat_palette_has_context_and_model_sections(self):
        _, chat = self._project_chat()
        html = self.client.get(f"/chats/{chat.id}/").content.decode()
        self.assertIn("cm-actions-btn", html)
        self.assertIn("Attach file", html)          # Context section (chat only)
        self.assertIn("Clear conversation", html)
        self.assertIn("Switch model", html)          # Model section

    def test_home_palette_omits_context_section(self):
        AIModel.objects.create(name="cm-x", is_active=True, execution_order=1)
        palette = self.client.get("/").content.decode().split('data-cm="palette"')[1].split("</form>")[0]
        self.assertIn("Switch model", palette)       # Model section present on home
        self.assertNotIn("Attach file", palette)     # Context section omitted on home
        self.assertNotIn("Clear conversation", palette)


class ChatStopTests(TestCase):
    """The composer Stop button: cancel a running turn; the agent aborts cooperatively."""

    def _chat(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="Stop", slug="stop-proj", origin=Project.Origin.NEW, sandbox_path="")
        return Chat.objects.create(project=project, title="w", status=RunStatus.RUNNING, settings={})

    def test_stop_cancels_running_turn(self):
        chat = self._chat()
        turn = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")
        resp = self.client.post(f"/chats/{chat.id}/stop/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["stopped"], 1)
        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.CANCELLED)
        self.assertEqual(chat.status, RunStatus.CANCELLED)

    def test_stop_with_nothing_running_is_noop(self):
        chat = self._chat()
        ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.SUCCEEDED, user_prompt="x")
        resp = self.client.post(f"/chats/{chat.id}/stop/", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.json()["stopped"], 0)

    def test_stream_aborts_when_turn_cancelled(self):
        from unittest.mock import Mock
        from dashboard.services.agent_runner import AgentModelRunner, TurnCancelled

        chat = self._chat()
        turn = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.CANCELLED, user_prompt="x")

        class FakeAgent:
            def stream(self, state, config=None, stream_mode=None):
                yield {"router": {"did_think": True}}
                yield {"router": {"did_think": True}}

        _, exc = AgentModelRunner(Mock())._stream(FakeAgent(), {"k": 1}, turn)
        self.assertIsInstance(exc, TurnCancelled)

    def test_composer_has_stop_and_send_buttons(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(name="SB", slug=project_sandbox.unique_project_slug("SB"), origin=Project.Origin.NEW)
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="w", status=RunStatus.SUCCEEDED, settings={})
        html = self.client.get(f"/chats/{chat.id}/").content.decode()
        self.assertIn("chat-stop-btn", html)
        self.assertIn("send-arrow", html)
        self.assertIn("data-stop-url", html)


class AuthSidebarTests(TestCase):
    """Sidebar account footer: Sign in / Sign up when signed out, avatar + name when signed in."""

    def test_signed_out_sidebar_shows_auth_links_not_github(self):
        html = self.client.get("/").content.decode()
        self.assertIn("/accounts/login/", html)
        self.assertIn("/accounts/signup/", html)
        self.assertNotIn("github.com", html)

    def test_signup_page_is_standalone_4step_wizard(self):
        html = self.client.get("/accounts/signup/").content.decode()
        self.assertIn("Kaip mums į jus kreiptis?", html)   # step 1
        self.assertIn("El. paštas", html)                  # step 2
        self.assertIn("Pakartokite slaptažodį", html)
        self.assertIn("gimimo data", html)                 # step 3 (title "Jūsų gimimo data")
        self.assertIn("Kur mus radote?", html)             # step 4
        self.assertIn("wiz-step", html)
        self.assertNotIn('class="sidebar"', html)          # the left menu is hidden on sign-up

    def test_signup_fields_carry_password_manager_autocomplete_tokens(self):
        """Chrome only offers a generated strong password when it can read the sign-up as one:
        a username field plus new-password fields."""
        html = self.client.get("/accounts/signup/").content.decode()
        self.assertIn('name="email" autocomplete="username"', html)
        self.assertEqual(html.count('type="password"'), 2)
        self.assertEqual(html.count('autocomplete="new-password"'), 2)

    def test_signup_creates_user_with_profile_and_signs_in(self):
        from django.contrib.auth import get_user_model

        resp = self.client.post("/accounts/signup/", {
            "name": "Researcher", "email": "r@example.com",
            "password1": "Testpass123!", "password2": "Testpass123!",
            "birthdate": "1990-05-01", "source": "search",
        })
        self.assertEqual(resp.status_code, 302)
        user = get_user_model().objects.get(username="r@example.com")   # email becomes the login id
        self.assertEqual(user.email, "r@example.com")
        self.assertEqual(user.first_name, "Researcher")
        self.assertEqual(str(user.profile.birthdate), "1990-05-01")     # step 3 persisted
        self.assertEqual(user.profile.source, "search")                 # step 4 persisted
        html = self.client.get("/").content.decode()
        self.assertIn("side-avatar", html)
        self.assertIn("Researcher", html)            # the chosen name, not the email, drives the avatar
        self.assertIn("Sign out", html)
        # The sidebar Sign-in button is gone once authed (the URL itself still ships inside the
        # sign-in gate popup's script, which is inert for a signed-in visitor).
        self.assertNotIn('class="side-auth-btn signin"', html)

    def test_signup_email_check_endpoint(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="taken@example.com", password="Testpass123!")
        self.assertTrue(self.client.get("/accounts/signup/email-check/?email=TAKEN@example.com").json()["taken"])
        self.assertFalse(self.client.get("/accounts/signup/email-check/?email=free@example.com").json()["taken"])
        self.assertFalse(self.client.get("/accounts/signup/email-check/").json()["taken"])

    def test_signup_bounce_explains_itself(self):
        """A rejected submit reopens the offending step — it must say why, from outside the steps,
        and warn that the passwords are gone."""
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="dup@example.com", password="Testpass123!")
        resp = self.client.post("/accounts/signup/", {
            "name": "Benas", "email": "dup@example.com",
            "password1": "Testpass123!", "password2": "Testpass123!",
            "birthdate": "1990-05-01", "source": "search",
        })
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Šis el. paštas jau užregistruotas.", html)
        self.assertIn("auth-nonfield", html)
        self.assertIn("Slaptažodžius reikės suvesti iš naujo.", html)

    def test_signup_rejects_password_mismatch(self):
        from django.contrib.auth import get_user_model

        resp = self.client.post("/accounts/signup/", {
            "name": "X", "email": "x@example.com",
            "password1": "Testpass123!", "password2": "Other123!",
            "birthdate": "1990-05-01", "source": "search",
        })
        self.assertEqual(resp.status_code, 200)       # re-rendered with the error
        self.assertFalse(get_user_model().objects.filter(username="x@example.com").exists())

    def test_login_then_logout(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="bob", password="Testpass123!")
        self.assertEqual(self.client.post("/accounts/login/", {"username": "bob", "password": "Testpass123!"}).status_code, 302)
        self.assertIn("side-avatar", self.client.get("/").content.decode())
        self.assertEqual(self.client.post("/accounts/logout/").status_code, 302)
        self.assertIn("/accounts/login/", self.client.get("/").content.decode())

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get("/accounts/logout/").status_code, 405)


class SettingsAndAccountMenuTests(TestCase):
    """The sidebar account menu (profile → Profile/Settings/Preferences/Help/Sign out) + the
    Settings page it opens, and the SVG nav icons."""

    def _login(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="bob@example.com", password="Testpass123!", first_name="Bob")
        self.client.post("/accounts/login/", {"username": "bob@example.com", "password": "Testpass123!"})

    def test_settings_requires_auth(self):
        resp = self.client.get("/settings/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_settings_page_has_three_tabs(self):
        self._login()
        html = self.client.get("/settings/").content.decode()
        for marker in ('data-tab="profile"', 'data-tab="preferences"', 'data-tab="help"',
                       'data-panel="profile"', "Keep the sidebar expanded",
                       "Reduce interface animations", "Keyboard shortcuts", "settings.js"):
            self.assertIn(marker, html)
        # full WAI-ARIA tablist wiring: tabs control labelled tabpanels
        self.assertIn('role="tabpanel"', html)
        self.assertIn('aria-controls="setpanel-profile"', html)
        self.assertIn('aria-labelledby="settab-profile"', html)
        # the profile tab is active by default; the email is shown
        self.assertIn("bob@example.com", html)

    def test_settings_tab_query_selects_active(self):
        self._login()
        resp = self.client.get("/settings/?tab=preferences")
        self.assertEqual(resp.context["active_tab"], "preferences")
        html = resp.content.decode()
        # the preferences panel is visible (no `hidden`) and the profile panel is hidden
        self.assertRegex(html, r'data-panel="preferences"(?:(?!hidden)[^>])*>')   # active panel: no hidden attr
        self.assertRegex(html, r'data-panel="profile"[^>]*\shidden')

    def test_settings_tab_query_rejects_unknown(self):
        self._login()
        self.assertEqual(self.client.get("/settings/?tab=bogus").context["active_tab"], "profile")

    def test_sidebar_account_menu_when_authenticated(self):
        self._login()
        html = self.client.get("/").content.decode()
        # the profile is a real button opening the menu, with the settings deep-links + hidden logout form
        self.assertIn('id="acct-btn"', html)
        self.assertIn('data-settings-url="/settings/"', html)
        self.assertIn('data-profile-url="/settings/?tab=profile"', html)
        self.assertIn('id="acct-signout-form"', html)
        # the old standalone "Sign out" sidebar button is gone (it lives in the menu now)
        self.assertNotIn('aria-label="Sign out"', html)

    def test_nav_uses_svg_icons_not_glyphs(self):
        html = self.client.get("/").content.decode()
        # the three main nav links carry animated inline SVGs, not the old unicode glyphs
        self.assertIn("ico-anim", html)
        self.assertIn("<svg", html)
        for glyph in ("▦", "❏", "⊞"):
            self.assertNotIn('<span class="side-ico">' + glyph, html)


class PlanningModeTests(TestCase):
    """The composer 'Planning mode' toggle drives the refiner behaviour (ask/plan, write no code)
    — without the standalone Prompt Writer's mode or its Create-project/benchmark hand-off."""

    def test_planning_toggle_blocks_code_and_skips_handoff(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _PlannerWritesCode(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                text = (
                    "## Plan\n**Step 1:** model the data\n\n"
                    "```python\n# blog/models.py\nfrom django.db import models\n"
                    "class Post(models.Model):\n    title = models.CharField(max_length=10)\n```\nDONE"
                )
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Plan", slug=project_sandbox.unique_project_slug("Plan"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        # Planning toggle ON, but NOT mode="prompt_writer".
        chat = Chat.objects.create(project=project, title="Plan", settings={"planning": True})
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="A blog", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _PlannerWritesCode):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        # planning is plan-only: the write directive was dropped → nothing landed in the sandbox
        self.assertFalse((Path(project.sandbox_path) / "blog" / "models.py").exists())
        # and a planning toggle (vs prompt_writer mode) stores no structured_prompt → no draft hand-off
        self.assertNotIn("structured_prompt", (chat.settings or {}))
        # ...it stores the finished plan instead, which drives the plan modal (Implement / Edit)
        self.assertIn("Plan", (chat.settings or {}).get("plan", ""))
        self.assertNotIn("plan_approved", (chat.settings or {}))  # not approved until the user clicks Implement
        self.assertTrue(chat.settings.get("planning"))


class PlanningBatchQuestionsTests(TransactionTestCase):
    """Planning asks all its questions AT ONCE: the batch creates N pending questions (with rich
    options), blocks until every one is answered, then returns the answers in order."""

    def test_batch_creates_all_questions_and_returns_answers(self):
        import time as _time

        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(name="Batch", slug="batch-q", origin=Project.Origin.NEW, sandbox_path="")
        chat = Chat.objects.create(project=project, title="Batch", settings={"planning": True})
        turn = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")

        class _StubRunner:
            def _command(self, *a, **k):
                pass

        questions = [
            {"prompt": "What kind of thing are we planning?",
             "options": ["Feature (Recommended) — define behavior, files, tests", "Bug fix", "Refactor"]},
            {"prompt": "How is it organized?", "options": ["By project", "Flat list"]},
        ]
        result = {}

        def run():
            with patch.object(ChatTurnRunner, "_maybe_start_preview", lambda *a, **k: None):
                result["answers"] = ChatTurnRunner()._handle_question_batch(_StubRunner(), turn, questions)

        t = threading.Thread(target=run)
        t.start()
        try:
            pis = []
            for _ in range(60):
                pis = list(PendingInteraction.objects.filter(
                    model_run=turn, status=PendingInteraction.Status.PENDING).order_by("id"))
                if len(pis) == 2:
                    break
                _time.sleep(0.05)
            self.assertEqual(len(pis), 2)                       # both questions created up front
            # the "(Recommended)" marker is a FLAG, not label text — the card shows a clean label
            self.assertEqual(pis[0].options[0]["label"], "Feature")
            self.assertTrue(pis[0].options[0]["recommended"])   # rich options for the card
            self.assertEqual(pis[0].options[0]["description"], "define behavior, files, tests")
            for pi, ans in zip(pis, ["Feature", "By project"]):
                pi.status = PendingInteraction.Status.ANSWERED
                pi.answer = ans
                pi.save(update_fields=["status", "answer"])
        finally:
            t.join(timeout=8)
        self.assertEqual(result.get("answers"), ["Feature", "By project"])


class PlanningUpfrontQuestionsTests(TransactionTestCase):
    """Planning generates ALL its questions in ONE go: the first round makes a single dedicated
    call that lists every question, so they appear as one batch (not one-by-one), then the agent
    writes the plan with the answers in hand."""

    def test_first_round_asks_every_question_at_once_then_plans(self):
        import time as _time

        from django.utils import timezone

        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _BatchThenPlanLLM(LLMBackend):
            """Call 1 (the dedicated question pass) lists THREE questions at once; later calls (the
            agent writing the plan) return the spec. This is exactly the shape a real model takes
            when asked to 'list every question now'."""

            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"
                self.calls = 0

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                self.calls += 1
                if self.calls == 1:
                    text = (
                        "ASK: Who manages the data?\n- Admin (Recommended) — full control\n- Team — shared\n\n"
                        "ASK: How are tasks organized?\n- By project (Recommended) — grouped\n- Flat list — simple\n\n"
                        "ASK: Which statuses are needed?\n- Todo/Doing/Done (Recommended) — kanban\n- Open/Closed — simple"
                    )
                else:
                    text = "## Plan\n**Overview:** A task manager.\n**Features:**\n- statuses, due dates\nDONE"
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Up", slug=project_sandbox.unique_project_slug("Up"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Up", settings={"mode": "prompt_writer"})
        run = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="A task manager", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _BatchThenPlanLLM):
            worker = threading.Thread(target=lambda: ChatTurnRunner().run_turn(chat.id, run.id), daemon=True)
            worker.start()
            pendings = []
            for _ in range(150):  # wait for the questions to appear
                pendings = list(PendingInteraction.objects.filter(model_run=run, status="pending").order_by("id"))
                if len(pendings) >= 3:
                    break
                _time.sleep(0.1)
            # all THREE questions are created together (batched), not dribbled out one-by-one
            self.assertEqual(len(pendings), 3, "planning should ask every question in one batch")
            self.assertTrue(all(p.options for p in pendings), "each question carries options")
            for p in pendings:
                p.status = PendingInteraction.Status.ANSWERED
                p.answer = (p.options[0]["label"] if p.options else "ok")
                p.answered_at = timezone.now()
                p.save(update_fields=["status", "answer", "answered_at"])
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "the agent should resume and finish after the answers")

        run.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        # the agent wrote the plan from the answers (didn't loop back into more questions)
        self.assertIn("Plan", (chat.settings or {}).get("structured_prompt", ""))
        # no code was written (planning is plan-only)
        self.assertFalse((Path(project.sandbox_path) / "models.py").exists())
        # the dedicated question-generation call is folded into the turn telemetry (4 tokens from
        # that call + >=4 from the agent's plan call) — not silently dropped
        self.assertGreaterEqual(run.prompt_tokens, 8, "the upfront question call's tokens are counted")

    def test_stop_during_question_batch_aborts_promptly(self):
        """Pressing Stop while the question batch is pending must abort the turn promptly (not spin
        until the 30-minute interaction timeout): Stop only flips ModelRun.status, so the batch poll
        loop has to notice CANCELLED and bail."""
        import time as _time

        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _AsksLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                text = ("ASK: Q1?\n- A (Recommended) — x\n- B — y\n\n"
                        "ASK: Q2?\n- C (Recommended) — x\n- D — y")
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Cancel", slug=project_sandbox.unique_project_slug("Cancel"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Cancel", settings={"planning": True})
        run = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="x", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _AsksLLM):
            worker = threading.Thread(target=lambda: ChatTurnRunner().run_turn(chat.id, run.id), daemon=True)
            worker.start()
            pendings = []
            for _ in range(150):
                pendings = list(PendingInteraction.objects.filter(model_run=run, status="pending"))
                if len(pendings) >= 2:
                    break
                _time.sleep(0.1)
            self.assertGreaterEqual(len(pendings), 2)
            # user presses Stop — chat_stop only flips the status; the questions stay PENDING
            ModelRun.objects.filter(id=run.id).update(status=RunStatus.CANCELLED)
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "the batch must abort on Stop, not wait for the timeout")

        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)


class _FakeAskLLM(LLMBackend):
    """Asks a clarifying question first (text ASK directive), then writes a model, then DONE."""

    name = "fake"
    supports_tools = False

    def __init__(self, model=None, host=None):
        self.model = model or "fake"
        self.calls = 0

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        from src.llm.LLMResponse import LLMResponse

        if _INTENT_PROMPT_MARKER in prompt:
            return _intent_build_response()
        self.calls += 1
        if self.calls == 1:
            text = "ASK: What fields should Post have?\n- Title only\n- Title and body"
        elif self.calls == 2:
            text = ("```python\n# blog/models.py\nfrom django.db import models\n\n"
                    "class Post(models.Model):\n    title = models.CharField(max_length=200)\n"
                    "    body = models.TextField()\n```")
        else:
            text = "Done.\nDONE"
        return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")


class ProjectChatInteractiveUnitTests(TestCase):
    """Unit-level checks for the interactive layer (no threads)."""

    def test_ask_node_calls_handler(self):
        from src.agent.nodes import make_tool_node

        node = make_tool_node("ask", interaction_handler=lambda kind, payload: "Title and body")
        out = node({"next_action": {"tool": "ask", "args": {"question": "Fields?", "options": ["A", "B"]}}, "messages": []})
        self.assertIn("Title and body", out["last_observation"])

    def test_ask_node_propagates_turn_cancelled_on_stop(self):
        """Pressing Stop surfaces as TurnCancelled from the interaction handler. The ask node must let
        it PROPAGATE (so the turn aborts immediately) rather than swallowing it into a benign
        'ERROR asking the user' observation and burning another agent step."""
        from dashboard.services.agent_runner import TurnCancelled
        from src.agent.nodes import make_tool_node

        def handler(kind, payload):
            raise TurnCancelled()

        node = make_tool_node("ask", interaction_handler=handler)
        with self.assertRaises(TurnCancelled):  # single-question path
            node({"next_action": {"tool": "ask", "args": {"question": "F?", "options": ["A", "B"]}}, "messages": []})
        with self.assertRaises(TurnCancelled):  # batched-questions path
            node({"next_action": {"tool": "ask", "args": {"questions": [{"prompt": "Q1", "options": ["A", "B"]}]}}, "messages": []})

    def test_approval_gating_denies_risky_shell(self):
        from src.agent.nodes import make_tool_node

        node = make_tool_node("shell", interaction_handler=lambda kind, payload: False, approval_tools={"shell"})
        out = node({
            "next_action": {"tool": "shell", "args": {"command": "echo hi"}},
            "messages": [], "workspace_path": tempfile.mkdtemp(),
        })
        self.assertIn("denied", out["last_observation"].lower())

    def test_ask_user_hidden_without_handler(self):
        from src.agent.nodes import RouterNode

        class _Dummy:
            supports_tools = True

        names = {s["function"]["name"] for s in RouterNode(_Dummy(), interactive=False)._schemas}
        self.assertNotIn("ask_user", names)
        names_i = {s["function"]["name"] for s in RouterNode(_Dummy(), interactive=True)._schemas}
        self.assertIn("ask_user", names_i)

    def test_interaction_respond_api(self):
        from dashboard.models import Chat, PendingInteraction, Project

        project = Project.objects.create(name="I", slug="i-proj", sandbox_path="")
        chat = Chat.objects.create(project=project, title="I")
        run = ModelRun.objects.create(chat=chat, model_name="agent")
        q = PendingInteraction.objects.create(model_run=run, kind="question", prompt="?", status="pending")
        self.assertEqual(self.client.post(f"/api/interactions/{q.id}/respond/", {"answer": "Blue"}).status_code, 200)
        q.refresh_from_db()
        self.assertEqual(q.status, "answered")
        self.assertEqual(q.answer, "Blue")
        a = PendingInteraction.objects.create(model_run=run, kind="approval", command="rm x", status="pending")
        self.client.post(f"/api/interactions/{a.id}/respond/", {"decision": "deny"})
        a.refresh_from_db()
        self.assertEqual(a.status, "denied")

    def test_ask_parsing_guards(self):
        from src.agent.tools import parse_actions_ordered

        # An ASK: line INSIDE a code block is file content, not a real question.
        code = "```python\n# blog/x.py\nASK: how many?\n- a\n- b\n```"
        self.assertFalse(any(x["tool"] == "ask" for x in parse_actions_ordered(code, interactive=True)))
        # Benchmark mode (interactive=False) never produces an ask.
        self.assertFalse(any(x["tool"] == "ask" for x in parse_actions_ordered("ASK: x?\n- a", interactive=False)))
        # Interactive top-level ASK is parsed; options are capped.
        bullets = "\n".join("- option %d" % i for i in range(10))
        acts = parse_actions_ordered("ASK: pick one\n" + bullets, interactive=True)
        ask = next(x for x in acts if x["tool"] == "ask")
        self.assertLessEqual(len(ask["args"]["options"]), 6)

    def test_chat_send_rejects_concurrent_turn(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="C", slug="c-proj", sandbox_path="")
        chat = Chat.objects.create(project=project, title="C")
        ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING)
        resp = self.client.post(f"/chats/{chat.id}/send/", {"prompt": "hi"})
        self.assertEqual(resp.status_code, 409)


class ProjectSandboxSafetyTests(TestCase):
    """Sandbox lifecycle safety: deletion wipes the dir, imports skip symlinks."""

    def test_project_delete_removes_sandbox(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(name="Del", slug=project_sandbox.unique_project_slug("Del"), origin="new")
        path = project_sandbox.create_sandbox(project)
        (path / "marker.txt").write_text("x", encoding="utf-8")
        self.assertTrue(path.exists())
        project.delete()
        self.assertFalse(path.exists())

    def test_import_path_skips_symlinks(self):
        import os
        import types

        from dashboard.services import project_sandbox

        src = Path(tempfile.mkdtemp(prefix="src_"))
        (src / "real.py").write_text("x = 1\n", encoding="utf-8")
        outside = Path(tempfile.mkdtemp(prefix="out_")) / "secret.txt"
        outside.write_text("TOPSECRET", encoding="utf-8")
        try:
            os.symlink(str(outside), str(src / "leak.txt"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks not permitted on this platform")
        sandbox = Path(tempfile.mkdtemp(prefix="sbx_"))
        result = project_sandbox.import_path(types.SimpleNamespace(sandbox_path=str(sandbox)), str(src))
        self.assertTrue(result["ok"])
        self.assertTrue((sandbox / "real.py").exists())
        self.assertFalse((sandbox / "leak.txt").exists(), "a symlink must not be dereferenced into the sandbox")


class ProjectVenvAndFilesTests(TestCase):
    """Per-project venv path resolution + the Files tab showing the Django scaffold."""

    def test_venv_path_resolution_gated_on_marker(self):
        import os

        from dashboard.services import project_venv

        ws = Path(tempfile.mkdtemp(prefix="venv_"))
        self.assertIsNone(project_venv.venv_python(ws))           # no venv yet
        self.assertNotIn("VIRTUAL_ENV", project_venv.shell_env(ws))

        bin_dir = ws / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        bin_dir.mkdir(parents=True)
        pyexe = bin_dir / ("python.exe" if os.name == "nt" else "python")
        pyexe.write_text("", encoding="utf-8")
        self.assertIsNone(project_venv.venv_python(ws))           # still not "ready" (no marker)

        (ws / ".venv" / ".kursinis_ready").write_text("ready", encoding="utf-8")
        self.assertEqual(project_venv.venv_python(ws), str(pyexe))
        env = project_venv.shell_env(ws)
        self.assertEqual(env["VIRTUAL_ENV"], str(ws / ".venv"))
        self.assertTrue(env["PATH"].startswith(str(bin_dir)))

    def test_ensure_venv_leaves_no_ready_marker_when_pip_install_fails(self):
        """A transient pip failure must NOT mark the venv ready, so migrate/runserver keep falling
        back to sys.executable (which has Django) instead of a package-less venv."""
        import os

        from dashboard.services import project_venv

        ws = Path(tempfile.mkdtemp(prefix="venvfail_"))
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)

        def fake_run(cmd, **kwargs):
            result = Mock()
            result.stdout = result.stderr = ""
            if "venv" in cmd:  # python -m venv <dir> — materialise the interpreter so exe.exists()
                bin_dir = project_venv._bin_dir(ws)
                bin_dir.mkdir(parents=True, exist_ok=True)
                (bin_dir / ("python.exe" if os.name == "nt" else "python")).write_text("", encoding="utf-8")
                result.returncode = 0
            else:  # pip install — simulate a non-zero (failed) install
                result.returncode = 1
            return result

        with patch("dashboard.services.project_venv.subprocess.run", side_effect=fake_run):
            result = project_venv.ensure_venv(ws)

        self.assertFalse(result["ok"])
        self.assertFalse(project_venv.is_ready(ws))         # no marker written
        self.assertIsNone(project_venv.venv_python(ws))     # so callers fall back to sys.executable

    def test_list_sandbox_files_includes_scaffold_and_migrations(self):
        from dashboard.views import _list_sandbox_files

        ws = Path(tempfile.mkdtemp(prefix="files_"))
        (ws / "manage.py").write_text("x", encoding="utf-8")
        (ws / "settings.py").write_text("x", encoding="utf-8")
        (ws / "blog" / "migrations").mkdir(parents=True)
        (ws / "blog" / "migrations" / "0001_initial.py").write_text("x", encoding="utf-8")
        (ws / ".venv" / "Lib").mkdir(parents=True)
        (ws / ".venv" / "Lib" / "big.py").write_text("x", encoding="utf-8")
        paths = {f["path"] for f in _list_sandbox_files(ws)}
        self.assertIn("manage.py", paths)
        self.assertIn("settings.py", paths)
        self.assertIn("blog/migrations/0001_initial.py", paths)  # migrations now shown
        self.assertNotIn(".venv/Lib/big.py", paths)              # venv stays hidden


class ProjectChatBlockResumeTests(TransactionTestCase):
    """End-to-end: the agent blocks on ask_user and resumes when the user answers."""

    def test_ask_user_blocks_then_resumes(self):
        import time

        from django.utils import timezone

        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(
            name="Blk", slug=project_sandbox.unique_project_slug("Blk"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Blk")
        run = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="Build a Post model", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _FakeAskLLM), \
                patch("dashboard.services.project_venv.ensure_venv", lambda *a, **k: {"ok": True, "built": False}):
            worker = threading.Thread(target=lambda: ChatTurnRunner().run_turn(chat.id, run.id), daemon=True)
            worker.start()
            pending = None
            for _ in range(150):  # up to ~15s for the question to appear
                pending = PendingInteraction.objects.filter(model_run=run, status="pending").first()
                if pending:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(pending, "the agent should have asked a clarifying question")
            self.assertEqual(pending.kind, "question")
            self.assertTrue(pending.options, "the question should carry options")
            pending.status = PendingInteraction.Status.ANSWERED
            pending.answer = "Title and body"
            pending.answered_at = timezone.now()
            pending.save(update_fields=["status", "answer", "answered_at"])
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "the agent should resume after the answer")

        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        self.assertTrue((Path(project.sandbox_path) / "blog" / "models.py").exists())


class _FakePromptWriterLLM(LLMBackend):
    """Refiner model: immediately finishes with a structured prompt (no code, no questions)."""

    name = "fake"
    supports_tools = False

    def __init__(self, model=None, host=None):
        self.model = model or "fake"
        self.calls = 0

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        from src.llm.LLMResponse import LLMResponse

        self.calls += 1
        text = (
            "## Task Tracker\n"
            "**Overview:** A small app to track personal tasks.\n"
            "**Features:**\n- Create and complete tasks\n"
            "**Data models:**\n- Task: title, done\n"
            "**Constraints:** none\n"
            "DONE"
        )
        return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")


class _FakeTestPlannerLLM(LLMBackend):
    """Returns a fenced pytest block for reference-test generation."""

    name = "fake"
    supports_tools = False

    def __init__(self, model=None, host=None):
        self.model = model or "fake"

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        from src.llm.LLMResponse import LLMResponse

        text = (
            "Here are the tests:\n\n```python\n"
            "import pytest\n\n@pytest.mark.django_db\n"
            "def test_task_round_trips():\n    assert True\n```\n"
        )
        return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")


class PromptWriterTests(TestCase):
    """Phase D — refiner chat → structured prompt → hand-off to Project / Benchmark."""

    def test_generate_reference_tests_extracts_pytest_block(self):
        from dashboard.services.test_planner import generate_reference_tests

        code = generate_reference_tests("Build a task tracker", backend=_FakeTestPlannerLLM())
        self.assertIn("import pytest", code)
        self.assertIn("test_task_round_trips", code)
        self.assertNotIn("```", code)

    def test_generate_reference_tests_swallows_failures(self):
        from dashboard.services.test_planner import generate_reference_tests

        class _Boom(LLMBackend):
            name = "boom"
            supports_tools = False

            def complete(self, *a, **k):
                raise RuntimeError("offline")

        self.assertEqual(generate_reference_tests("anything", backend=_Boom()), "")

    def test_generate_reference_tests_rejects_non_python(self):
        """Prose or a fenced-but-unparsable block must yield "" so it never lands as test_reference.py
        and aborts the whole pytest collection."""
        from dashboard.services.test_planner import generate_reference_tests

        def _llm(text):
            from src.llm.LLMResponse import LLMResponse

            class _Fixed(LLMBackend):
                name = "fixed"
                supports_tools = False

                def complete(self, *a, **k):
                    return LLMResponse(text=text, prompt_tokens=1, completion_tokens=1, latency_sec=0.0, model="fixed")

            return _Fixed()

        self.assertEqual(generate_reference_tests("x", backend=_llm("Here are some tests you could add.")), "")
        self.assertEqual(generate_reference_tests("x", backend=_llm("```python\ndef test( :\n  pass\n```")), "")
        self.assertIn("def test_ok", generate_reference_tests("x", backend=_llm("```python\ndef test_ok():\n    assert True\n```")))

    def test_refiner_mode_structurally_drops_code_writes(self):
        """A non-compliant refiner that emits a code block must NOT write into the sandbox: the
        allowed_tools={'ask'} whitelist drops the write, yet the structured prompt is still captured."""
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _RefinerWritesCode(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                text = (
                    "## App\n**Overview:** x\n\n"
                    "```python\n# blog/models.py\nfrom django.db import models\n"
                    "class Post(models.Model):\n    title = models.CharField(max_length=10)\n```\n"
                    "DONE"
                )
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Refiner", slug=project_sandbox.unique_project_slug("Refiner"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Refiner", settings={"mode": "prompt_writer"})
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="A blog", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _RefinerWritesCode):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        # the write directive was dropped — nothing landed in the sandbox
        self.assertFalse((Path(project.sandbox_path) / "blog" / "models.py").exists())
        # but the structured prompt was still captured for hand-off
        self.assertIn("App", (chat.settings or {}).get("structured_prompt", ""))

    def test_prompt_writer_followup_does_not_force_new_questions(self):
        """A follow-up turn (after prior questions) must NOT force a fresh question — the ask floor is
        conversation-level (seeded from prior turns), so the refiner can finish with the spec."""
        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _SpecLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                text = "## Planner\n**Overview:** A task planner.\n**Features:**\n- track tasks\nDONE"
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="PW", slug=project_sandbox.unique_project_slug("PW"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="PW", settings={"mode": "prompt_writer"})
        # a PRIOR turn that already asked a clarifying question (seeds the conversation question count)
        prior = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="idea", status=RunStatus.SUCCEEDED)
        PendingInteraction.objects.create(
            model_run=prior, kind=PendingInteraction.Kind.QUESTION, prompt="Q1?",
            status=PendingInteraction.Status.ANSWERED, answer="A",
        )
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="continue", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _SpecLLM):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        # no NEW question was forced on the follow-up (the floor saw prior asks via the seed)
        self.assertEqual(PendingInteraction.objects.filter(model_run=turn, kind=PendingInteraction.Kind.QUESTION).count(), 0)
        # and it finished WITH the structured prompt
        self.assertIn("Planner", (chat.settings or {}).get("structured_prompt", ""))

    def test_prompt_writer_page_renders_and_creates_draft_chat(self):
        from dashboard.models import Chat

        self.assertEqual(self.client.get("/prompt-writer/").status_code, 200)
        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post("/prompt-writer/", {"prompt": "A blog with posts", "effort": "medium"})
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.latest("id")
        self.addCleanup(chat.project.delete)  # removes the sandbox dir
        self.assertEqual((chat.settings or {}).get("mode"), "prompt_writer")
        self.assertIn("/chats/", resp.url)
        start_turn.assert_called_once()

    def test_prompt_writer_turn_stores_structured_prompt_and_builds_nothing(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(
            name="Idea", slug=project_sandbox.unique_project_slug("Idea"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Idea", settings={"mode": "prompt_writer"})
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="A task tracker", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _FakePromptWriterLLM):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        chat.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        self.assertIn("Task Tracker", (chat.settings or {}).get("structured_prompt", ""))
        # the refiner writes no code — the sandbox holds no app package
        self.assertFalse((Path(project.sandbox_path) / "blog").exists())
        # the chat logs API exposes the ready draft (incl. the structured prompt, which the
        # in-project Prompt Writer drawer drops into the composer) so the hand-off can reveal
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertEqual(data["draft"]["mode"], "prompt_writer")
        self.assertTrue(data["draft"]["ready"])
        self.assertIn("Task Tracker", data["draft"]["structured_prompt"])

    def test_create_from_draft_project_spawns_build_chat(self):
        from dashboard.models import Chat, Project

        project = Project.objects.create(name="Idea", slug="idea-proj", origin=Project.Origin.NEW, sandbox_path="")
        chat = Chat.objects.create(
            project=project, title="Idea",
            settings={"mode": "prompt_writer", "structured_prompt": "## Shop\n**Features:**\n- cart"},
        )
        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{chat.id}/create/project/")
        self.assertEqual(resp.status_code, 302)
        new_chat = Chat.objects.exclude(id=chat.id).latest("id")
        self.addCleanup(new_chat.project.delete)
        # the new build chat is a normal (non-refiner) chat carrying the structured prompt
        self.assertNotIn("mode", new_chat.settings or {})
        first_turn = ModelRun.objects.filter(chat=new_chat).first()
        self.assertEqual(first_turn.user_prompt, "## Shop\n**Features:**\n- cart")
        start_turn.assert_called_once()


class TasksToolTests(TestCase):
    """The agent `update_tasks` tool: parsing, normalisation, and end-to-end surfacing."""

    def test_normalize_tasks_coerces_shapes_and_statuses(self):
        from src.agent.tools import normalize_tasks

        out = normalize_tasks([
            "bare string",
            {"content": "do it", "status": "doing"},
            {"content": "", "status": "pending"},       # dropped (empty)
            {"task": "alt key", "status": "weird"},      # unknown status → pending
            {"content": "finished", "status": "done"},
        ])
        self.assertEqual(out, [
            {"content": "bare string", "status": "pending"},
            {"content": "do it", "status": "in_progress"},
            {"content": "alt key", "status": "pending"},
            {"content": "finished", "status": "completed"},
        ])
        self.assertEqual(normalize_tasks("not a list"), [])

    def test_parse_actions_ordered_parses_task_block(self):
        from src.agent.tools import parse_actions_ordered

        text = "Here is my plan.\nTASKS:\n- [x] Design models\n- [>] Write views\n- [ ] Wire urls\n"
        actions = parse_actions_ordered(text, interactive=False)
        tasks_actions = [a for a in actions if a["tool"] == "tasks"]
        self.assertEqual(len(tasks_actions), 1)
        tasks = tasks_actions[0]["args"]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["completed", "in_progress", "pending"])
        self.assertEqual(tasks[1]["content"], "Write views")

    def test_task_block_inside_code_fence_is_ignored(self):
        from src.agent.tools import parse_actions_ordered

        text = "```python\n# notes.py\n# TASKS:\n# - [ ] not a real task\n```\n"
        actions = parse_actions_ordered(text, interactive=False)
        self.assertFalse([a for a in actions if a["tool"] == "tasks"])

    def test_last_task_block_wins_when_a_turn_has_two(self):
        """Latest-wins (matches views._latest_tasks): a corrected second block supersedes the first."""
        from src.agent.tools import _parse_tasks

        text = (
            "TASKS:\n- [ ] old one\n- [ ] old two\n\n"
            "On reflection, here is the corrected list:\n"
            "TASKS:\n- [x] new A\n- [>] new B\n"
        )
        tasks = _parse_tasks(text)
        self.assertEqual([t["content"] for t in tasks], ["new A", "new B"])
        self.assertEqual([t["status"] for t in tasks], ["completed", "in_progress"])


class ChatErrorSurfacingTests(TestCase):
    """A failed chat turn must explain WHY in the timeline (not just a bare 'Failed')."""

    def test_failed_turn_emits_error_card_with_hint(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _BoomLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, *a, **k):
                raise RuntimeError('model "qwen2.5-coder:7b-instruct" not found, try pulling it first')

            def chat(self, *a, **k):
                raise RuntimeError('model "qwen2.5-coder:7b-instruct" not found, try pulling it first')

        project = Project.objects.create(
            name="Boom", slug=project_sandbox.unique_project_slug("Boom"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Boom")
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="build something", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _BoomLLM), \
                patch("dashboard.services.project_venv.ensure_venv", lambda *a, **k: {"ok": True, "built": False}):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.FAILED)
        err = CommandLog.objects.filter(model_run=turn, agent="error").first()
        self.assertIsNotNone(err, "a failed turn should leave an error card in the timeline")
        self.assertIn("isn't installed", err.stdout)  # the model-not-found hint
        # and the chat logs API returns it so the front-end renders it
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertTrue(any(l["agent"] == "error" for l in data["logs"]))


class ChatRetryTests(TestCase):
    """The failure-recovery bar: retry / edit prompt / change model."""

    def _project_chat(self, settings=None):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Rt", slug=project_sandbox.unique_project_slug("Rt"), origin=Project.Origin.NEW, sandbox_path="",
        )
        return Chat.objects.create(project=project, title="Rt", settings=settings or {})

    def test_resolve_tag_honors_explicit_ollama_tag(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        chat = self._project_chat({"ollama_tag": "codellama:latest", "model": "999"})
        self.assertEqual(ChatTurnRunner()._resolve_tag(chat), "codellama:latest")

    def test_retry_persists_model_and_reuses_last_prompt(self):
        chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="Build a blog", status=RunStatus.FAILED)

        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{chat.id}/retry/", {"model": "codellama:latest"})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        chat.refresh_from_db()
        self.assertEqual((chat.settings or {}).get("ollama_tag"), "codellama:latest")
        new = ModelRun.objects.filter(chat=chat).order_by("-id").first()
        self.assertEqual(new.user_prompt, "Build a blog")          # reused the failed prompt
        self.assertEqual(new.model_name, "codellama:latest")        # turn reflects the chosen tag
        start_turn.assert_called_once()

    def test_retry_with_edited_prompt_and_auto_model_clears_tag(self):
        chat = self._project_chat({"ollama_tag": "codellama:latest"})
        ModelRun.objects.create(chat=chat, model_name="x", user_prompt="old prompt", status=RunStatus.FAILED)

        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{chat.id}/retry/", {"model": "", "prompt": "new edited prompt"})

        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertNotIn("ollama_tag", chat.settings or {})         # Auto clears the override
        new = ModelRun.objects.filter(chat=chat).order_by("-id").first()
        self.assertEqual(new.user_prompt, "new edited prompt")
        start_turn.assert_called_once()

    def test_chat_models_api_lists_installed_tags(self):
        chat = self._project_chat({"ollama_tag": "codellama:latest"})
        with patch("src.llm.OllamaBackend.OllamaBackend.list_installed",
                   return_value=["codellama:latest", "mistral:latest"]):
            resp = self.client.get(f"/api/chats/{chat.id}/models/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("codellama:latest", data["models"])
        self.assertEqual(data["current"], "codellama:latest")

    def test_retry_blocked_while_a_turn_is_running(self):
        chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="x", status=RunStatus.RUNNING)

        with patch("dashboard.services.chat_runner.start_turn") as start_turn:
            resp = self.client.post(f"/chats/{chat.id}/retry/", {"model": "codellama:latest"})

        self.assertEqual(resp.status_code, 409)
        start_turn.assert_not_called()

    def test_tasks_tool_node_updates_state(self):
        from src.agent.nodes import make_tool_node

        node = make_tool_node("tasks")
        out = node({
            "next_action": {"tool": "tasks", "args": {"tasks": [
                {"content": "A", "status": "completed"},
                {"content": "B", "status": "in_progress"},
            ]}},
            "messages": [],
        })
        self.assertEqual([t["status"] for t in out["tasks"]], ["completed", "in_progress"])

    def test_chat_turn_tasks_surface_in_logs_api(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        class _TasksLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"
                self.calls = 0

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                if _INTENT_PROMPT_MARKER in prompt:
                    return _intent_build_response()
                self.calls += 1
                if self.calls == 1:
                    text = "TASKS:\n- [x] Design models\n- [>] Write views\n- [ ] Wire urls\n"
                else:
                    text = "All set.\nDONE"
                return LLMResponse(text=text, prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project = Project.objects.create(
            name="Tk", slug=project_sandbox.unique_project_slug("Tk"), origin=Project.Origin.NEW,
        )
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Tk")
        turn = ModelRun.objects.create(
            chat=chat, model_name="agent", workspace_path=project.sandbox_path,
            user_prompt="Build a blog", status=RunStatus.QUEUED,
        )

        with patch("src.llm.OllamaBackend.OllamaBackend", _TasksLLM), \
                patch("dashboard.services.project_venv.ensure_venv", lambda *a, **k: {"ok": True, "built": False}):
            ChatTurnRunner().run_turn(chat.id, turn.id)

        turn.refresh_from_db()
        self.assertEqual(turn.status, RunStatus.SUCCEEDED)
        self.assertTrue(CommandLog.objects.filter(model_run=turn, agent="tasks").exists())
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertEqual([t["status"] for t in data["tasks"]], ["completed", "in_progress", "pending"])
        self.assertEqual(data["tasks"][1]["content"], "Write views")


class PlanModalWorkflowTests(TestCase):
    """Plan storage + the plan modal's endpoints (Edit / Refine / Implement) + the plan-driven build."""

    def _project_chat(self, settings):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Plan", slug=project_sandbox.unique_project_slug("Plan"), origin=Project.Origin.NEW)
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="Plan", settings=settings)
        return project, chat

    def test_planning_turn_stores_plan_and_logs_api_exposes_it(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        class _PlanLLM(LLMBackend):
            name = "fake"
            supports_tools = False

            def __init__(self, model=None, host=None):
                self.model = model or "fake"

            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                from src.llm.LLMResponse import LLMResponse

                return LLMResponse(text="## Plan\n**Overview:** A blog.\n**Features:**\n- posts\nDONE",
                                   prompt_tokens=4, completion_tokens=6, latency_sec=0.01, model="fake")

        project, chat = self._project_chat({"planning": True})
        turn = ModelRun.objects.create(chat=chat, model_name="agent",
            workspace_path=project.sandbox_path, user_prompt="A blog", status=RunStatus.QUEUED)
        with patch("src.llm.OllamaBackend.OllamaBackend", _PlanLLM):
            ChatTurnRunner().run_turn(chat.id, turn.id)
        chat.refresh_from_db()
        self.assertIn("Plan", (chat.settings or {}).get("plan", ""))      # planning toggle stores the plan
        self.assertNotIn("structured_prompt", (chat.settings or {}))      # ...not the prompt_writer key
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertIsNotNone(data["plan"])
        self.assertTrue(data["plan"]["ready"])                            # settled + unapproved → modal opens
        self.assertFalse(data["plan"]["approved"])
        self.assertIsNone(data["draft"])                                  # not a prompt_writer chat

    def test_plan_edit_endpoint_saves_text_and_reopens_decision(self):
        project, chat = self._project_chat({"planning": True, "plan": "old", "plan_approved": True})
        resp = self.client.post(f"/chats/{chat.id}/plan/", {"plan": "## New plan"})
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertEqual(chat.settings["plan"], "## New plan")
        self.assertNotIn("plan_approved", chat.settings)                  # an edit re-opens the decision

    def test_plan_refine_endpoint_starts_one_shot_refine_turn(self):
        project, chat = self._project_chat({"planning": False, "plan": "## P"})
        with patch("dashboard.services.chat_runner.start_turn") as st:
            resp = self.client.post(f"/chats/{chat.id}/plan/refine/",
                                    {"plan": "## edited", "feedback": "make it darker"})
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertTrue(chat.settings["_refine_pending"])                 # one-shot, not sticky planning
        self.assertFalse(chat.settings.get("planning"))                   # doesn't trap the chat in planning
        self.assertEqual(chat.settings["plan"], "## edited")              # carried the manual edits
        st.assert_called_once()
        turn = ModelRun.objects.filter(chat=chat).order_by("-id").first()
        self.assertEqual(turn.user_prompt, "make it darker")

    def test_implementing_an_already_approved_plan_is_rejected(self):
        project, chat = self._project_chat({"planning": True, "plan": "## P", "plan_approved": True})
        resp = self.client.post(f"/chats/{chat.id}/implement/", {})
        self.assertEqual(resp.status_code, 409)  # don't re-run the prototype/startproject over a built project

    def test_logs_api_plan_not_ready_after_a_failed_turn(self):
        from dashboard.models import ModelRun

        project, chat = self._project_chat({"planning": True, "plan": "## stale plan"})
        ModelRun.objects.create(chat=chat, model_name="agent",
            workspace_path=project.sandbox_path, user_prompt="refine", status=RunStatus.FAILED)
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertFalse(data["plan"]["ready"])  # a failed refine must not leave the old plan actionable

    def test_has_real_django_distinguishes_real_startproject_from_scaffold(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        ws = Path(tempfile.mkdtemp(prefix="rd_"))
        self.addCleanup(lambda: shutil.rmtree(ws, ignore_errors=True))
        self.assertFalse(ChatTurnRunner._has_real_django(str(ws)))         # empty / prototype phase
        (ws / "manage.py").write_text("x", encoding="utf-8")
        self.assertTrue(ChatTurnRunner._has_real_django(str(ws)))          # a real startproject
        (ws / ".kursinis_scaffolded").write_text("", encoding="utf-8")
        self.assertFalse(ChatTurnRunner._has_real_django(str(ws)))         # our scaffold, not a real project

    def test_implement_endpoint_switches_to_build_and_starts_turn(self):
        project, chat = self._project_chat({"planning": True, "plan": "## P"})
        with patch("dashboard.services.chat_runner.start_turn") as st:
            resp = self.client.post(f"/chats/{chat.id}/implement/", {})
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertTrue(chat.settings["plan_approved"])
        self.assertFalse(chat.settings["planning"])                       # switched to build mode
        st.assert_called_once()
        self.assertTrue(ModelRun.objects.filter(chat=chat).exists())

    def test_implement_without_a_plan_is_rejected(self):
        project, chat = self._project_chat({"planning": True})
        resp = self.client.post(f"/chats/{chat.id}/implement/", {})
        self.assertEqual(resp.status_code, 400)

    def test_build_task_injects_the_approved_plan(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        project, chat = self._project_chat({"plan": "## ApprovedPlan\n- step one", "plan_approved": True})
        turn = ModelRun.objects.create(chat=chat, model_name="agent",
            workspace_path=project.sandbox_path, user_prompt="Implement the approved plan.", status=RunStatus.QUEUED)
        task = ChatTurnRunner()._build_task(chat, turn, project)
        self.assertIn("ApprovedPlan", task)
        self.assertIn("APPROVED PLAN", task)


class PreviewAndScaffolderReworkTests(TestCase):
    """Static prototype preview (no Django) + the scaffolder never clobbering a real `startproject`."""

    def test_preview_serves_static_prototype_when_no_manage_py(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox
        from dashboard.services.previews import PreviewService

        project = Project.objects.create(name="Proto", slug=project_sandbox.unique_project_slug("Proto"), origin="new")
        ws = project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        (ws / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        with patch.object(PreviewService, "_launch") as launch, \
                patch.object(PreviewService, "_ensure_built") as built:
            PreviewService().start_project(project)
        self.assertFalse(built.called, "a static prototype must NOT scaffold a Django project")
        self.assertIn("http.server", launch.call_args.args[2])

    def test_preview_runs_django_when_manage_py_present(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox
        from dashboard.services.previews import PreviewService

        project = Project.objects.create(name="Dj", slug=project_sandbox.unique_project_slug("Dj"), origin="new")
        ws = project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        (ws / "manage.py").write_text("# manage", encoding="utf-8")
        with patch.object(PreviewService, "_launch") as launch, \
                patch.object(PreviewService, "_ensure_built", return_value="") as built:
            PreviewService().start_project(project)
        self.assertTrue(built.called)
        self.assertIn("runserver", launch.call_args.args[2])

    def test_real_startproject_is_not_clobbered_by_the_scaffolder(self):
        from dashboard.services import project_builder
        from dashboard.services.project_sandbox import SandboxRef

        ws = Path(tempfile.mkdtemp(prefix="real_"))
        self.addCleanup(lambda: shutil.rmtree(ws, ignore_errors=True))
        (ws / "manage.py").write_text("REAL-MANAGE", encoding="utf-8")   # a real startproject, no marker
        with patch.object(project_builder, "_write_scaffold") as scaffold, \
                patch.object(project_builder, "_migrate", return_value=("", True)) as migrate:
            out = project_builder.ensure_runnable_project(SandboxRef(str(ws)), force=True)
        self.assertFalse(scaffold.called, "a real startproject must never be scaffolded over")
        self.assertFalse(migrate.called)
        self.assertFalse(out["built"])
        self.assertEqual((ws / "manage.py").read_text(encoding="utf-8"), "REAL-MANAGE")  # left intact

    def test_marked_scaffold_is_still_rebuilt(self):
        from dashboard.services import project_builder
        from dashboard.services.project_sandbox import SandboxRef

        ws = Path(tempfile.mkdtemp(prefix="scaf_"))
        self.addCleanup(lambda: shutil.rmtree(ws, ignore_errors=True))
        (ws / "manage.py").write_text("OURS", encoding="utf-8")
        (ws / ".kursinis_scaffolded").write_text("", encoding="utf-8")   # our scaffold marker
        with patch.object(project_builder, "_write_scaffold") as scaffold, \
                patch.object(project_builder, "_migrate", return_value=("", True)):
            project_builder.ensure_runnable_project(SandboxRef(str(ws)), force=True)
        self.assertTrue(scaffold.called, "our own marked scaffold must still be (re)built")

    def _project_with_running_preview(self, command):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(name="Sw", slug=project_sandbox.unique_project_slug("Sw"), origin="new")
        ws = project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        (ws / "manage.py").write_text("# manage", encoding="utf-8")      # Django now exists
        pv, _ = PreviewServer.objects.get_or_create(project=project)
        pv.status = PreviewServer.Status.RUNNING
        pv.pid = 999999
        pv.command = command
        pv.save()
        return project

    def test_django_phase_replaces_a_running_static_prototype_server(self):
        import sys as _sys
        from dashboard.services.previews import PreviewService

        project = self._project_with_running_preview([_sys.executable, "-m", "http.server", "8000"])
        with patch.object(PreviewService, "_ensure_built", return_value=""), \
                patch.object(PreviewService, "_port_alive", return_value=True), \
                patch.object(PreviewService, "_kill") as kill, \
                patch.object(PreviewService, "_spawn") as spawn:
            PreviewService().start_project(project)
        self.assertTrue(kill.called, "the stale static server must be killed")
        self.assertTrue(spawn.called, "the Django runserver must be spawned")

    def test_django_phase_keeps_a_running_django_server(self):
        import sys as _sys
        from dashboard.services.previews import PreviewService

        project = self._project_with_running_preview([_sys.executable, "manage.py", "runserver", "127.0.0.1:8000"])
        with patch.object(PreviewService, "_ensure_built", return_value=""), \
                patch.object(PreviewService, "_port_alive", return_value=True), \
                patch.object(PreviewService, "_kill") as kill, \
                patch.object(PreviewService, "_spawn") as spawn:
            PreviewService().start_project(project)
        self.assertFalse(kill.called)   # already a LIVE Django server → leave it running
        self.assertFalse(spawn.called)

    def test_start_respawns_a_dead_running_row(self):
        """A RUNNING row whose server died (crashed runserver / reboot) must not block starts."""
        import sys as _sys
        from dashboard.services.previews import PreviewService

        project = self._project_with_running_preview([_sys.executable, "manage.py", "runserver", "127.0.0.1:8000"])
        with patch.object(PreviewService, "_ensure_built", return_value=""), \
                patch.object(PreviewService, "_port_alive", return_value=False), \
                patch.object(PreviewService, "_spawn") as spawn:
            PreviewService().start_project(project)
        self.assertTrue(spawn.called, "a dead RUNNING row must be respawned, not returned as-is")

    def test_reconcile_demotes_a_dead_running_row_to_failed(self):
        """The status poll demotes crashed servers so the panel shows the failure + Retry."""
        import sys as _sys
        from datetime import timedelta

        from django.utils import timezone

        from dashboard.services.previews import PreviewService

        project = self._project_with_running_preview([_sys.executable, "manage.py", "runserver", "127.0.0.1:8000"])
        pv = project.preview
        pv.port = 1  # nothing listens there
        pv.started_at = timezone.now() - timedelta(seconds=60)  # past the binding grace period
        pv.save()
        out = PreviewService().reconcile(pv)
        self.assertEqual(out.status, PreviewServer.Status.FAILED)
        self.assertIn("stopped unexpectedly", out.error_message)

    def test_reconcile_gives_a_fresh_spawn_time_to_bind(self):
        """A row spawned moments ago must not be demoted while runserver is still binding."""
        import sys as _sys

        from django.utils import timezone

        from dashboard.services.previews import PreviewService

        project = self._project_with_running_preview([_sys.executable, "manage.py", "runserver", "127.0.0.1:8000"])
        pv = project.preview
        pv.port = 1
        pv.started_at = timezone.now()  # just spawned
        pv.save()
        out = PreviewService().reconcile(pv)
        self.assertEqual(out.status, PreviewServer.Status.RUNNING)


class LooseChatCreationTests(TestCase):
    """A standalone "New chat" is backed by a LOOSE project (own sandbox, hidden from Projektai)."""

    def test_home_composer_creates_loose_project(self):
        from dashboard.models import Chat
        with patch("dashboard.services.chat_runner.start_turn"):
            resp = self.client.post("/projects/new/", {"prompt": "Build a todo app", "from": "home"})
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.select_related("project").order_by("-id").first()
        self.addCleanup(chat.project.delete)
        self.assertTrue(chat.project.is_loose, "the home 'New chat' composer must create a loose project")

    def test_full_new_project_form_is_not_loose(self):
        from dashboard.models import Chat
        with patch("dashboard.services.chat_runner.start_turn"):
            resp = self.client.post("/projects/new/", {"prompt": "Build a CRM", "name": "My CRM"})  # no from=home
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.select_related("project").order_by("-id").first()
        self.addCleanup(chat.project.delete)
        self.assertFalse(chat.project.is_loose, "the full New-Project form must create a real project")

    def test_prompt_writer_project_is_loose(self):
        from dashboard.models import Chat
        with patch("dashboard.services.chat_runner.start_turn"):
            resp = self.client.post("/prompt-writer/", {"prompt": "an idea for a blog"})
        self.assertEqual(resp.status_code, 302)
        chat = Chat.objects.select_related("project").order_by("-id").first()
        self.addCleanup(chat.project.delete)
        self.assertTrue(chat.project.is_loose, "the standalone prompt-writer idea project should be loose")

    def test_loose_project_hidden_from_projektai_grid(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        loose = Project.objects.create(name="LooseHidden", slug=project_sandbox.unique_project_slug("LooseHidden"),
                                       origin="new", is_loose=True, sandbox_path="")
        Chat.objects.create(project=loose, title="loose c")
        real = Project.objects.create(name="RealShown", slug=project_sandbox.unique_project_slug("RealShown"),
                                      origin="new", is_loose=False, sandbox_path="")
        html = self.client.get("/projects/").content.decode()
        self.assertIn("RealShown", html)
        self.assertNotIn("LooseHidden", html)


class ProjectDashboardTests(TestCase):
    """Each real project has its own dashboard at /projects/<id>/ (the grid links there)."""

    def _project(self, name="Dash", loose=False):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        return Project.objects.create(
            name=name, slug=project_sandbox.unique_project_slug(name),
            origin="new", is_loose=loose, sandbox_path="",
        )

    def test_dashboard_renders_with_chats_and_stats(self):
        from dashboard.models import Chat

        project = self._project("Acme")
        self.addCleanup(project.delete)
        Chat.objects.create(project=project, title="First chat", status=RunStatus.SUCCEEDED)
        resp = self.client.get(f"/projects/{project.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Acme", html)
        self.assertIn("First chat", html)
        self.assertIn("pd-stats", html)  # stat tiles present

    def test_grid_card_links_to_the_dashboard(self):
        project = self._project("Linky")
        self.addCleanup(project.delete)
        html = self.client.get("/projects/").content.decode()
        self.assertIn(f'href="/projects/{project.id}/"', html)

    def test_loose_project_detail_redirects_to_its_chat(self):
        from dashboard.models import Chat

        loose = self._project("LooseDash", loose=True)
        self.addCleanup(loose.delete)
        chat = Chat.objects.create(project=loose, title="loose work")
        resp = self.client.get(f"/projects/{loose.id}/")
        self.assertRedirects(resp, f"/chats/{chat.id}/", fetch_redirect_response=False)

    def test_new_chat_action_creates_a_chat_in_the_project(self):
        from dashboard.models import Chat

        project = self._project("Builder")
        self.addCleanup(project.delete)
        resp = self.client.post(f"/projects/{project.id}/chats/new/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Chat.objects.filter(project=project).exists())

    def test_dashboard_has_all_five_tabs(self):
        project = self._project("Tabs")
        self.addCleanup(project.delete)
        html = self.client.get(f"/projects/{project.id}/").content.decode()
        for panel in ("panel-chats", "panel-files", "panel-databases", "panel-workflows", "panel-settings"):
            self.assertIn(panel, html)

    def test_workflows_tab_lists_build_runs(self):
        from dashboard.models import Chat, ModelRun

        project = self._project("WF")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="c")
        ModelRun.objects.create(chat=chat, model_name="m", status=RunStatus.SUCCEEDED, user_prompt="Build the homepage")
        html = self.client.get(f"/projects/{project.id}/").content.decode()
        self.assertIn("pd-wf", html)
        self.assertIn("Build the homepage", html)

    def test_settings_tab_renames_the_project(self):
        project = self._project("OldName")
        self.addCleanup(project.delete)
        resp = self.client.post(f"/projects/{project.id}/update/", {"name": "Fresh Name"})
        self.assertRedirects(resp, f"/projects/{project.id}/", fetch_redirect_response=False)
        project.refresh_from_db()
        self.assertEqual(project.name, "Fresh Name")

    def test_databases_tab_without_a_db_shows_empty_state_and_no_fetch(self):
        project = self._project("NoDb")  # sandbox_path="" → no db.sqlite3
        self.addCleanup(project.delete)
        html = self.client.get(f"/projects/{project.id}/").content.decode()
        self.assertIn("No database yet", html)
        self.assertNotIn('class="pd-db"', html)  # lazy-fetch container must NOT be rendered


class _ScriptedToolLLM(LLMBackend):
    """A tool-capable fake: each chat() call returns the next scripted tool_calls batch."""

    name = "scripted"
    supports_tools = True

    def __init__(self, scripts: list):
        self.scripts = list(scripts)
        self.calls = 0

    def chat(self, messages, *, tools=None, temperature=0.2, max_tokens=2048):
        from src.llm.LLMResponse import LLMResponse

        batch = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        return LLMResponse(text="", tool_calls=batch, prompt_tokens=1, completion_tokens=1,
                           latency_sec=0.01, model="scripted")

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        raise AssertionError("router must use chat()")


class DesignGateTests(TestCase):
    """The router structurally blocks finish / design-approval asks until the prototype exists."""

    def _router(self, llm, gate):
        from src.agent.nodes.DynamicNodes import RouterNode

        return RouterNode(llm, interactive=True, design_gate=gate)

    def _state(self):
        return {"messages": [{"role": "user", "content": "Build a site"}], "step_count": 0,
                "pending_actions": [], "finished": False}

    def test_premature_finish_is_nudged_into_building(self):
        llm = _ScriptedToolLLM([
            [{"name": "finish", "arguments": {"summary": "done!"}}],
            [{"name": "write_file", "arguments": {"path": "index.html", "content": "<html></html>"}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "write")
        self.assertEqual(llm.calls, 2, "the premature finish must trigger one corrective re-query")
        self.assertFalse(out.get("finished"))

    def test_finish_alongside_real_work_is_stripped_silently(self):
        llm = _ScriptedToolLLM([
            [{"name": "write_file", "arguments": {"path": "index.html", "content": "<html></html>"}},
             {"name": "finish", "arguments": {}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "write")
        self.assertEqual(llm.calls, 1, "real work in the batch → no re-query, just drop the finish")
        self.assertEqual(out.get("pending_actions"), [], "the stripped finish must not stay queued")

    def test_design_approval_ask_is_blocked_until_files_exist(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"question": "Approve this design, or what should change?"}}],
            [{"name": "write_file", "arguments": {"path": "index.html", "content": "<html></html>"}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "write")

    def test_clarifying_question_passes_the_gate(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"question": "Which colour scheme do you prefer?",
                                                "options": ["Dark", "Light"]}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "ask")
        self.assertEqual(llm.calls, 1)

    def test_open_gate_lets_finish_through(self):
        llm = _ScriptedToolLLM([[{"name": "finish", "arguments": {"summary": "built"}}]])
        out = self._router(llm, gate=lambda: True).run(self._state())
        self.assertTrue(out.get("finished"))
        self.assertEqual(out.get("stop_reason"), "model called finish")

    def test_gate_error_never_blocks_the_agent(self):
        def boom():
            raise OSError("disk gone")
        llm = _ScriptedToolLLM([[{"name": "finish", "arguments": {}}]])
        out = self._router(llm, gate=boom).run(self._state())
        self.assertTrue(out.get("finished"), "a broken gate must fail OPEN")

    def test_stubborn_finisher_ends_without_a_fake_done(self):
        llm = _ScriptedToolLLM([[{"name": "finish", "arguments": {"summary": "done!"}}]])  # never complies
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertTrue(out.get("finished"))
        self.assertEqual(out.get("stop_reason"), "model produced no further action",
                         "give-up must not report a confident model-called-finish")

    def test_domain_question_mentioning_approve_passes(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"question": "Who can approve time-off requests?",
                                                "options": ["Managers", "HR", "Anyone"]}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "ask",
                         "a DOMAIN question about approving things is clarifying, not design sign-off")
        self.assertEqual(llm.calls, 1)

    def test_batched_ask_keeps_clarifying_questions_drops_approval(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"questions": [
                {"question": "Approve this design prototype?", "options": ["Approve", "Request changes"]},
                {"question": "Which currency should prices use?", "options": ["EUR", "USD"]},
            ]}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        action = out.get("next_action") or {}
        self.assertEqual(action.get("tool"), "ask")
        qs = (action.get("args") or {}).get("questions") or []
        self.assertEqual(len(qs), 1, "only the approval question is dropped, the batch survives")
        self.assertIn("currency", qs[0]["question"])

    def test_canonical_approve_options_blocked_even_without_design_word(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"question": "Is this okay to continue?",
                                                "options": ["Approve", "Request changes"]}}],
            [{"name": "write_file", "arguments": {"path": "index.html", "content": "<html></html>"}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "write")

    def test_prompt_alias_in_batched_questions_is_checked(self):
        llm = _ScriptedToolLLM([
            [{"name": "ask_user", "arguments": {"questions": [
                {"prompt": "Approve this design, or request changes?"},
            ]}}],
            [{"name": "write_file", "arguments": {"path": "index.html", "content": "<html></html>"}}],
        ])
        out = self._router(llm, gate=lambda: False).run(self._state())
        self.assertEqual((out.get("next_action") or {}).get("tool"), "write",
                         "the 'prompt' question-text alias must hit the approval check too")

    def test_frontend_detector_ignores_venv_templates(self):
        import tempfile

        from dashboard.services.previews import _has_frontend

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vendored = root / ".venv" / "Lib" / "site-packages" / "django" / "forms"
            vendored.mkdir(parents=True)
            (vendored / "widget.html").write_text("<p>vendored</p>", encoding="utf-8")
            self.assertFalse(_has_frontend(root), "site-packages templates are NOT the project's design")
            (root / "index.html").write_text("<html></html>", encoding="utf-8")
            self.assertTrue(_has_frontend(root))


class StaleTurnHealingTests(TestCase):
    """Orphaned QUEUED/RUNNING turns (heartbeat gone stale after a server restart) self-heal."""

    def _chat_with_run(self, status, age_seconds):
        from datetime import timedelta

        from django.utils import timezone

        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(name="Heal", slug=project_sandbox.unique_project_slug("Heal"),
                                         origin="new", sandbox_path="")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="h", status=status)
        run = ModelRun.objects.create(chat=chat, model_name="agent", status=status)
        ModelRun.objects.filter(id=run.id).update(updated_at=timezone.now() - timedelta(seconds=age_seconds))
        run.refresh_from_db()
        return chat, run

    def test_stale_running_turn_is_failed_by_the_poll(self):
        from django.utils import timezone as _tz  # noqa: F401  (used via module-level timezone)
        chat, run = self._chat_with_run(RunStatus.RUNNING, age_seconds=300)
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("restarted", run.error_summary)
        self.assertEqual(data["model_run"]["status"], RunStatus.FAILED)
        self.assertTrue(CommandLog.objects.filter(model_run=run, stdout__icontains="restarted").exists())

    def test_fresh_running_turn_is_left_alone(self):
        chat, run = self._chat_with_run(RunStatus.RUNNING, age_seconds=5)
        self.client.get(f"/api/chats/{chat.id}/logs/")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.RUNNING, "a beating turn must never be killed")

    def test_stale_turn_no_longer_blocks_sending(self):
        chat, run = self._chat_with_run(RunStatus.RUNNING, age_seconds=300)
        with patch("dashboard.services.chat_runner.start_turn"):
            resp = self.client.post(f"/chats/{chat.id}/send/", {"prompt": "continue please"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200, resp.content)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)

    def test_heartbeat_loop_stops_once_the_turn_is_final(self):
        import threading as _threading

        from dashboard.services import chat_runner

        chat, run = self._chat_with_run(RunStatus.SUCCEEDED, age_seconds=0)
        with patch.object(chat_runner, "_HEARTBEAT_SEC", 0.01):
            chat_runner._heartbeat_loop(run.id, _threading.Event())  # must return, not spin

    def test_heal_denies_the_dead_turns_pending_interactions(self):
        from dashboard.models import PendingInteraction
        from dashboard.services.chat_runner import heal_stale_turns

        chat, run = self._chat_with_run(RunStatus.RUNNING, age_seconds=300)
        pi = PendingInteraction.objects.create(model_run=run, kind=PendingInteraction.Kind.QUESTION,
                                               prompt="Approve this design?", status=PendingInteraction.Status.PENDING)
        heal_stale_turns(chat)
        pi.refresh_from_db()
        self.assertEqual(pi.status, PendingInteraction.Status.DENIED,
                         "no thread is left to consume the answer — the ghost wizard must be cleared")

    def test_heal_note_renders_as_an_error_card(self):
        from dashboard.services.chat_runner import heal_stale_turns

        chat, run = self._chat_with_run(RunStatus.RUNNING, age_seconds=300)
        heal_stale_turns(chat)
        note = CommandLog.objects.filter(model_run=run).order_by("-id").first()
        self.assertEqual(note.agent, "error", "the restart note must use the failed styling, not THINKING")

    def test_mark_running_respects_a_stop_that_won_the_race(self):
        chat, run = self._chat_with_run(RunStatus.CANCELLED, age_seconds=0)
        run.mark_running()
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED,
                         "QUEUED→RUNNING is a guarded transition; a landed Stop must stick")


class BestInstalledModelTests(TestCase):
    """Auto = the best available model: preferred coder family first, then largest size."""

    def test_pick_fallback_prefers_biggest_coder_model(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["llama3.2:3b", "qwen2.5-coder:7b-instruct", "qwen2.5-coder:14b", "mistral:7b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "qwen2.5-coder:14b")

    def test_family_preference_beats_raw_size(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["llama3.3:70b", "qwen2.5-coder:7b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "qwen2.5-coder:7b",
                         "a coder model is a better coding default than a bigger generic one")

    def test_embedding_models_are_never_best(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["nomic-embed-text:latest", "llama3.2:3b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "llama3.2:3b")

    def test_best_installed_empty_when_ollama_unreachable(self):
        from src.llm.OllamaBackend import OllamaBackend

        with patch.object(OllamaBackend, "list_installed", return_value=[]):
            self.assertEqual(OllamaBackend.best_installed(), "")

    def test_auto_chat_resolves_to_best_installed(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner
        from src.llm.OllamaBackend import OllamaBackend

        project = Project.objects.create(name="Auto", slug=project_sandbox.unique_project_slug("Auto"),
                                         origin="new", sandbox_path="")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="a", settings={})  # no model → Auto
        with patch.object(OllamaBackend, "best_installed", return_value="qwen2.5-coder:14b"):
            self.assertEqual(ChatTurnRunner()._resolve_tag(chat), "qwen2.5-coder:14b")

    def test_tiny_coder_model_loses_to_a_big_modern_coder(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["qwen2.5-coder:1.5b", "qwen3-coder:30b", "llama3.3:70b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "qwen3-coder:30b",
                         "a 1.5b draft model must not outrank a 30b coder")

    def test_tiny_model_never_beats_a_big_general_model(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["qwen2.5:0.5b", "qwq:32b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "qwq:32b")

    def test_moe_size_is_parsed_as_experts_times_size(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["mixtral:8x7b", "llama3.1:8b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "mixtral:8x7b")

    def test_vision_models_rank_below_text_models(self):
        from src.llm.OllamaBackend import OllamaBackend

        installed = ["qwen2.5vl:7b", "qwen2.5:7b"]
        self.assertEqual(OllamaBackend._pick_fallback(installed), "qwen2.5:7b")

    def test_explicit_tag_still_wins_over_auto(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(name="Pin", slug=project_sandbox.unique_project_slug("Pin"),
                                         origin="new", sandbox_path="")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, title="p", settings={"ollama_tag": "codellama:13b"})
        self.assertEqual(ChatTurnRunner()._resolve_tag(chat), "codellama:13b")


class AuthGateTests(TestCase):
    """The sign-in gate popup: anonymous visitors can browse, but chatting and Projects ask them to
    sign in first. Models stays open to everyone."""

    def _login(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="bob@example.com", password="Testpass123!")
        self.client.post("/accounts/login/", {"username": "bob@example.com", "password": "Testpass123!"})

    def test_anonymous_home_composer_and_projects_link_are_gated(self):
        html = self.client.get("/").content.decode()
        self.assertIn('data-authed="0"', html)
        self.assertIn("KmAuthGate", html)                      # the popup ships on every page
        composer = html.split('id="home-composer"')[1].split(">")[0]
        self.assertIn("data-auth-gate", composer)
        projects = html.split('aria-label="Projects"')[1].split(">")[0]
        self.assertIn("data-auth-gate", projects)

    def test_models_is_not_gated_for_anonymous_visitors(self):
        html = self.client.get("/").content.decode()
        models_link = html.split('aria-label="Models"')[1].split(">")[0]
        self.assertNotIn("data-auth-gate", models_link)
        self.assertEqual(self.client.get("/models/").status_code, 200)

    def test_signed_in_pages_report_authed_so_the_gate_stays_inert(self):
        self._login()
        self.assertIn('data-authed="1"', self.client.get("/").content.decode())

    def test_signup_honours_next_from_the_gate(self):
        resp = self.client.post("/accounts/signup/?next=/models/", {
            "name": "Gated", "email": "gated@example.com",
            "password1": "Testpass123!", "password2": "Testpass123!",
            "birthdate": "1990-05-01", "source": "search", "next": "/models/",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/models/")

    def test_next_cannot_redirect_off_site(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="bob@example.com", password="Testpass123!")
        resp = self.client.post("/accounts/login/?next=https://evil.example/", {
            "username": "bob@example.com", "password": "Testpass123!",
        })
        self.assertEqual(resp["Location"], "/")   # off-site next is dropped, not followed


class RecentSidebarTests(TestCase):
    """The sidebar "Recent" list (injected by the context processor on every signed-in page)."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_user(username="bob@example.com", password="Testpass123!")
        self.client.post("/accounts/login/", {"username": "bob@example.com", "password": "Testpass123!"})

    def _chat(self, title, *, loose=False, mode=None):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        p = Project.objects.create(name=title + " P", slug=project_sandbox.unique_project_slug(title),
                                   origin="new", is_loose=loose, sandbox_path="")
        settings = {"mode": mode} if mode else {}
        return Chat.objects.create(project=p, title=title, settings=settings)

    def test_recent_excludes_prompt_writer_and_lists_chats(self):
        self._chat("Alpha loose", loose=True)
        self._chat("Beta project")
        self._chat("Hidden writer", mode="prompt_writer")
        resp = self.client.get("/")
        recent = resp.context["recent_chats"]
        titles = [r["title"] for r in recent]
        self.assertIn("Alpha loose", titles)
        self.assertIn("Beta project", titles)
        self.assertNotIn("Hidden writer", titles, "prompt-writer chats must not appear in Recent")
        # Project chats are labeled by their project; loose chats are not.
        beta = next(r for r in recent if r["title"] == "Beta project")
        alpha = next(r for r in recent if r["title"] == "Alpha loose")
        self.assertTrue(beta["project_label"])
        self.assertFalse(alpha["project_label"])

    def test_recent_marks_active_chat_on_chat_page(self):
        from dashboard.models import AIModel

        AIModel.objects.create(name="m", is_active=True, execution_order=1)
        other = self._chat("Other")
        current = self._chat("Current")
        resp = self.client.get(f"/chats/{current.id}/")
        recent = {r["id"]: r for r in resp.context["recent_chats"]}
        self.assertTrue(recent[current.id]["active"])
        self.assertFalse(recent[other.id]["active"])

    def test_recent_is_capped(self):
        for i in range(15):
            self._chat(f"Chat {i}")
        recent = self.client.get("/").context["recent_chats"]
        self.assertLessEqual(len(recent), 12)

    def test_recent_is_hidden_for_anonymous_visitors(self):
        self._chat("Alpha loose", loose=True)
        self.client.logout()
        resp = self.client.get("/")
        self.assertEqual(resp.context["recent_chats"], [])
        html = resp.content.decode()
        # The whole sidebar block is gone, not just the rows (the CSS/JS for it still ships in base.html).
        self.assertNotIn('<div class="side-recent">', html)
        self.assertNotIn("Alpha loose", html)


class ChatRenameMoveDeleteTests(TestCase):
    """Rename / Move-to-project (merge) / Delete for loose + project chats."""

    def _loose_chat(self, name="Loose"):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        p = Project.objects.create(name=name, slug=project_sandbox.unique_project_slug(name),
                                   origin="new", is_loose=True)
        path = project_sandbox.create_sandbox(p)
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        chat = Chat.objects.create(project=p, title=name)
        return p, path, chat

    def _real_project(self, name="Target"):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        p = Project.objects.create(name=name, slug=project_sandbox.unique_project_slug(name),
                                   origin="new", is_loose=False)
        path = project_sandbox.create_sandbox(p)
        self.addCleanup(p.delete)
        return p, path

    def test_rename_sets_title(self):
        _, _, chat = self._loose_chat("Before")
        resp = self.client.post(f"/chats/{chat.id}/rename/", {"title": "After"},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertEqual(chat.title, "After")

    def test_rename_rejects_empty(self):
        _, _, chat = self._loose_chat("Keep")
        resp = self.client.post(f"/chats/{chat.id}/rename/", {"title": "  "},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)
        chat.refresh_from_db()
        self.assertEqual(chat.title, "Keep")

    def test_move_merges_files_keeps_scaffold_and_deletes_loose(self):
        from dashboard.models import Chat, Project

        loose, loose_path, chat = self._loose_chat("Mv")
        (loose_path / "app.py").write_text("print('hi')", encoding="utf-8")
        (loose_path / "manage.py").write_text("LOOSE-MANAGE", encoding="utf-8")  # scaffold → must be skipped
        target, target_path = self._real_project("Dest")
        (target_path / "manage.py").write_text("TARGET-MANAGE", encoding="utf-8")

        resp = self.client.post(f"/chats/{chat.id}/move/", {"project": str(target.id)},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))
        # target scaffold survives; the loose source file was merged in
        self.assertEqual((target_path / "manage.py").read_text(encoding="utf-8"), "TARGET-MANAGE")
        self.assertEqual((target_path / "app.py").read_text(encoding="utf-8"), "print('hi')")
        # chat reassigned; loose project + sandbox gone
        chat.refresh_from_db()
        self.assertEqual(chat.project_id, target.id)
        self.assertFalse(Project.objects.filter(id=loose.id).exists())
        self.assertFalse(loose_path.exists())

    def test_move_does_not_overwrite_existing_target_file(self):
        loose, loose_path, chat = self._loose_chat("Mv2")
        (loose_path / "views.py").write_text("LOOSE", encoding="utf-8")
        target, target_path = self._real_project("Dest2")
        (target_path / "views.py").write_text("TARGET", encoding="utf-8")
        resp = self.client.post(f"/chats/{chat.id}/move/", {"project": str(target.id)},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((target_path / "views.py").read_text(encoding="utf-8"), "TARGET")  # not clobbered
        self.assertGreaterEqual(resp.json().get("skipped", 0), 1)

    def test_move_blocked_while_running(self):
        from dashboard.models import ModelRun

        loose, _, chat = self._loose_chat("Busy")
        target, _ = self._real_project("Dest3")
        ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")
        resp = self.client.post(f"/chats/{chat.id}/move/", {"project": str(target.id)},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 409)
        chat.refresh_from_db()
        self.assertEqual(chat.project_id, loose.id)  # still loose

    def test_move_rejects_non_loose_chat(self):
        target, _ = self._real_project("Dest4")
        other, _ = self._real_project("Source")
        from dashboard.models import Chat
        chat = Chat.objects.create(project=other, title="not loose")
        resp = self.client.post(f"/chats/{chat.id}/move/", {"project": str(target.id)},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)

    def test_delete_loose_chat_removes_project_and_sandbox(self):
        from dashboard.models import Project

        loose, loose_path, chat = self._loose_chat("Doomed")
        self.assertTrue(loose_path.exists())
        resp = self.client.post(f"/chats/{chat.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/")
        self.assertFalse(Project.objects.filter(id=loose.id).exists())
        self.assertFalse(loose_path.exists())

    def test_delete_project_chat_keeps_project(self):
        from dashboard.models import Chat, Project

        target, _ = self._real_project("KeepMe")
        chat = Chat.objects.create(project=target, title="a chat")
        resp = self.client.post(f"/chats/{chat.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Project.objects.filter(id=target.id).exists())

    def test_delete_only_work_chat_does_not_fall_back_to_prompt_writer(self):
        # Deleting the only work chat must NOT redirect to the docked prompt-writer chat (a hidden,
        # non-functional page) — it should fall back to the projects list.
        from dashboard.models import Chat

        target, _ = self._real_project("WithWriter")
        work = Chat.objects.create(project=target, title="work")
        Chat.objects.create(project=target, title="writer", settings={"mode": "prompt_writer"})
        resp = self.client.post(f"/chats/{work.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/projects/")


class MergeSandboxUnitTests(TestCase):
    """Direct unit coverage of project_sandbox.merge_sandbox."""

    def test_merge_skips_scaffold_and_collisions(self):
        from dashboard.services.project_sandbox import merge_sandbox

        src = Path(tempfile.mkdtemp(prefix="msrc_"))
        dst = Path(tempfile.mkdtemp(prefix="mdst_"))
        self.addCleanup(lambda: shutil.rmtree(src, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(dst, ignore_errors=True))
        (src / "new.py").write_text("NEW", encoding="utf-8")
        (src / "manage.py").write_text("SRC-MANAGE", encoding="utf-8")     # scaffold → skip
        (src / "dup.py").write_text("SRC-DUP", encoding="utf-8")           # collision → skip
        (dst / "dup.py").write_text("DST-DUP", encoding="utf-8")
        result = merge_sandbox(str(src), str(dst))
        self.assertTrue(result["ok"])
        self.assertEqual((dst / "new.py").read_text(encoding="utf-8"), "NEW")
        self.assertFalse((dst / "manage.py").exists())                     # scaffold never copied
        self.assertEqual((dst / "dup.py").read_text(encoding="utf-8"), "DST-DUP")
        self.assertIn("manage.py", result["skipped"])
        self.assertIn("dup.py", result["skipped"])


class ResourcesTabOptInTests(TestCase):
    """The Resources tab is opt-in: its button is hidden by default in the rendered page."""

    def test_resources_tab_hidden_by_default(self):
        from dashboard.models import AIModel, Chat, Project
        from dashboard.services import project_sandbox

        AIModel.objects.create(name="m", is_active=True, execution_order=1)
        p = Project.objects.create(name="R", slug=project_sandbox.unique_project_slug("R"), origin="new")
        self.addCleanup(p.delete)
        project_sandbox.create_sandbox(p)
        chat = Chat.objects.create(project=p, title="r")
        html = self.client.get(f"/chats/{chat.id}/").content.decode()
        self.assertIn('data-tab="resources" hidden', html)
        self.assertIn('data-pane="empty"', html)


class LooseChatChromeTests(TestCase):
    """The chat topbar adapts for loose vs. project chats."""

    def _render(self, *, loose):
        from dashboard.models import AIModel, Chat, Project
        from dashboard.services import project_sandbox

        AIModel.objects.create(name="m", is_active=True, execution_order=1)
        name = "Loosey" if loose else "Realy"
        p = Project.objects.create(name=name, slug=project_sandbox.unique_project_slug(name),
                                   origin="new", is_loose=loose)
        self.addCleanup(p.delete)
        project_sandbox.create_sandbox(p)
        chat = Chat.objects.create(project=p, title="chat")
        return self.client.get(f"/chats/{chat.id}/").content.decode()

    def test_loose_chat_hides_switcher(self):
        # The header is minimal (actions moved to the sidebar Recent ⋯ menu); a loose chat shows no
        # project switcher and no project-level chrome.
        html = self._render(loose=True)
        self.assertNotIn('id="chat-switch"', html)
        self.assertNotIn("Delete project", html)

    def test_project_chat_shows_switcher(self):
        html = self._render(loose=False)
        self.assertIn('id="chat-switch"', html)


class SidebarNavTests(TestCase):
    """English sidebar labels + the Projects item not lighting up on a standalone (loose) chat."""

    def _chat(self, *, loose):
        from dashboard.models import AIModel, Chat, Project
        from dashboard.services import project_sandbox

        AIModel.objects.create(name="m", is_active=True, execution_order=1)
        name = "L" if loose else "R"
        p = Project.objects.create(name=name, slug=project_sandbox.unique_project_slug(name),
                                   origin="new", is_loose=loose)
        self.addCleanup(p.delete)
        project_sandbox.create_sandbox(p)
        return Chat.objects.create(project=p, title="c")

    def test_models_page_has_library_tabs(self):
        html = self.client.get("/models/").content.decode()
        for marker in ('data-mtab="available"', 'data-mtab="installed"',
                       "data-library-url", "data-page-url-template", "data-remove-url",
                       'id="mlib-pager"', 'id="minst-pager"'):
            self.assertIn(marker, html)
        # the registry tab + its "New registry entry" button were removed from this page
        self.assertNotIn('data-mtab="registry"', html)
        self.assertNotIn("New registry entry", html)
        # accessible tablist: panes are tabpanels wired to their tab, current tab is aria-selected,
        # and the live download/status region announces updates
        self.assertIn('aria-selected="true"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('aria-controls="mpane-available"', html)
        self.assertIn('aria-live="polite"', html)

    def test_sidebar_labels_are_english(self):
        html = self.client.get("/").content.decode()
        self.assertIn(">Projects</span>", html)
        self.assertIn(">Models</span>", html)
        self.assertNotIn("Projektai", html)
        self.assertNotIn("Modeliai", html)

    def test_projects_nav_inactive_on_loose_chat(self):
        chat = self._chat(loose=True)
        resp = self.client.get(f"/chats/{chat.id}/")
        self.assertTrue(resp.context["nav_active_loose"])  # → the "Projects" link is NOT marked active
        self.assertIn('data-loose="1"', resp.content.decode())

    def test_projects_nav_active_on_project_chat(self):
        chat = self._chat(loose=False)
        resp = self.client.get(f"/chats/{chat.id}/")
        self.assertFalse(resp.context["nav_active_loose"])
        self.assertIn('data-loose="0"', resp.content.decode())


class OllamaLibraryParserTests(TestCase):
    """Pure parsers + sanitiser for the scraped ollama.com pages."""

    _LIBRARY_HTML = """
    <ul>
      <li x-test-model>
        <a href="/library/llama9"><div title="llama9" x-test-model-title>
          <h2><span>llama9</span></h2>
          <p>A very good model.</p></div>
          <span x-test-capability>tools</span>
          <span x-test-size>8b</span><span x-test-size>70b</span>
          <p><span x-test-pull-count>12.3M</span>
             <span x-test-tag-count>21</span>
             <span x-test-updated>3 weeks ago</span></p>
        </a>
      </li>
      <li x-test-model><a href="/library/bad name"><p>skipped because of the invalid name</p></a></li>
    </ul>
    """

    def test_parse_library(self):
        from dashboard.services import ollama_library

        models = ollama_library.parse_library(self._LIBRARY_HTML)
        self.assertEqual(len(models), 1)  # the invalid name is skipped
        m = models[0]
        self.assertEqual(m["name"], "llama9")
        self.assertEqual(m["description"], "A very good model.")
        self.assertEqual(m["capabilities"], ["tools"])
        self.assertEqual(m["sizes"], ["8b", "70b"])
        self.assertEqual(m["pulls"], "12.3M")
        self.assertEqual(m["tag_count"], "21")
        self.assertEqual(m["updated"], "3 weeks ago")

    def test_parse_tags_dedupes_and_extracts_size(self):
        from dashboard.services import ollama_library

        html = """
        <div><a href="/library/llama9:latest">llama9:latest</a>
             <span>abc123 - 4.9GB - 128K context window - Text input</span></div>
        <div><a href="/library/llama9:latest">llama9:latest 4.9GB</a></div>
        <div><a href="/library/llama9:70b">llama9:70b</a><span>43GB - 128K context window</span></div>
        """
        tags = ollama_library.parse_tags(html, "llama9")
        self.assertEqual([t["tag"] for t in tags], ["llama9:latest", "llama9:70b"])
        by_tag = {t["tag"]: t for t in tags}
        self.assertEqual(by_tag["llama9:latest"]["size"], "4.9GB")
        self.assertEqual(by_tag["llama9:70b"]["size"], "43GB")
        self.assertIn("128K context", by_tag["llama9:70b"]["context"])

    def test_parse_detail_readme_and_images(self):
        from dashboard.services import ollama_library

        html = """
        <html><body>
          <section id="summary"><h2>llama9</h2><p>A very good model.</p>
            <span x-test-capability>tools</span></section>
          <div id="readme"><h1>Llama 9</h1><p>Long readme.</p>
            <img src="/assets/shot.png" alt="screenshot">
            <script>alert(1)</script></div>
        </body></html>
        """
        d = ollama_library.parse_detail(html, "llama9")
        self.assertEqual(d["name"], "llama9")
        self.assertIn("Long readme.", d["readme_html"])
        self.assertNotIn("script", d["readme_html"])
        self.assertEqual(d["images"], ["https://ollama.com/assets/shot.png"])

    def test_sanitize_html_whitelist(self):
        from dashboard.services.ollama_library import sanitize_html

        dirty = (
            '<h1 onclick="evil()">Title</h1>'
            "<script>alert(1)</script>"
            '<iframe src="https://evil"></iframe>'
            '<a href="javascript:evil()">bad link</a>'
            '<a href="/blog/x">good link</a>'
            '<img src="https://ollama.com/a.png" onerror="evil()">'
            '<video src="x">clip</video>'
        )
        out = sanitize_html(dirty)
        self.assertIn("<h1>Title</h1>", out)                      # on* attr stripped
        self.assertNotIn("script", out)
        self.assertNotIn("iframe", out)
        self.assertNotIn("javascript:", out)                       # js: href dropped
        self.assertIn('href="https://ollama.com/blog/x"', out)     # relative href resolved
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('src="https://ollama.com/a.png"', out)
        self.assertNotIn("onerror", out)
        self.assertNotIn("<video", out)                            # unknown tag unwrapped
        self.assertIn("clip", out)                                 # but its text is kept


class OllamaModelsPageApiTests(TestCase):
    """The Models-page endpoints, with the network/CLI layers patched."""

    def test_library_api_annotates_installed(self):
        with patch("dashboard.services.ollama_library.list_library",
                   lambda refresh=False: {"models": [{"name": "llama9"}, {"name": "phi9"}], "error": None}), \
                patch("dashboard.services.ollama_models.installed_models",
                      lambda: {"llama9:8b"}):
            data = self.client.get("/api/ollama/library/").json()
        flags = {m["name"]: m["installed"] for m in data["models"]}
        self.assertTrue(flags["llama9"])
        self.assertFalse(flags["phi9"])

    def test_detail_api_annotates_tags(self):
        detail = {"name": "llama9", "description": "d", "capabilities": [], "readme_html": "",
                  "images": [], "tags": [{"tag": "llama9:8b", "size": "4.9GB", "context": ""},
                                         {"tag": "llama9:70b", "size": "43GB", "context": ""}]}
        with patch("dashboard.services.ollama_library.model_detail",
                   lambda name, refresh=False: {"detail": detail, "error": None}), \
                patch("dashboard.services.ollama_models.installed_models", lambda: {"llama9:8b"}), \
                patch("dashboard.services.ollama_models.pull_states",
                      lambda: {"llama9:70b": {"status": "pulling", "line": "42%", "started": 0}}):
            data = self.client.get("/api/ollama/library/llama9/").json()
        tags = {t["full_tag"]: t for t in data["detail"]["tags"]}
        self.assertTrue(tags["llama9:8b"]["installed"])
        self.assertFalse(tags["llama9:8b"]["pulling"])
        self.assertTrue(tags["llama9:70b"]["pulling"])

    def test_installed_api_lists_models_and_pulls(self):
        rows = [{"tag": "llama9:8b", "id": "abc", "size": "4.9 GB", "modified": "2 days ago"}]
        with patch("dashboard.services.ollama_models.installed_models_detailed", lambda: rows), \
                patch("dashboard.services.ollama_models.pull_states", lambda: {}):
            data = self.client.get("/api/ollama/installed/").json()
        self.assertEqual(data["models"], rows)
        self.assertIsNone(data["error"])

    def test_install_api_validates_and_starts(self):
        resp = self.client.post("/api/ollama/install/", {"tag": "bad tag; rm -rf"})
        self.assertEqual(resp.status_code, 400)
        started = []
        with patch("dashboard.services.ollama_models.start_background_pull",
                   lambda tag: (started.append(tag) or True, "Download started.")):
            resp = self.client.post("/api/ollama/install/", {"tag": "llama9:8b"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(started, ["llama9:8b"])

    def test_remove_api_validates_and_removes(self):
        resp = self.client.post("/api/ollama/remove/", {"tag": "bad tag"})
        self.assertEqual(resp.status_code, 400)
        removed = []
        with patch("dashboard.services.ollama_models.remove_model",
                   lambda tag: (removed.append(tag) or True, "deleted")):
            resp = self.client.post("/api/ollama/remove/", {"tag": "llama9:8b"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(removed, ["llama9:8b"])

    def test_installed_models_detailed_parses_columns(self):
        from dashboard.services import ollama_models

        sample = ("NAME                ID              SIZE      MODIFIED\n"
                  "llama9:8b           46e0c10c039e    4.9 GB    45 hours ago\n"
                  "deepseek:latest     3ddd2d3fc8d2    776 MB    11 months ago\n")
        fake = Mock(returncode=0, stdout=sample, stderr="")
        with patch("dashboard.services.ollama_models.subprocess.run", lambda *a, **k: fake):
            rows = ollama_models.installed_models_detailed()
        self.assertEqual(rows[0], {"tag": "llama9:8b", "id": "46e0c10c039e",
                                   "size": "4.9 GB", "modified": "45 hours ago"})
        self.assertEqual(rows[1]["size"], "776 MB")


class OllamaModelDetailPageTests(TestCase):
    """The server-rendered model detail PAGE (/models/library/<name>/) replacing the old modal."""

    _DETAIL = {
        "name": "llama9", "description": "A very good model.", "capabilities": ["tools"],
        "readme_html": '<h1>Llama 9</h1><p>Long readme.</p><img src="https://ollama.com/a.png">',
        "images": ["https://ollama.com/a.png"],
        "tags": [{"tag": "llama9:8b", "size": "4.9GB", "context": "128K context"},
                 {"tag": "llama9:70b", "size": "43GB", "context": ""}],
    }

    def test_detail_page_renders_server_side(self):
        with patch("dashboard.services.ollama_library.model_detail",
                   lambda name, refresh=False: {"detail": dict(self._DETAIL), "error": None}), \
                patch("dashboard.services.ollama_models.installed_models", lambda: {"llama9:8b"}), \
                patch("dashboard.services.ollama_models.pull_states", lambda: {}):
            resp = self.client.get("/models/library/llama9/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # content present on load (no JS/modal needed)
        self.assertIn("A very good model.", html)
        self.assertIn("Long readme.", html)            # sanitised README rendered inline
        self.assertIn('id="mdl-install-btn"', html)    # the install control
        self.assertIn("llama9:8b", html)
        self.assertIn("— installed", html)             # 8b annotated installed (it's in the set)
        self.assertIn('src="https://ollama.com/a.png"', html)
        self.assertIn("model_detail.js", html)         # page-scoped install/poll script
        self.assertIn('aria-live="polite"', html)      # progress announced to screen readers
        self.assertTrue(resp.context["detail"])

    def test_detail_page_handles_unknown_model_gracefully(self):
        with patch("dashboard.services.ollama_library.model_detail",
                   lambda name, refresh=False: {"detail": None, "error": "not found"}):
            resp = self.client.get("/models/library/totally-made-up/")
        self.assertEqual(resp.status_code, 200)  # no 500 — graceful empty state
        self.assertIn("Could not load details", resp.content.decode())

    def test_detail_page_rejects_invalid_name(self):
        # a name with characters outside the library naming charset → 404, never a scrape attempt
        resp = self.client.get("/models/library/bad%20name%21/")
        self.assertEqual(resp.status_code, 404)

    def test_detail_page_rejects_punctuation_only_name(self):
        from dashboard.services import ollama_library

        # pure-punctuation segments must never reach the scraper (they'd resolve to ollama.com root)
        self.assertFalse(ollama_library.MODEL_NAME_RE.match(".."))
        self.assertFalse(ollama_library.MODEL_NAME_RE.match("."))
        self.assertTrue(ollama_library.MODEL_NAME_RE.match("llama3.1"))
        # a dots-only name reaches the view but is rejected with 404 by its MODEL_NAME_RE guard
        self.assertEqual(self.client.get("/models/library/.../").status_code, 404)

    def test_models_nav_active_on_detail_page(self):
        with patch("dashboard.services.ollama_library.model_detail",
                   lambda name, refresh=False: {"detail": dict(self._DETAIL), "error": None}), \
                patch("dashboard.services.ollama_models.installed_models", lambda: set()), \
                patch("dashboard.services.ollama_models.pull_states", lambda: {}):
            html = self.client.get("/models/library/llama9/").content.decode()
        # the Models sidebar link should carry the active class on the detail page
        self.assertRegex(html, r'side-link active[^>]*aria-label="Models"')


class ContextMeterAndMemoryTests(TestCase):
    """The context-window meter estimate + manual/auto compaction into chat memory."""

    def _project_chat(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Ctx", slug=project_sandbox.unique_project_slug("Ctx"), origin=Project.Origin.NEW)
        project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        return project, Chat.objects.create(project=project, title="Ctx")

    def test_estimate_context_shape_and_threshold(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        project, chat = self._project_chat()
        ctx = ChatTurnRunner.estimate_context(chat)
        for k in ("used_tokens", "window_tokens", "remaining_pct", "auto_compact_pct", "should_compact", "has_memory"):
            self.assertIn(k, ctx)
        self.assertFalse(ctx["should_compact"])           # empty chat is nowhere near full
        self.assertFalse(ctx["has_memory"])

        # a tiny window makes even a small history exceed the auto-compact threshold
        chat.settings = {"context_window": 50}
        chat.save(update_fields=["settings"])
        ModelRun.objects.create(chat=chat, model_name="agent",
                                user_prompt="x" * 4000, status=RunStatus.SUCCEEDED)
        ctx2 = ChatTurnRunner.estimate_context(chat)
        self.assertTrue(ctx2["should_compact"])
        self.assertEqual(ctx2["remaining_pct"], 0)

    def test_compact_chat_writes_memory_and_advances_boundary(self):
        from dashboard.services.chat_runner import ChatTurnRunner
        from src.llm.LLMResponse import LLMResponse

        project, chat = self._project_chat()
        r1 = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="Build a blog",
                                     status=RunStatus.SUCCEEDED)
        CommandLog.objects.create(model_run=r1, kind=CommandLog.Kind.SYSTEM, agent="finish",
                                  stdout="Created the Post model and a homepage.")
        r2 = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="Add comments",
                                     status=RunStatus.SUCCEEDED)

        class _FakeBackend:
            def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
                return LLMResponse(text="- Blog with posts + comments\n- Post model done",
                                   prompt_tokens=5, completion_tokens=8, latency_sec=0.01, model="fake")

        ok = ChatTurnRunner().compact_chat(chat, _FakeBackend())
        self.assertTrue(ok)
        chat.refresh_from_db()
        self.assertIn("comments", chat.settings["memory"])
        self.assertEqual(chat.settings["compacted_through_run_id"], r2.id)
        # a 3rd, NEW turn after the boundary no longer re-reads r1/r2 as raw history
        r3 = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="Add tags",
                                     status=RunStatus.QUEUED)
        task = ChatTurnRunner()._build_task(chat, r3, project)
        self.assertIn("compacted memory", task)
        self.assertNotIn("Build a blog", task)        # old prompt now lives only in memory
        self.assertIn("Add tags", task)

    def test_compact_chat_noop_when_nothing_to_compact(self):
        from dashboard.services.chat_runner import ChatTurnRunner

        project, chat = self._project_chat()
        self.assertFalse(ChatTurnRunner().compact_chat(chat, object()))  # no runs → no LLM call

    def test_logs_api_exposes_context(self):
        project, chat = self._project_chat()
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertIn("context", data)
        self.assertIn("compact_url", data["context"])
        self.assertEqual(data["context"]["compact_url"], f"/chats/{chat.id}/compact/")

    def test_logs_api_has_summary_gates_the_done_tag(self):
        """`model_run.has_summary` is what lets the feed show "Done." — it must be False for a
        succeeded turn WITHOUT a final summary and True once a finish card exists."""
        project, chat = self._project_chat()
        run = ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="build it",
                                      status=RunStatus.SUCCEEDED)
        data = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertFalse(data["model_run"]["has_summary"])     # ended, but said nothing → no Done tag

        CommandLog.objects.create(model_run=run, kind=CommandLog.Kind.SYSTEM, agent="finish",
                                  stdout="Built the homepage; run with `manage.py runserver`.")
        data2 = self.client.get(f"/api/chats/{chat.id}/logs/").json()
        self.assertTrue(data2["model_run"]["has_summary"])     # real summary → Done may render

    def test_compact_endpoint_sets_compacting_flag(self):
        project, chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="hi", status=RunStatus.SUCCEEDED)
        # Stub Thread so the daemon body doesn't run (it would race the SQLite test DB lock); assert
        # only the endpoint's synchronous part — it sets the _compacting flag the meter reads.
        with patch("threading.Thread") as MockThread:
            MockThread.return_value.start = lambda: None
            resp = self.client.post(f"/chats/{chat.id}/compact/")
        self.assertEqual(resp.status_code, 200)
        chat.refresh_from_db()
        self.assertTrue(chat.settings.get("_compacting"))

    def test_compact_endpoint_409_while_running(self):
        project, chat = self._project_chat()
        ModelRun.objects.create(chat=chat, model_name="agent", user_prompt="hi", status=RunStatus.RUNNING)
        self.assertEqual(self.client.post(f"/chats/{chat.id}/compact/").status_code, 409)


class BuildOptsSanitisingTests(TestCase):
    """Weak models copy the question-prompt template literally — the option builder must clean it."""

    def test_placeholder_label_with_markdown_is_replaced_by_its_description(self):
        from dashboard.services.chat_runner import _build_opts

        opts = _build_opts(["**Option A (Recommended)** — Store fields as free text"])
        self.assertEqual(opts[0]["label"], "Store fields as free text")
        self.assertEqual(opts[0]["description"], "")
        self.assertTrue(opts[0]["recommended"])

    def test_recommended_marker_is_a_flag_not_label_text(self):
        from dashboard.services.chat_runner import _build_opts

        opts = _build_opts(["Admins only (Recommended) — staff manage all recipes", "Anyone — open"])
        self.assertEqual(opts[0]["label"], "Admins only")
        self.assertTrue(opts[0]["recommended"])
        self.assertEqual(opts[0]["description"], "staff manage all recipes")
        self.assertEqual(opts[1]["label"], "Anyone")
        self.assertFalse(opts[1]["recommended"])

    def test_plain_labels_pass_through_and_markdown_is_stripped(self):
        from dashboard.services.chat_runner import _build_opts

        opts = _build_opts(["`SQLite` — default", "__PostgreSQL__"])
        self.assertEqual(opts[0]["label"], "SQLite")
        self.assertEqual(opts[1]["label"], "PostgreSQL")


class MixedPrototypeScaffoldPreviewTests(TestCase):
    """A sandbox holding BOTH a prototype and a (premature) Django scaffold: while the app wires
    no pages the preview serves the PROTOTYPE being approved; Django wins once routes exist."""

    def _project(self):
        from dashboard.models import Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Mix", slug=project_sandbox.unique_project_slug("Mix"), origin="new")
        ws = project_sandbox.create_sandbox(project)
        self.addCleanup(project.delete)
        (ws / "index.html").write_text("<h1>proto</h1>", encoding="utf-8")
        (ws / "manage.py").write_text("# manage", encoding="utf-8")
        return project

    def test_serves_prototype_while_django_wires_no_routes(self):
        from dashboard.services import route_discovery
        from dashboard.services.previews import PreviewService

        project = self._project()
        with patch.object(PreviewService, "_launch") as launch, \
                patch.object(PreviewService, "_ensure_built", return_value=""), \
                patch.object(route_discovery, "discover_routes", return_value=[]):
            PreviewService().start_project(project)
        self.assertIn("http.server", launch.call_args.args[2])

    def test_serves_django_once_routes_exist(self):
        from dashboard.services import route_discovery
        from dashboard.services.previews import PreviewService

        project = self._project()
        with patch.object(PreviewService, "_launch") as launch, \
                patch.object(PreviewService, "_ensure_built", return_value=""), \
                patch.object(route_discovery, "discover_routes", return_value=[{"path": "/", "name": "home"}]):
            PreviewService().start_project(project)
        self.assertIn("runserver", launch.call_args.args[2])


class AutoModeDesignSignoffTests(TestCase):
    """Auto mode makes builds fully hands-free: the design sign-off question auto-approves,
    while command approvals keep their existing fast-path."""

    def _turn(self):
        from dashboard.models import Chat, Project
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="AutoSign", slug=project_sandbox.unique_project_slug("AutoSign"), origin="new")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, settings={"auto_approve": True})
        return ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")

    class _StubRunner:
        def _command(self, *a, **k):
            pass

    def test_design_signoff_question_auto_approves(self):
        from dashboard.models import PendingInteraction
        from dashboard.services.chat_runner import ChatTurnRunner

        run = self._turn()
        out = ChatTurnRunner()._handle_interaction(
            self._StubRunner(), run, "question",
            {"prompt": "Approve this prototype, or what should change?",
             "options": ["Approve", "Request changes"]},
            True,
        )
        self.assertEqual(out, "Approve")
        self.assertFalse(PendingInteraction.objects.filter(model_run=run).exists())

    def test_command_approval_still_fast_paths(self):
        from dashboard.models import PendingInteraction
        from dashboard.services.chat_runner import ChatTurnRunner

        run = self._turn()
        out = ChatTurnRunner()._handle_interaction(
            self._StubRunner(), run, "approval",
            {"tool": "shell", "command": "django-admin startproject x ."}, True)
        self.assertIs(out, True)
        self.assertFalse(PendingInteraction.objects.filter(model_run=run).exists())


class TaskReconciliationTests(TestCase):
    """After a successful turn, one strict LLM audit flips truly-done tasks to completed."""

    def test_reconcile_flips_evidenced_tasks_and_publishes_the_list(self):
        import json as _json

        from dashboard.models import Chat, PendingInteraction, Project  # noqa: F401
        from dashboard.services import chat_runner as cr
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Rec", slug=project_sandbox.unique_project_slug("Rec"), origin="new")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, settings={})
        run = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")

        class _Auditor:
            def __init__(self, model=None, host=None):
                pass

            def complete(self, prompt, **kw):
                from src.llm.LLMResponse import LLMResponse

                return LLMResponse(text="COMPLETED: 1, 3")

        final_state = {"tasks": [
            {"content": "Create the model", "status": "in_progress"},
            {"content": "Write docs", "status": "pending"},
            {"content": "Wire the urls", "status": "pending"},
        ]}
        with patch("src.llm.OllamaBackend.OllamaBackend", _Auditor):
            cr.ChatTurnRunner()._reconcile_tasks(chat, run, final_state)
        log = CommandLog.objects.filter(model_run=run, agent="tasks").order_by("-id").first()
        self.assertIsNotNone(log)
        tasks = _json.loads(log.stdout)
        self.assertEqual([t["status"] for t in tasks], ["completed", "pending", "completed"])
        self.assertEqual(log.command, "2/3 done")

    def test_reconcile_is_silent_when_nothing_flips(self):
        from dashboard.models import Chat, Project
        from dashboard.services import chat_runner as cr
        from dashboard.services import project_sandbox

        project = Project.objects.create(
            name="Rec2", slug=project_sandbox.unique_project_slug("Rec2"), origin="new")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, settings={})
        run = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")

        class _Auditor:
            def __init__(self, model=None, host=None):
                pass

            def complete(self, prompt, **kw):
                from src.llm.LLMResponse import LLMResponse

                return LLMResponse(text="COMPLETED:")  # audit confirms nothing

        final_state = {"tasks": [{"content": "Write docs", "status": "pending"}]}
        with patch("src.llm.OllamaBackend.OllamaBackend", _Auditor):
            cr.ChatTurnRunner()._reconcile_tasks(chat, run, final_state)
        self.assertFalse(CommandLog.objects.filter(model_run=run, agent="tasks").exists())


class RepeatedQuestionBreakerTests(TestCase):
    """A model re-asking the identical question gets its previous answer replayed after two
    answered copies — the user is never blocked by the same question a third time."""

    def test_third_identical_question_reuses_the_previous_answer(self):
        from dashboard.models import Chat, PendingInteraction, Project
        from dashboard.services import project_sandbox
        from dashboard.services.chat_runner import ChatTurnRunner

        project = Project.objects.create(
            name="Loop", slug=project_sandbox.unique_project_slug("Loop"), origin="new")
        self.addCleanup(project.delete)
        chat = Chat.objects.create(project=project, settings={})
        run = ModelRun.objects.create(chat=chat, model_name="agent", status=RunStatus.RUNNING, user_prompt="x")
        q = "It seems the manage.py file is missing. Did you create the Django project correctly?"
        for _ in range(2):
            PendingInteraction.objects.create(
                model_run=run, kind=PendingInteraction.Kind.QUESTION, prompt=q,
                status=PendingInteraction.Status.ANSWERED, answer="Yes")

        class _StubRunner:
            def _command(self, *a, **k):
                pass

        out = ChatTurnRunner()._handle_interaction(
            _StubRunner(), run, "question", {"prompt": q, "options": ["Yes", "No"]}, False)
        self.assertEqual(out, "Yes")
        self.assertFalse(PendingInteraction.objects.filter(
            model_run=run, status=PendingInteraction.Status.PENDING).exists())
