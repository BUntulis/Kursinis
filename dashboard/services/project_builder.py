"""Build a full, runnable Django project inside a model run's workspace.

The dynamic agent only writes app source files (models.py, views.py, urls.py, …) into the
model workspace. To actually RUN that code (the Preview server) and expose a real database
(the Database tab), we scaffold the surrounding project around those files:

  * ``manage.py`` — the project entrypoint,
  * ``settings.py`` — a settings module backed by a FILE SQLite database (``db.sqlite3``),
    with the detected apps / custom user / middleware wired in,
  * ``urls_root.py`` — a root URLconf (admin + each app's urls),
  * app skeletons (``__init__.py`` / ``apps.py``) for any app missing them,

then runs ``makemigrations`` + ``migrate`` to create ``db.sqlite3`` with the app's tables.

It is **best-effort and idempotent**: if a project + database already exist it is a no-op
(unless ``force=True``). A broken model solution may fail to migrate — that is reported in the
returned ``log``/``error`` and simply means the project is not runnable (as expected).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from src.eval.GeneratedCodeAnalyzer import GeneratedCodeAnalyzer

_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "node_modules", "migrations"}
_SOURCE_SUFFIXES = {".py", ".html", ".htm", ".css", ".js", ".json", ".txt", ".md", ".cfg", ".ini", ".yaml", ".yml"}
#: Files the builder owns — excluded when analysing the agent's source.
_SCAFFOLD_FILES = {"manage.py", "settings.py", "urls_root.py", "pytest.ini", "conftest.py", "test_reference.py"}
#: Written by our scaffolder so we can tell our flat scaffold apart from a REAL `django-admin
#: startproject` (which lays out `manage.py` + `<name>/settings.py`). A workspace with a `manage.py`
#: but no marker is a real project: never scaffold/migrate over it (the agent owns + migrates it).
_SCAFFOLD_MARKER = ".kursinis_scaffolded"
#: App names that would shadow stdlib / Django / our own scaffold modules on PYTHONPATH —
#: never register these as apps (a stray top-level dir named e.g. "django" would break import).
_RESERVED_APPS = {
    "django", "os", "sys", "re", "json", "settings", "urls_root", "manage", "test", "tests",
    "rest_framework", "asyncio", "typing", "abc", "config", "webapp", "dashboard", "src",
}
#: Detects a real DRF import (not a mention in a comment/docstring/string).
_DRF_IMPORT_RE = re.compile(r"(?m)^[ \t]*(?:from\s+rest_framework|import\s+rest_framework)\b")

#: Serialize concurrent builds of the SAME workspace (the dev runserver is multithreaded, and
#: Preview-start + the Database API can both trigger a build at once). Per-workspace lock.
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def _lock_for(workspace: Path) -> threading.Lock:
    key = str(workspace)
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BUILD_LOCKS[key] = lock
        return lock

MANAGE_PY = '''\
#!/usr/bin/env python
"""Auto-generated project entrypoint (Kursinis preview)."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''

SETTINGS_PY = '''\
"""Auto-generated settings for the Kursinis preview project (file-backed SQLite)."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = "kursinis-preview-secret-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "urls_root"
DATABASES = {{
    "default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": str(BASE_DIR / "db.sqlite3")}}
}}
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
{rest_framework}{extra_apps}]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
{extra_middleware}]
TEMPLATES = [
    {{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Models routinely render from a ROOT-LEVEL templates/ dir ("render(request, 'index.html')")
        # rather than per-app dirs — without this the page 500s with TemplateDoesNotExist.
        "DIRS": [str(BASE_DIR / "templates")],
        "APP_DIRS": True,
        "OPTIONS": {{
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        }},
    }}
]
STATIC_URL = "/static/"
# Serve a ROOT-LEVEL static/ dir (models put their ported prototype CSS/JS there); without it
# every `/static/...` link 404s and the page renders unstyled.
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
{auth_user_model}'''

URLS_ROOT_PY = '''\
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
{includes}]
'''


def ensure_runnable_project(model_run, *, force: bool = False, timeout: int = 180) -> dict:
    """Scaffold + migrate a runnable project in the run's workspace (idempotent)."""
    workspace = Path(model_run.workspace_path or "")
    if not workspace.exists():
        return {"ok": False, "built": False, "error": "Workspace does not exist yet."}

    db_path = workspace / "db.sqlite3"
    manage_py = workspace / "manage.py"
    marker = workspace / _SCAFFOLD_MARKER
    # A REAL `django-admin startproject` (manage.py present but NOT written by our scaffolder) owns
    # its own settings/urls and runs its own migrations — never scaffold or migrate over it, even on
    # force=True. This is what keeps the "Implement this plan" startproject from being clobbered.
    if manage_py.exists() and not marker.exists():
        return {"ok": True, "built": False,
                "db_path": str(db_path) if db_path.exists() else "", "log": "(real project — left intact)"}
    if not force and manage_py.exists() and db_path.exists():
        return {"ok": True, "built": False, "db_path": str(db_path), "log": "(already built)"}

    # Serialize concurrent builds of this workspace; re-check the guards once we hold the lock, so a
    # second waiter returns the already-built result instead of migrating again.
    with _lock_for(workspace):
        if manage_py.exists() and not marker.exists():
            return {"ok": True, "built": False,
                    "db_path": str(db_path) if db_path.exists() else "", "log": "(real project — left intact)"}
        if not force and manage_py.exists() and db_path.exists():
            return {"ok": True, "built": False, "db_path": str(db_path), "log": "(already built)"}

        sources = _collect_sources(workspace)
        analyzer = GeneratedCodeAnalyzer(sources)
        apps = _real_apps(workspace, analyzer)
        _write_scaffold(workspace, analyzer, apps)
        log, migrated = _migrate(workspace, apps, timeout)
        ok = migrated and db_path.exists()
        return {
            "ok": ok,
            "built": True,
            "apps": apps,
            "db_path": str(db_path) if ok else "",
            "log": log,
            "error": "" if ok else "The project could not be migrated (see log) — the code may not run.",
        }


# ------------------------------------------------------------------ analysis
def _collect_sources(workspace: Path) -> dict[str, str]:
    """Return ``{relpath: content}`` for the agent's source files (excluding our scaffold)."""
    root = workspace.resolve()
    files: dict[str, str] = {}
    for item in workspace.rglob("*"):
        if not item.is_file() or any(part in _SKIP_DIRS for part in item.parts):
            continue
        if item.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        try:
            rel = item.resolve().relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) == 1 and rel.name in _SCAFFOLD_FILES:
            continue
        try:
            files[rel.as_posix()] = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


def _real_apps(workspace: Path, analyzer: GeneratedCodeAnalyzer) -> list[str]:
    """Detected app dirs that actually contain Python modules (not just templates/static).

    Uses a recursive scan so apps that keep their code in sub-packages (e.g. ``blog/models/``)
    still count, and skips reserved names that would shadow stdlib/Django modules on PYTHONPATH.
    """
    apps = []
    for app in analyzer.apps:
        if app.lower() in _RESERVED_APPS:
            continue
        app_dir = workspace / app
        if not app_dir.is_dir():
            continue
        if any(
            p.is_file() and p.suffix == ".py" and not any(part in _SKIP_DIRS for part in p.parts)
            for p in app_dir.rglob("*")
        ):
            apps.append(app)
    return apps


# ------------------------------------------------------------------ scaffold
def _write_scaffold(workspace: Path, analyzer: GeneratedCodeAnalyzer, apps: list[str]) -> None:
    (workspace / "manage.py").write_text(MANAGE_PY, encoding="utf-8")
    _write_settings(workspace, analyzer, apps)
    _write_urls(workspace, analyzer, apps)
    for app in apps:
        _ensure_app_skeleton(workspace, app)
    # Mark this as OUR scaffold so a later real `django-admin startproject` is recognised as distinct
    # and never overwritten (see ensure_runnable_project).
    (workspace / _SCAFFOLD_MARKER).write_text("", encoding="utf-8")


def _write_settings(workspace: Path, analyzer: GeneratedCodeAnalyzer, apps: list[str]) -> None:
    # Only enable DRF when a real import is present (not a mention in a comment/docstring),
    # so a project that does not use it isn't broken by an unnecessary INSTALLED_APPS entry.
    uses_drf = any(_DRF_IMPORT_RE.search(content or "") for content in analyzer.files.values())
    rest = '    "rest_framework",\n' if uses_drf else ""
    extra_apps = "".join(f'    "{app}",\n' for app in apps)
    extra_mw = "".join(f'    "{item}",\n' for item in analyzer.middleware)
    # AUTH_USER_MODEL must point at an INSTALLED app, else migrate fails — only set it when the
    # custom user's app actually made it into the apps list.
    custom_user_app = analyzer.custom_user.split(".")[0] if analyzer.custom_user else None
    auth = (
        f'AUTH_USER_MODEL = "{analyzer.custom_user}"\n'
        if custom_user_app and custom_user_app in apps
        else ""
    )
    (workspace / "settings.py").write_text(
        SETTINGS_PY.format(
            rest_framework=rest,
            extra_apps=extra_apps,
            extra_middleware=extra_mw,
            auth_user_model=auth,
        ),
        encoding="utf-8",
    )


def _write_urls(workspace: Path, analyzer: GeneratedCodeAnalyzer, apps: list[str]) -> None:
    includes = "".join(
        f'    path("", include("{app}.urls")),\n'
        for app in apps
        if analyzer.has_urls(app)
    )
    (workspace / "urls_root.py").write_text(URLS_ROOT_PY.format(includes=includes), encoding="utf-8")


def _ensure_app_skeleton(workspace: Path, app: str) -> None:
    app_dir = workspace / app
    init = app_dir / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    apps_py = app_dir / "apps.py"
    if not apps_py.exists():
        cls = "".join(part.capitalize() for part in app.replace("-", "_").split("_")) or "App"
        apps_py.write_text(
            "from django.apps import AppConfig\n\n\n"
            f"class {cls}Config(AppConfig):\n"
            '    default_auto_field = "django.db.models.BigAutoField"\n'
            f'    name = "{app}"\n',
            encoding="utf-8",
        )


# ------------------------------------------------------------------ migrate
def _migrate(workspace: Path, apps: list[str], timeout: int) -> tuple[str, bool]:
    """Run makemigrations then (only if it succeeded) migrate. Returns (log, success)."""
    env = dict(os.environ)
    # The dashboard runs under DJANGO_SETTINGS_MODULE=webapp.settings; the workspace project
    # must use ITS OWN top-level `settings` module, importable because the workspace is on PYTHONPATH.
    env["DJANGO_SETTINGS_MODULE"] = "settings"
    env["PYTHONPATH"] = str(workspace) + os.pathsep + env.get("PYTHONPATH", "")
    # Use the project's own venv python once it's ready (so the app's deps resolve); benchmark
    # temp sandboxes have no venv → fall back to the dashboard interpreter.
    from .project_venv import venv_python

    python_exec = venv_python(workspace) or sys.executable
    log: list[str] = []

    def run(args: list[str]) -> int:
        """Run a manage.py command; return its exit code (-1 on timeout/exception)."""
        try:
            proc = subprocess.run(
                [python_exec, "manage.py", *args],
                cwd=str(workspace), env=env, capture_output=True, text=True, timeout=timeout,
            )
            body = ((proc.stdout or "") + (proc.stderr or "")).strip()
            log.append(f"$ manage.py {' '.join(args)} (exit {proc.returncode})\n{body}".strip())
            return proc.returncode
        except subprocess.TimeoutExpired:
            log.append(f"$ manage.py {' '.join(args)} → TIMEOUT after {timeout}s")
            return -1
        except Exception as exc:  # pragma: no cover - defensive
            log.append(f"$ manage.py {' '.join(args)} → ERROR {exc}")
            return -1

    # If makemigrations fails/times out, do NOT migrate — otherwise we'd create a DB with only
    # the contrib tables and falsely report success while the app's tables are missing.
    if run(["makemigrations", *apps, "--no-input"]) != 0:
        return "\n\n".join(log), False
    migrated = run(["migrate", "--no-input"]) == 0
    return "\n\n".join(log), migrated
