"""Generated code extraction and Django workspace preparation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.benchmark import BenchmarkLoader
from src.eval.DjangoProjectScaffolder import DjangoProjectScaffolder
from src.eval.GeneratedCodeAnalyzer import GeneratedCodeAnalyzer

from .commands import _extract_fence_path, materialize_path_marked_code_blocks
from .workspaces import safe_workspace_path


SKIP_DIRS = {"tests", "__pycache__", ".pytest_cache", ".venv", "venv", "env", "node_modules"}
DJANGO_APP_FILES = {
    "admin.py",
    "apps.py",
    "forms.py",
    "middleware.py",
    "models.py",
    "serializers.py",
    "signals.py",
    "urls.py",
    "views.py",
}
ROOT_SCAFFOLD_FILES = {"manage.py", "pytest.ini", "settings.py", "urls_root.py"}


@dataclass(frozen=True)
class MaterializationResult:
    """Files written from a model response plus user-facing system messages."""

    written_paths: list[str]
    messages: list[str]


@dataclass(frozen=True)
class CodeBlock:
    """A fenced code block extracted from model prose."""

    meta: str
    content: str


def expected_files_for_benchmark(benchmark) -> list[str]:
    """Return task expected_files metadata when the benchmark points at a YAML task."""
    loader = BenchmarkLoader()
    for task_id in _task_id_candidates(benchmark):
        try:
            return list(loader.load(task_id).expected_files)
        except FileNotFoundError:
            continue
    return []


def materialize_generated_code(
    response_text: str,
    workspace: Path,
    expected_files: list[str] | tuple[str, ...] = (),
) -> MaterializationResult:
    """Write generated code blocks to workspace files."""
    written = materialize_path_marked_code_blocks(response_text, workspace)
    if written:
        return MaterializationResult(written_paths=written, messages=[])

    blocks = [block for block in _plain_code_blocks(response_text) if block.content.strip()]
    safe_expected = [_normalize_expected_path(path) for path in expected_files if path]
    safe_expected = [path for path in safe_expected if path]
    if blocks and safe_expected:
        # Map the unlabelled code blocks onto the expected files in order, so that a
        # model that returns clean ```python blocks (without a `# path` first line)
        # still produces written files instead of nothing.
        written_paths: list[str] = []
        for block, rel_path in zip(blocks, safe_expected):
            target = safe_workspace_path(workspace, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(block.content.rstrip() + "\n", encoding="utf-8")
            written_paths.append(rel_path)
        return MaterializationResult(
            written_paths=written_paths,
            messages=[
                f"Mapped {len(written_paths)} plain fenced code block(s) onto benchmark "
                f"expected_files in order: {', '.join(written_paths)}."
            ],
        )

    if blocks and not safe_expected:
        return MaterializationResult(
            written_paths=[],
            messages=["Plain fenced code was found, but no benchmark expected_files metadata was available."],
        )
    return MaterializationResult(written_paths=[], messages=[])


def scaffold_django_workspace(workspace: Path) -> list[str]:
    """Create the minimal Django project files needed to test generated apps."""
    files = _django_source_files(workspace)
    if not files:
        return []

    analyzer = GeneratedCodeAnalyzer(files)
    if not analyzer.apps:
        return []

    written: list[str] = []
    for app in analyzer.apps:
        written.extend(_ensure_app_skeleton(workspace, app, files))
    written.extend(_write_project_scaffold(workspace, analyzer))
    return written


def _task_id_candidates(benchmark) -> list[str]:
    candidates: list[str] = []
    prompt_template = getattr(benchmark, "prompt_template", None)
    if prompt_template is not None and getattr(prompt_template, "slug", ""):
        candidates.append(prompt_template.slug)

    benchmark_template = getattr(benchmark, "benchmark_template", None)
    if benchmark_template is not None:
        nested_prompt = getattr(benchmark_template, "prompt_template", None)
        if nested_prompt is not None and getattr(nested_prompt, "slug", ""):
            candidates.append(nested_prompt.slug)
        slug = getattr(benchmark_template, "slug", "")
        if slug:
            candidates.append(slug)
            for suffix in ("-sablonas", "-template"):
                if slug.endswith(suffix):
                    candidates.append(slug[: -len(suffix)])

    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _plain_code_blocks(response_text: str) -> list[CodeBlock]:
    blocks: list[CodeBlock] = []
    current_meta: str | None = None
    buffer: list[str] = []
    for raw_line in response_text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("```"):
            if current_meta is not None:
                if not _extract_fence_path(current_meta):
                    blocks.append(CodeBlock(meta=current_meta, content="\n".join(buffer)))
                current_meta = None
                buffer = []
                continue
            current_meta = line[3:].strip()
            buffer = []
            continue
        if current_meta is not None:
            buffer.append(line)
    return blocks


def _normalize_expected_path(path: str) -> str:
    cleaned = path.strip().strip("'\"`").replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        return ""
    return cleaned


def _django_source_files(workspace: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in workspace.rglob("*.py"):
        relative = path.relative_to(workspace).as_posix()
        parts = Path(relative).parts
        if not _is_django_app_source(parts):
            continue
        files[relative] = path.read_text(encoding="utf-8", errors="replace")
    return files


def _is_django_app_source(parts: tuple[str, ...]) -> bool:
    if len(parts) < 2:
        return False
    if any(part in SKIP_DIRS for part in parts):
        return False
    if parts[0] in ROOT_SCAFFOLD_FILES:
        return False
    return parts[-1] in DJANGO_APP_FILES or "templatetags" in parts


def _ensure_app_skeleton(workspace: Path, app: str, files: dict[str, str]) -> list[str]:
    written: list[str] = []
    app_dir = safe_workspace_path(workspace, app)
    app_dir.mkdir(exist_ok=True)
    init_file = app_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
        written.append(f"{app}/__init__.py")
    apps_py = app_dir / "apps.py"
    if f"{app}/apps.py" not in files and not apps_py.exists():
        apps_py.write_text(_apps_py(app), encoding="utf-8")
        written.append(f"{app}/apps.py")
    return written


def _write_project_scaffold(workspace: Path, analyzer: GeneratedCodeAnalyzer) -> list[str]:
    extra_apps = ",\n    ".join(f'"{app}"' for app in analyzer.apps)
    if extra_apps:
        extra_apps += ","
    extra_middleware = ",\n    ".join(f'"{item}"' for item in analyzer.middleware)
    if extra_middleware:
        extra_middleware += ","
    auth_line = f'AUTH_USER_MODEL = "{analyzer.custom_user}"' if analyzer.custom_user else ""
    files = {
        "settings.py": DjangoProjectScaffolder.SETTINGS_PY.format(
            extra_apps=extra_apps,
            extra_middleware=extra_middleware,
            auth_user_model=auth_line,
        ),
        "urls_root.py": DjangoProjectScaffolder.URLS_ROOT_PY.format(
            includes=(
                "\n".join(
                    f'    path("", include("{app}.urls")),'
                    for app in analyzer.apps
                    if analyzer.has_urls(app)
                )
                or "    # no urls"
            )
        ),
        "pytest.ini": DjangoProjectScaffolder.PYTEST_INI,
        "manage.py": _manage_py(),
    }
    written: list[str] = []
    for relative, content in files.items():
        target = safe_workspace_path(workspace, relative)
        target.write_text(content, encoding="utf-8")
        written.append(relative)
    return written


def _apps_py(app: str) -> str:
    class_name = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", app) if part)
    if not class_name or not class_name[0].isalpha():
        class_name = "GeneratedApp"
    return (
        "from django.apps import AppConfig\n\n\n"
        f"class {class_name}Config(AppConfig):\n"
        '    default_auto_field = "django.db.models.BigAutoField"\n'
        f'    name = "{app}"\n'
    )


def _manage_py() -> str:
    return (
        "#!/usr/bin/env python\n"
        "import os\n"
        "import sys\n\n\n"
        "def main():\n"
        '    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")\n'
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )
