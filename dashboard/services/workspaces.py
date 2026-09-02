"""Workspace helpers for generated benchmark artifacts."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings


RUNS_DIR = Path(settings.BASE_DIR) / "runs"


def benchmark_workspace(benchmark_id: int) -> Path:
    """Return the root workspace for a benchmark run."""
    return RUNS_DIR / f"run_{benchmark_id:04d}"


def model_workspace(benchmark_id: int, model_slug: str) -> Path:
    """Return the isolated workspace for one model run."""
    return benchmark_workspace(benchmark_id) / model_slug


def ensure_workspace(path: Path) -> Path:
    """Create a workspace and return its resolved path."""
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def safe_workspace_path(root: Path | str, relative_path: str | Path) -> Path:
    """Resolve a relative path inside a workspace and reject path traversal."""
    workspace = Path(root).resolve()
    rel = Path(relative_path)
    if rel.is_absolute():
        raise ValueError("Absolute file paths are not allowed inside generated workspaces.")
    candidate = (workspace / rel).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError("File path escapes the generated workspace.")
    return candidate


def safe_relative_path(root: Path | str, file_path: Path | str) -> str:
    """Return a normalized relative path for a file known to be inside a workspace."""
    workspace = Path(root).resolve()
    candidate = Path(file_path).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError("File path escapes the generated workspace.")
    return candidate.relative_to(workspace).as_posix()
