"""Per-project sandbox lifecycle: create, import (zip / server path), delete.

Each ``Project`` owns a persistent directory under ``BASE_DIR/project_sandboxes/<slug>/``.
That directory IS the agent's workspace — ``ensure_runnable_project`` / ``db_inspector`` /
``PreviewService`` all operate on a ``.workspace_path``, so :class:`SandboxRef` (a tiny
adapter exposing ``workspace_path``) lets them work on a project unchanged.
"""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from django.conf import settings

from .workspaces import ensure_workspace, safe_workspace_path

#: Root that holds every project sandbox (sibling of the benchmark ``runs/`` dir).
PROJECT_SANDBOX_DIR = Path(settings.BASE_DIR) / "project_sandboxes"

#: Dirs we never copy when importing an existing project (heavy / machine-specific).
_IMPORT_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".venv", "venv",
                     "env", "node_modules", ".mypy_cache", ".idea", ".vscode", "db.sqlite3"}
#: Top-level files the destination project owns — never overwritten/copied when MERGING a loose chat's
#: sandbox into a real project (mirrors project_builder._SCAFFOLD_FILES + the DB + the scaffold marker).
_MERGE_SCAFFOLD_FILES = {"manage.py", "settings.py", "urls_root.py", "pytest.ini", "conftest.py",
                         "test_reference.py", "db.sqlite3", ".kursinis_scaffolded"}
#: Hard cap on imported content so a runaway upload can't fill the disk.
MAX_IMPORT_BYTES = 50 * 1024 * 1024  # 50 MB


class SandboxRef:
    """Minimal duck-typed stand-in for a ModelRun so the workspace-keyed services
    (``ensure_runnable_project`` / ``db_inspector`` / ``PreviewService``) accept a project."""

    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = str(workspace_path)


def sandbox_dir_for(slug: str) -> Path:
    return PROJECT_SANDBOX_DIR / slug


def unique_project_slug(name: str) -> str:
    """A unique, filesystem-safe slug for a project name (deduped against existing projects)."""
    from django.utils.text import slugify

    base = slugify(name or "")[:60] or "project"
    from dashboard.models import Project

    slug = base
    n = 2
    while Project.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_sandbox(project) -> Path:
    """Create (or ensure) the project's sandbox dir and persist its path on the project."""
    path = ensure_workspace(sandbox_dir_for(project.slug))
    project.sandbox_path = str(path)
    project.save(update_fields=["sandbox_path", "updated_at"])
    return path


def remove_sandbox_dir(path: str | Path) -> None:
    """Recursively delete a sandbox dir — guarded to stay strictly under PROJECT_SANDBOX_DIR."""
    if not path:
        return
    target = Path(path).resolve()
    base = PROJECT_SANDBOX_DIR.resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"Refusing to delete a path outside the sandbox root: {target}")
    if target == base:
        raise ValueError("Refusing to delete the sandbox root itself.")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


# --------------------------------------------------------------------- import: zip
def import_zip(project, uploaded_file) -> dict:
    """Extract an uploaded .zip of an existing project into the sandbox (traversal-safe).

    If every member shares a single top-level directory (the common "zip of a folder"
    shape), that wrapper directory is stripped so files land at the sandbox root.
    """
    root = Path(project.sandbox_path).resolve()
    written = 0
    total = 0
    try:
        with zipfile.ZipFile(uploaded_file) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            strip = _common_root([m.filename for m in members])
            for info in members:
                rel = _strip_prefix(info.filename, strip)
                if not rel or _should_skip(rel):
                    continue
                try:
                    target = safe_workspace_path(root, rel)
                except ValueError:
                    continue  # traversal / absolute — skip silently
                total += info.file_size
                if total > MAX_IMPORT_BYTES:
                    return {"ok": False, "error": f"Archive exceeds {MAX_IMPORT_BYTES // (1024*1024)} MB.", "files": written}
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written += 1
    except zipfile.BadZipFile:
        return {"ok": False, "error": "The uploaded file is not a valid .zip archive.", "files": 0}
    return {"ok": True, "files": written}


# --------------------------------------------------------------------- import: path
def import_path(project, src_path: str) -> dict:
    """Copy an existing project folder on this machine into the sandbox (filtered + capped)."""
    src = Path(src_path).expanduser()
    if not src.exists() or not src.is_dir():
        return {"ok": False, "error": f"Folder not found: {src_path}", "files": 0}
    root = Path(project.sandbox_path).resolve()
    src = src.resolve()
    if root == src or root in src.parents or src in root.parents:
        return {"ok": False, "error": "Source folder overlaps the sandbox.", "files": 0}
    written = 0
    total = 0
    for item in src.rglob("*"):
        # Skip symlinks (defense-in-depth): a symlink pointing outside the source tree must not
        # be dereferenced + copied into the sandbox.
        if item.is_symlink() or not item.is_file() or any(part in _IMPORT_SKIP_DIRS for part in item.parts):
            continue
        rel = item.relative_to(src).as_posix()
        try:
            target = safe_workspace_path(root, rel)
        except ValueError:
            continue
        total += item.stat().st_size
        if total > MAX_IMPORT_BYTES:
            return {"ok": False, "error": f"Folder exceeds {MAX_IMPORT_BYTES // (1024*1024)} MB.", "files": written}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(item, target)
            written += 1
        except OSError:
            continue
    return {"ok": True, "files": written}


# --------------------------------------------------------------------- merge (move chat → project)
def merge_sandbox(src_path: str | Path, dst_path: str | Path) -> dict:
    """Copy a loose chat's sandbox files into a target project's sandbox (move-into-project).

    Used when a standalone (loose) chat is moved into a real project: the loose work is folded into
    the project's shared sandbox. To stay safe it NEVER overwrites the destination — the project's
    scaffold (manage.py / settings.py / db.sqlite3 / the scaffold marker) and any pre-existing file
    are left untouched; colliding loose files are skipped and reported. Heavy/machine dirs (.venv,
    node_modules, .git, …) are skipped, traversal is blocked, and the 50 MB cap is reused.

    Returns ``{"ok", "files_copied", "skipped": [<relative posix path>, ...]}``.
    """
    src = Path(src_path or "").resolve()
    dst = Path(dst_path or "").resolve()
    if not src.exists() or not src.is_dir():
        return {"ok": True, "files_copied": 0, "skipped": []}
    if not dst.exists() or not dst.is_dir():
        return {"ok": False, "error": "Target sandbox is not ready.", "files_copied": 0, "skipped": []}
    if src == dst or src in dst.parents or dst in src.parents:
        return {"ok": False, "error": "Source and target sandboxes overlap.", "files_copied": 0, "skipped": []}

    copied = 0
    skipped: list[str] = []
    total = 0
    for item in src.rglob("*"):
        # Skip symlinks (defense-in-depth), non-files, and heavy/machine-specific dirs.
        if item.is_symlink() or not item.is_file() or any(part in _IMPORT_SKIP_DIRS for part in item.parts):
            continue
        rel = item.relative_to(src).as_posix()
        # Never clobber the destination's scaffold (top-level owned files).
        if "/" not in rel and rel in _MERGE_SCAFFOLD_FILES:
            skipped.append(rel)
            continue
        try:
            target = safe_workspace_path(dst, rel)
        except ValueError:
            continue  # traversal / absolute — skip silently
        if target.exists():
            # Don't overwrite the project's existing work — keep the destination version.
            skipped.append(rel)
            continue
        total += item.stat().st_size
        if total > MAX_IMPORT_BYTES:
            return {"ok": False, "error": f"Merge exceeds {MAX_IMPORT_BYTES // (1024*1024)} MB.",
                    "files_copied": copied, "skipped": skipped}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(item, target)
            copied += 1
        except OSError:
            continue
    return {"ok": True, "files_copied": copied, "skipped": skipped}


# --------------------------------------------------------------------- project name
def generate_project_name(prompt: str, backend=None) -> str:
    """Ask the LLM for a short project title from the prompt; fall back to a slug of the prompt."""
    fallback = _fallback_name(prompt)
    try:
        if backend is None:
            from src.llm.OllamaBackend import OllamaBackend

            backend = OllamaBackend()
        resp = backend.complete(
            "Give a short, catchy project name (2-4 words, Title Case) for this request. "
            "Reply with ONLY the name, no quotes or punctuation.\n\nRequest:\n" + (prompt or "")[:600],
            system="You name software projects concisely.",
            temperature=0.3,
            max_tokens=24,
        )
        name = re.sub(r'["`\']', "", (resp.text or "").strip().splitlines()[0] if resp.text else "").strip()
        name = re.sub(r"\s+", " ", name)[:60]
        return name or fallback
    except Exception:
        return fallback


def quick_project_name(prompt: str) -> str:
    """An INSTANT project name from the prompt (no LLM) — keeps project creation snappy.

    ``generate_project_name`` makes a blocking Ollama call; doing that in the request thread stalls
    the New-Project POST (the model has to load first), so the page appears to hang. Naming a
    project doesn't warrant a model load, so the create flow uses this fast derivation instead.
    """
    return _fallback_name(prompt)


# --------------------------------------------------------------------- helpers
def _fallback_name(prompt: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", prompt or "")[:4]
    return " ".join(w.capitalize() for w in words) or "New Project"


def _should_skip(rel_posix: str) -> bool:
    return any(part in _IMPORT_SKIP_DIRS for part in Path(rel_posix).parts)


def _common_root(names: list[str]) -> str:
    """Return the single shared top-level dir across all names, else ''."""
    tops = set()
    for name in names:
        norm = name.replace("\\", "/").lstrip("/")
        head = norm.split("/", 1)[0]
        # a file at the archive root means there is no single wrapper dir
        if "/" not in norm:
            return ""
        tops.add(head)
    return tops.pop() + "/" if len(tops) == 1 else ""


def _strip_prefix(name: str, prefix: str) -> str:
    norm = name.replace("\\", "/").lstrip("/")
    if prefix and norm.startswith(prefix):
        return norm[len(prefix):]
    return norm
