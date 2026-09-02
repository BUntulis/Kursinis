"""Generated file indexing and safe file reads."""
from __future__ import annotations

import hashlib
from pathlib import Path

from django.utils import timezone

from dashboard.models import GeneratedFile, ModelRun

from .workspaces import safe_relative_path, safe_workspace_path


SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", "node_modules", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py",
    ".html",
    ".css",
    ".js",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".md",
    ".env",
    ".ini",
    ".cfg",
}

CODE_SUFFIX_TO_KEY = {
    ".py": "python_lines",
    ".html": "html_lines",
    ".htm": "html_lines",
    ".css": "css_lines",
    ".js": "javascript_lines",
}


def index_generated_files(model_run: ModelRun) -> int:
    """Index generated files under a model workspace."""
    root = Path(model_run.workspace_path)
    if not root.exists():
        return 0

    seen: set[str] = set()
    metrics = calculate_code_metrics(root)
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = safe_relative_path(root, path)
        seen.add(relative)
        stat = path.stat()
        GeneratedFile.objects.update_or_create(
            model_run=model_run,
            relative_path=relative,
            defaults={
                "file_type": file_type_for_path(path),
                "size_bytes": stat.st_size,
                "sha256": _sha256(path),
                "modified_at": timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone()),
            },
        )

    model_run.generated_files.exclude(relative_path__in=seen).delete()
    for key, value in metrics.items():
        setattr(model_run, key, value)
    model_run.save(update_fields=[*metrics.keys(), "updated_at"])
    return len(seen)


def calculate_code_metrics(root: Path) -> dict[str, int]:
    """Calculate file, directory, line, and disk metrics under a workspace."""
    metrics = {
        "disk_usage_bytes": 0,
        "directory_count": 0,
        "file_count": 0,
        "total_lines": 0,
        "python_lines": 0,
        "html_lines": 0,
        "css_lines": 0,
        "javascript_lines": 0,
    }
    if not root.exists():
        return metrics
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_dir():
            metrics["directory_count"] += 1
            continue
        metrics["file_count"] += 1
        try:
            metrics["disk_usage_bytes"] += path.stat().st_size
        except OSError:
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES or suffix in CODE_SUFFIX_TO_KEY:
            try:
                line_count = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
            except OSError:
                line_count = 0
            metrics["total_lines"] += line_count
            key = CODE_SUFFIX_TO_KEY.get(suffix)
            if key:
                metrics[key] += line_count
    return metrics


def file_type_for_path(path: Path) -> str:
    """Return a compact display type for a generated file."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "Python"
    if suffix in {".html", ".htm"}:
        return "Template"
    if suffix in {".css", ".js"}:
        return "Static"
    if suffix in {".yaml", ".yml", ".toml", ".ini", ".cfg", ".json"}:
        return "Config"
    if suffix in {".md", ".txt"}:
        return "Text"
    if "test" in path.name.lower():
        return "Test"
    return suffix.lstrip(".").upper() or "File"


def read_generated_file(model_run: ModelRun, relative_path: str, max_bytes: int = 300_000) -> tuple[str, bool]:
    """Read a generated file safely, returning content and a binary flag."""
    target = safe_workspace_path(model_run.workspace_path, relative_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relative_path)
    data = target.read_bytes()[:max_bytes]
    if _is_binary(data):
        return "", True
    return data.decode("utf-8", errors="replace"), False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_binary(data: bytes) -> bool:
    if not data:
        return False
    return b"\0" in data
