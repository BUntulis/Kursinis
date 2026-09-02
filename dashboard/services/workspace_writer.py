"""Permanent workspace writer for agent-generated files.

This is intentionally separate from the throwaway evaluation sandbox
(``src/eval/DjangoSandbox``): the sandbox runs in a temp directory that is
deleted after pytest, while ``WorkspaceWriter`` persists every generation under
``<BASE_DIR>/workspaces/run_<model_run_id>/`` so the user can browse, preview,
and reuse the code the model produced.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings

from .workspaces import safe_workspace_path


class WorkspaceWriter:
    """Write a ``path -> content`` map to a permanent per-run workspace on disk."""

    #: Root directory that holds every permanent run workspace.
    BASE_DIR = Path(settings.BASE_DIR) / "workspaces"

    def get_workspace_path(self, model_run_id: int | str) -> Path:
        """Return the workspace root for one model run (``workspaces/run_<id>``)."""
        return self.BASE_DIR / f"run_{model_run_id}"

    def write(self, model_run_id: int | str, files: dict[str, str]) -> Path:
        """
        Write ``files`` to ``workspaces/run_<model_run_id>/<relative_path>``.

        Parent directories are created as needed and existing files are
        overwritten — each agent iteration intentionally replaces the previous
        attempt. Path traversal / absolute paths are rejected via
        :func:`safe_workspace_path`. Returns the workspace root path.
        """
        root = self.get_workspace_path(model_run_id)
        root.mkdir(parents=True, exist_ok=True)
        for relative_path, content in (files or {}).items():
            if not relative_path:
                continue
            try:
                target = safe_workspace_path(root, relative_path)
            except ValueError:
                # Skip unsafe paths rather than escaping the workspace.
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
        return root

    def list_files(self, model_run_id: int | str) -> list[dict]:
        """Return ``[{path, size, content}]`` for every file in the workspace."""
        root = self.get_workspace_path(model_run_id)
        results: list[dict] = []
        if not root.exists():
            return results
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            results.append({"path": relative, "size": path.stat().st_size, "content": content})
        return results
