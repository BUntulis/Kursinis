"""Generate pytest reference tests for a benchmark from a structured prompt (one LLM call).

Used when a benchmark is created from the Prompt Writer with ``generate_tests`` on: instead of
the user hand-writing ``reference_tests``, the model plans + writes acceptance tests that the
benchmark then evaluates each model against.
"""
from __future__ import annotations

import ast
import re

_SYSTEM = "You are a senior Django test engineer who writes precise, runnable pytest tests."


def generate_reference_tests(prompt: str, backend=None) -> str:
    """Return a pytest ``test_reference.py`` body for the spec (empty string on any failure)."""
    try:
        if backend is None:
            from src.llm.OllamaBackend import OllamaBackend

            backend = OllamaBackend()
        resp = backend.complete(
            "Write a SINGLE pytest test file that acceptance-tests a Django implementation of the "
            "spec below. Use `@pytest.mark.django_db`, import the app's models/views, create objects, "
            "and assert the key behaviours and relations. Keep it focused (a handful of clear tests). "
            "Output ONLY the test code inside one ```python code block.\n\nSPEC:\n" + (prompt or "")[:2500],
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=2000,
        )
        return _extract_code(resp.text or "")
    except Exception:
        return ""


def _extract_code(text: str) -> str:
    """Pull the python from a fenced block, but ONLY if it parses.

    Returning prose verbatim would be written to ``test_reference.py`` and abort pytest collection
    for the whole directory (a known failure mode), so anything that isn't valid Python yields ""
    and the orchestrator falls back to the configured test suites instead.
    """
    match = re.search(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n?([\s\S]*?)```", text or "")
    candidate = (match.group(1) if match else (text or "")).strip()
    if not candidate:
        return ""
    try:
        ast.parse(candidate)
    except SyntaxError:
        return ""
    return candidate
