"""Per-project virtualenvs.

Each project sandbox gets its own ``.venv`` with a default package set (config
``agent_project_packages``) pre-installed; the agent can ``pip install`` more on demand. All
Django subprocesses for a project (migrate / runserver) and the agent's shell run against that
venv's python. Benchmark temp sandboxes never get a venv → they keep using ``sys.executable``.

Creation is kicked off in the background when a project is created (``provision_async``), logged
into the chat as it goes; the first chat turn waits for readiness (``is_ready``). A ``.kursinis_ready``
marker makes it idempotent, and a per-workspace lock serializes concurrent provisioners.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from config import settings

VENV_NAME = ".venv"
_READY_MARKER = ".kursinis_ready"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(workspace: Path) -> threading.Lock:
    key = str(Path(workspace).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def venv_dir(workspace) -> Path:
    return Path(workspace) / VENV_NAME


def _bin_dir(workspace) -> Path:
    return venv_dir(workspace) / ("Scripts" if os.name == "nt" else "bin")


def is_ready(workspace) -> bool:
    """True once the venv exists AND its default packages finished installing."""
    return (venv_dir(workspace) / _READY_MARKER).exists()


def venv_python(workspace) -> str | None:
    """The project venv's python executable — but only once the venv is fully provisioned."""
    if not is_ready(workspace):
        return None
    exe = _bin_dir(workspace) / ("python.exe" if os.name == "nt" else "python")
    return str(exe) if exe.exists() else None


def shell_env(workspace) -> dict:
    """An env for the agent's shell: the project venv's bin on PATH so bare python/pip use it.

    Always scrubs the dashboard's own Django vars (DJANGO_SETTINGS_MODULE=webapp.settings etc.) —
    sandbox ``manage.py`` files use ``setdefault``, so an inherited value points every child
    Django command at the DASHBOARD's settings and crashes it (same bug the preview had)."""
    env = dict(os.environ)
    for var in ("DJANGO_SETTINGS_MODULE", "PYTHONPATH", "PYTHONHOME"):
        env.pop(var, None)
    if not is_ready(workspace):
        env.pop("VIRTUAL_ENV", None)
        return env
    bin_dir = _bin_dir(workspace)
    if bin_dir.exists():
        env["VIRTUAL_ENV"] = str(venv_dir(workspace))
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def ensure_venv(workspace, log_cb=None, timeout: int = 900) -> dict:
    """Create ``<workspace>/.venv`` + install the default packages (idempotent, locked)."""
    ws = Path(workspace)
    if not ws.exists():
        return {"ok": False, "error": "workspace does not exist"}

    def log(message: str) -> None:
        if log_cb:
            try:
                log_cb(message)
            except Exception:
                pass

    with _lock_for(ws):
        vdir = venv_dir(ws)
        marker = vdir / _READY_MARKER
        if marker.exists():
            return {"ok": True, "built": False}

        log("Creating the project virtualenv…")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(vdir)],
                cwd=str(ws), capture_output=True, text=True, timeout=timeout,
            )
        except Exception as exc:  # pragma: no cover - venv tooling failure
            return {"ok": False, "error": f"venv creation failed: {exc}"}

        exe = _bin_dir(ws) / ("python.exe" if os.name == "nt" else "python")
        if not exe.exists():
            return {"ok": False, "error": "venv python not found after creation"}

        packages = list(getattr(settings, "agent_project_packages", []) or [])
        install_ok = True
        if packages:
            log("Installing " + ", ".join(packages) + "…")
            try:
                proc = subprocess.run(
                    [str(exe), "-m", "pip", "install", "--disable-pip-version-check", *packages],
                    cwd=str(ws), capture_output=True, text=True, timeout=timeout,
                )
                if proc.returncode != 0:
                    install_ok = False
                    log("Some packages failed to install — will retry on the next turn.")
            except Exception as exc:  # pragma: no cover - network/pip/timeout failure
                install_ok = False
                log(f"pip install failed: {exc}")

        # Only mark the venv ready when the default stack actually installed. Otherwise leave NO
        # marker so is_ready() stays False — migrate/runserver/agent-shell keep falling back to
        # sys.executable (which has Django), and the next turn re-runs the install (idempotent).
        if not install_ok:
            return {"ok": False, "error": "default package install failed", "built": False}

        try:
            marker.write_text("ready\n", encoding="utf-8")
        except OSError:
            return {"ok": False, "error": "could not write ready marker", "built": False}
        log("Project environment is ready.")
        return {"ok": True, "built": True, "python": str(exe)}
