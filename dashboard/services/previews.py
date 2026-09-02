"""Local preview server lifecycle management (benchmark model runs + project sandboxes)."""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

from django.utils import timezone

from dashboard.models import CommandLog, ModelRun, PreviewServer


class PreviewService:
    """Start and stop generated Django project previews (a ``manage.py runserver``)."""

    # ------------------------------------------------------------------ benchmark model runs
    def start(self, model_run: ModelRun) -> PreviewServer:
        workspace = Path(model_run.workspace_path)
        preview, _ = PreviewServer.objects.get_or_create(model_run=model_run)

        # The dynamic agent only writes app source files; scaffold a full runnable project
        # (manage.py + settings + migrated SQLite db) around them on demand before serving.
        build_error = self._ensure_built(model_run)

        manage_py = workspace / "manage.py"
        if not manage_py.exists():
            return self._fail(preview, build_error or "No manage.py found in this model workspace.")
        if self._running_and_alive(preview):
            return preview

        started = time.perf_counter()
        preview = self._spawn(preview, workspace)
        if preview.status == PreviewServer.Status.RUNNING:
            model_run.preview_startup_duration_seconds = time.perf_counter() - started
            model_run.save(update_fields=["preview_startup_duration_seconds", "updated_at"])
            CommandLog.objects.create(
                model_run=model_run, kind="preview", command=preview.command,
                cwd=str(workspace), stdout=preview.url,
            )
        return preview

    def stop(self, model_run: ModelRun) -> PreviewServer:
        preview, _ = PreviewServer.objects.get_or_create(model_run=model_run)
        preview = self._kill(preview)
        CommandLog.objects.create(model_run=model_run, kind="preview", command=["stop-preview"], cwd=model_run.workspace_path)
        return preview

    def restart(self, model_run: ModelRun) -> PreviewServer:
        self.stop(model_run)
        return self.start(model_run)

    # ------------------------------------------------------------------ project sandboxes
    def start_project(self, project) -> PreviewServer:
        workspace = Path(project.sandbox_path)
        preview, _ = PreviewServer.objects.get_or_create(project=project)
        manage_py = workspace / "manage.py"
        # PROTOTYPE PHASE — a standalone HTML/CSS/JS prototype (no Django yet): serve it statically so
        # the user can review the design in Preview BEFORE any startproject. We must NOT scaffold a
        # Django project here — that would clobber the upcoming real `django-admin startproject`.
        if not manage_py.exists():
            if not _has_frontend(workspace):
                return self._fail(preview, "Nothing to preview yet — build the front-end prototype first.")
            if self._running_and_alive(preview):
                return preview
            return self._spawn_static(preview, workspace)
        # DJANGO PHASE — a real project exists: build (migrate-only for a real, unmarked project; full
        # scaffold for an agent-source-only one) then runserver.
        build_error = self._ensure_built(_SandboxRef(str(workspace)))
        if not (workspace / "manage.py").exists():
            return self._fail(preview, build_error or "Could not build a runnable project (no manage.py).")
        # WHAT to serve: the Django app — unless it wires NO pages while user-authored front-end
        # files exist. That mixed state (a prototype plus a premature scaffold / agent-written
        # manage.py) happens mid design-approval: the user must see the PROTOTYPE they are being
        # asked to approve, not Django's default placeholder page. Once routes exist, Django wins.
        serve_static = False
        if _has_frontend(workspace):
            try:
                from .route_discovery import discover_routes

                serve_static = not discover_routes(workspace, static=False)
            except Exception:
                # Discovery flaked (e.g. subprocess timeout while the CPU is pinned by the LLM):
                # with a front-end present, the SAFER visual is the prototype — an unroutable
                # Django rocket page is never the right thing to show.
                serve_static = True
        if self._running_and_alive(preview):
            # Honour the running server only when it already serves the RIGHT thing — a stale
            # static server yields to runserver once routes exist, and vice versa.
            if ("http.server" in (preview.command or [])) == serve_static:
                return preview
            self._kill(preview)
        if serve_static:
            return self._spawn_static(preview, workspace)
        return self._spawn(preview, workspace)

    def stop_project(self, project) -> PreviewServer:
        preview, _ = PreviewServer.objects.get_or_create(project=project)
        return self._kill(preview)

    def restart_project(self, project) -> PreviewServer:
        self.stop_project(project)
        return self.start_project(project)

    # ------------------------------------------------------------------ liveness
    @staticmethod
    def _port_alive(preview: PreviewServer) -> bool:
        """True when something actually accepts connections on the preview's port."""
        if not preview.port:
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(preview.port)), timeout=0.25):
                return True
        except OSError:
            return False

    def _running_and_alive(self, preview: PreviewServer) -> bool:
        """A RUNNING row counts only while its server still answers. _launch marks the row
        RUNNING straight after Popen, so a crashed runserver (broken agent code) or a host
        reboot leaves a stale RUNNING row behind — those must read as dead so the UI can
        surface the failure and a start can respawn them."""
        if preview.status != PreviewServer.Status.RUNNING or not preview.pid:
            return False
        started = preview.started_at
        if started and (timezone.now() - started).total_seconds() < 6:
            return True  # just spawned — still binding the port; do not respawn over it
        return self._port_alive(preview)

    def reconcile(self, preview: PreviewServer | None) -> PreviewServer | None:
        """Called by the status polls: demote a RUNNING row whose server has died, so the
        panel shows the failure (with its Retry affordance) instead of a dead iframe."""
        if preview and preview.status == PreviewServer.Status.RUNNING and not self._running_and_alive(preview):
            message = "The preview server stopped unexpectedly."
            tail = _log_tail(_preview_workspace(preview))
            if tail:
                message += "\n\n" + tail
            return self._fail(preview, message)
        return preview

    # ------------------------------------------------------------------ shared internals
    @staticmethod
    def _ensure_built(owner) -> str:
        """Build a runnable project in the workspace; return an error string (empty on success)."""
        try:
            from .project_builder import ensure_runnable_project

            build = ensure_runnable_project(owner)
            return build.get("error") or ""
        except Exception as exc:  # pragma: no cover - defensive
            return f"Could not build a runnable project: {exc}"

    @staticmethod
    def _fail(preview: PreviewServer, message: str) -> PreviewServer:
        preview.status = PreviewServer.Status.FAILED
        preview.error_message = message
        preview.save(update_fields=["status", "error_message", "updated_at"])
        return preview

    def _spawn(self, preview: PreviewServer, workspace: Path) -> PreviewServer:
        from .project_venv import venv_python

        port = _free_port()
        python_exec = venv_python(workspace) or sys.executable  # the project's own venv when ready
        command = [python_exec, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload", "--verbosity", "0"]
        return self._launch(preview, workspace, command, port)

    def _spawn_static(self, preview: PreviewServer, workspace: Path) -> PreviewServer:
        """Serve the standalone prototype (plain HTML/CSS/JS) over a static server — used in the
        design-first PROTOTYPE phase, before any Django project exists."""
        port = _free_port()
        command = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(workspace)]
        return self._launch(preview, workspace, command, port)

    def _launch(self, preview: PreviewServer, workspace: Path, command: list, port: int) -> PreviewServer:
        # The dashboard's own runserver exports DJANGO_SETTINGS_MODULE=webapp.settings (manage.py
        # setdefault); a spawned sandbox manage.py ALSO uses setdefault, so the inherited value
        # wins and the child dies importing the dashboard's settings from the sandbox cwd. Scrub
        # every var that can point the child at the wrong interpreter/settings.
        env = dict(os.environ)
        for var in ("DJANGO_SETTINGS_MODULE", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(var, None)
        # Preview shim (sitecustomize.py): no-ops Django's XFrameOptionsMiddleware in the CHILD
        # so the generated app can render inside the Preview <iframe> — startproject installs the
        # clickjacking middleware, whose X-Frame-Options: DENY makes the browser refuse to embed
        # the page ("127.0.0.1 refused to connect") even though the server answers 200.
        env["PYTHONPATH"] = str(_SHIM_DIR)
        log_path = workspace / _PREVIEW_LOG
        try:
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError:
            log_file = subprocess.DEVNULL
        try:
            process = subprocess.Popen(
                command, cwd=str(workspace), env=env,
                stdout=subprocess.DEVNULL, stderr=log_file, text=True,
            )
        except Exception as exc:
            return self._fail(preview, str(exc))
        finally:
            if log_file is not subprocess.DEVNULL:
                log_file.close()  # the child holds its own handle; ours is only a leak
        # An instant crash (broken settings, import error, port clash) exits within a moment —
        # catch it here so the UI gets the real traceback instead of a RUNNING row that a later
        # poll demotes to an unexplained "stopped unexpectedly".
        time.sleep(1.0)
        if process.poll() is not None:
            return self._fail(preview, _crash_message(workspace, process.returncode))
        preview.status = PreviewServer.Status.RUNNING
        preview.port = port
        preview.url = f"http://127.0.0.1:{port}/"
        preview.pid = process.pid
        preview.command = command
        preview.error_message = ""
        preview.started_at = timezone.now()
        preview.stopped_at = None
        preview.save(update_fields=[
            "status", "port", "url", "pid", "command", "error_message",
            "started_at", "stopped_at", "updated_at",
        ])
        # Refresh the discovered-pages cache in the background so the Preview panel's links
        # bar (and its default page pick) reflects the code this server is now running.
        try:
            from .route_discovery import refresh_routes_async

            # A static server must publish its PROTOTYPE pages even when a (premature) manage.py
            # exists — key the discovery off what this server actually serves.
            refresh_routes_async(workspace, static=("http.server" in [str(part) for part in command]))
        except Exception:
            pass
        return preview

    @staticmethod
    def _kill(preview: PreviewServer) -> PreviewServer:
        if preview.pid:
            if platform.system().lower().startswith("win"):
                subprocess.run(["taskkill", "/PID", str(preview.pid), "/T", "/F"], check=False)
            else:
                try:
                    os.kill(preview.pid, 15)
                except ProcessLookupError:
                    pass
        preview.status = PreviewServer.Status.STOPPED
        preview.pid = None
        preview.stopped_at = timezone.now()
        preview.save(update_fields=["status", "pid", "stopped_at", "updated_at"])
        return preview


class _SandboxRef:
    """Workspace-only adapter so ``ensure_runnable_project`` accepts a project sandbox path."""

    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path


#: Captures the spawned server's stderr so a crash is diagnosable (it used to go to DEVNULL).
_PREVIEW_LOG = ".kursinis_preview.log"

#: Injected onto the preview child's PYTHONPATH — holds sitecustomize.py (iframe-embed shim).
_SHIM_DIR = Path(__file__).resolve().parent / "preview_shim"


def _log_tail(workspace: Path | None, limit: int = 2000) -> str:
    """The tail of the preview server's stderr log (empty when absent/unreadable)."""
    if workspace is None:
        return ""
    try:
        text = (workspace / _PREVIEW_LOG).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-limit:]


def _crash_message(workspace: Path, returncode) -> str:
    message = f"The preview server exited immediately (code {returncode})."
    tail = _log_tail(workspace)
    if tail:
        message += "\n\n" + tail
    return message


def _preview_workspace(preview: PreviewServer) -> Path | None:
    """The workspace the preview's server ran in, derived from its owner (None when unknown)."""
    if preview.model_run_id and preview.model_run.workspace_path:
        return Path(preview.model_run.workspace_path)
    if preview.project_id and preview.project.sandbox_path:
        return Path(preview.project.sandbox_path)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


#: Directories that must never count as "the project's front-end": the per-project venv ships
#: hundreds of Django/DRF admin templates (site-packages/**/*.html), which made every sandbox
#: look like it had a prototype the moment pip finished.
_FRONTEND_SKIP_DIRS = {".venv", "venv", "env", "node_modules", ".git", "__pycache__", "site-packages", "staticfiles"}


def _has_frontend(workspace: Path) -> bool:
    """True if the sandbox has any USER-authored HTML to serve as a static prototype.

    Walks with pruning (skips venv/node_modules/… subtrees entirely) — both for correctness
    (vendored templates are not the project's design) and to keep the check cheap."""
    import os

    try:
        for dirpath, dirnames, filenames in os.walk(workspace):
            dirnames[:] = [d for d in dirnames if d not in _FRONTEND_SKIP_DIRS]
            if any(f.lower().endswith((".html", ".htm")) for f in filenames):
                return True
        return False
    except OSError:
        return False
