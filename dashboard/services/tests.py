"""Test command selection and result parsing."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any


COUNT_RE = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|error|errors|xfailed|xpassed)")
DJANGO_RAN_RE = re.compile(r"Ran\s+(?P<count>\d+)\s+tests?", re.IGNORECASE)
DJANGO_FAILED_RE = re.compile(r"(failures|errors|skipped)=(?P<count>\d+)")


def write_reference_tests(workspace: Path, test_directory: str, reference_tests: str) -> Path | None:
    """Write reference tests into the generated workspace when provided."""
    if not reference_tests.strip():
        return None
    target_dir = workspace / (test_directory or "tests")
    target_dir.mkdir(parents=True, exist_ok=True)
    init_file = target_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    target = target_dir / "test_reference.py"
    target.write_text(reference_tests.rstrip() + "\n", encoding="utf-8")
    return target


def write_test_suite(workspace: Path, suite: Any, index: int = 1) -> Path:
    """Write a reusable suite object to a stable test file."""
    slug = getattr(suite, "slug", f"rinkinys-{index}") or f"rinkinys-{index}"
    test_directory = getattr(suite, "default_test_directory", "tests") or "tests"
    target_dir = workspace / test_directory
    target_dir.mkdir(parents=True, exist_ok=True)
    init_file = target_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    target = target_dir / f"test_{slug.replace('-', '_')}.py"
    content = getattr(suite, "test_content", "") or "def test_placeholder():\n    assert True\n"
    target.write_text(content.rstrip() + "\n", encoding="utf-8")
    return target


def default_suite_command(workspace: Path, suite: Any, test_file: Path, configured_command: str = "") -> str:
    """Return the command for a specific test suite."""
    suite_command = getattr(suite, "default_command", "") or ""
    if configured_command.strip():
        return configured_command.strip()
    if suite_command.strip():
        return suite_command.strip()
    return f'"{sys.executable}" -m pytest "{test_file.relative_to(workspace).as_posix()}" -q'


def default_test_command(workspace: Path, test_directory: str, configured_command: str = "") -> str:
    """Return the configured test command or a simple framework-aware default."""
    if configured_command.strip():
        return configured_command.strip()
    manage_py = workspace / "manage.py"
    if manage_py.exists():
        return f'"{sys.executable}" manage.py test'
    target = test_directory or "tests"
    return f'"{sys.executable}" -m pytest {target} -q'


def parse_test_output(stdout: str, stderr: str, exit_code: int, duration_seconds: float | None = None) -> dict[str, object]:
    """Parse common pytest and Django test-runner summaries."""
    combined = "\n".join([stdout or "", stderr or ""])
    counts = {"passed": 0, "failed": 0, "skipped": 0}

    for match in COUNT_RE.finditer(combined):
        kind = match.group("kind")
        count = int(match.group("count"))
        if kind == "passed":
            counts["passed"] += count
        elif kind in {"failed", "error", "errors"}:
            counts["failed"] += count
        elif kind == "skipped":
            counts["skipped"] += count

    django_ran = DJANGO_RAN_RE.search(combined)
    if django_ran:
        total = int(django_ran.group("count"))
        failed = 0
        skipped = 0
        for match in DJANGO_FAILED_RE.finditer(combined):
            if match.group(1) == "skipped":
                skipped += int(match.group("count"))
            else:
                failed += int(match.group("count"))
        if "OK" in combined and exit_code == 0:
            failed = 0
        passed = max(total - failed - skipped, 0)
        counts = {"passed": passed, "failed": failed, "skipped": skipped}
    else:
        total = counts["passed"] + counts["failed"] + counts["skipped"]
        if total == 0 and exit_code != 0:
            counts["failed"] = 1
            total = 1

    total = counts["passed"] + counts["failed"] + counts["skipped"]
    return {
        "total": total,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "duration_seconds": duration_seconds,
        "failure_details": _failure_details(combined),
    }


def _failure_details(output: str) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED ") or stripped.startswith("ERROR ") or stripped.startswith("FAIL: "):
            details.append({"summary": stripped})
        if len(details) >= 20:
            break
    return details
