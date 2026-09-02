"""Markdown code-block extractor: turns an LLM response into a ``path -> code`` map."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import ClassVar, Iterable, Iterator, Pattern


class CodeBlockExtractor:
    """
    Extract a ``path -> code`` mapping from an LLM markdown response.

    Models label files in many different ways. This extractor recognises all of
    the common formats so that real code is never silently dropped:

      * first line inside the block: ``# blog/models.py``
      * ``# path: blog/models.py`` / ``# File: blog/models.py``
      * ``// blog/models.py``, ``<!-- x.html -->``, ``{# x.html #}`` comments
      * path in the fence info string: ```` ```python blog/models.py ```` or
        ```` ```python:blog/models.py ````
      * a markdown heading / path line right before the block: ``### blog/models.py``
      * no path at all -> fall back to ``expected_files`` in order

    Guarantee: when the response contains at least one non-empty code block and
    ``expected_files`` is supplied, the returned mapping is never empty.
    """

    #: File extensions used to tell a real path token apart from prose.
    CODE_SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {
            ".py",
            ".html",
            ".htm",
            ".css",
            ".js",
            ".json",
            ".txt",
            ".md",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".env",
        }
    )

    #: Bare language words that may appear as a fence info string with no path.
    LANG_WORDS: ClassVar[frozenset[str]] = frozenset(
        {
            "python",
            "py",
            "python3",
            "html",
            "css",
            "js",
            "javascript",
            "json",
            "bash",
            "sh",
            "shell",
            "text",
            "plaintext",
            "django",
            "jinja",
            "jinja2",
            "ini",
            "yaml",
            "yml",
            "toml",
            "md",
            "markdown",
            "diff",
            "console",
        }
    )

    #: ``# File: x`` / ``// path = x`` style labels.
    _LABEL_RE: ClassVar[Pattern[str]] = re.compile(
        r"^\s*(?:#|//)\s*(?:file|path)\s*[:=]\s*(?P<path>\S+)", re.IGNORECASE
    )
    #: ``# blog/models.py`` / ``// blog/models.py`` single-token comment.
    _COMMENT_RE: ClassVar[Pattern[str]] = re.compile(r"^\s*(?:#|//)\s*(?P<path>\S+)\s*$")
    #: ``<!-- blog/templates/x.html -->`` HTML comment.
    _HTML_RE: ClassVar[Pattern[str]] = re.compile(r"^\s*<!--\s*(?P<path>\S+?)\s*-->\s*$")
    #: ``{# blog/templates/x.html #}`` Django comment.
    _DJANGO_RE: ClassVar[Pattern[str]] = re.compile(r"^\s*\{#\s*(?P<path>\S+?)\s*#\}\s*$")
    #: ``### blog/models.py`` markdown heading.
    _HEADING_RE: ClassVar[Pattern[str]] = re.compile(r"^\s*#{1,6}\s*(?P<path>\S+)")

    def extract(
        self,
        text: str,
        expected_files: Iterable[str] | None = None,
    ) -> dict[str, str]:
        """
        Return a ``path -> code`` mapping extracted from ``text``.

        ``expected_files`` is used as a fallback: any code block whose path could
        not be detected is assigned the next unused expected path, in order.

        Raises:
            ValueError: if ``text`` contained at least one non-empty fenced code
                block but no file could be extracted from any of them (every
                detection strategy failed and the ``expected_files`` fallback was
                exhausted/empty). The message includes the first 500 characters of
                the raw response so the Critic can see what the model actually sent.
        """
        expected = [path for path in (self._clean(item) for item in (expected_files or [])) if path]
        result: dict[str, str] = {}
        saw_code_block = False

        for info, body, preceding in self._iter_blocks(text):
            code = body.strip("\n")
            if not code.strip():
                continue
            saw_code_block = True
            path = (
                self._path_from_info(info)
                or self._path_from_first_line(code)
                or self._path_from_heading(preceding)
            )
            stripped = self._strip_marker(code)
            if not path:
                path = self._next_expected(expected, result)
                if path:
                    print(f"[CodeBlockExtractor] No path found in block, assigning to {path}")
            path = self._clean(path)
            if not path:
                continue
            result[path] = stripped.strip("\n") + "\n"

        if not result and saw_code_block:
            preview = (text or "").strip()[:500]
            raise ValueError(
                "Found fenced code block(s) but could not resolve any file path "
                "(no path comment/heading/fence-info and no expected_files to fall "
                f"back on). First 500 chars of the response:\n{preview}"
            )

        return result

    # ------------------------------------------------------------------ blocks
    def _iter_blocks(self, text: str) -> Iterator[tuple[str, str, str]]:
        """Yield ``(info_string, body, preceding_line)`` for each fenced block."""
        lines = text.splitlines()
        index = 0
        last_nonblank = ""
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped.startswith("```"):
                info = stripped[3:].strip()
                body_lines: list[str] = []
                index += 1
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    body_lines.append(lines[index])
                    index += 1
                index += 1  # consume the closing fence
                yield info, "\n".join(body_lines), last_nonblank
                last_nonblank = ""
                continue
            if stripped:
                last_nonblank = stripped
            index += 1

    # ---------------------------------------------------------------- detection
    def _path_from_info(self, info: str) -> str | None:
        """Detect a path inside the fence info string (``python blog/models.py``)."""
        info = info.strip().strip("`")
        if not info:
            return None
        if ":" in info and " " not in info:  # ```python:blog/models.py
            candidate = info.split(":", 1)[1]
            return candidate if self._looks_like_path(candidate) else None
        tokens = info.split()
        if not tokens:
            return None
        if len(tokens) == 1:
            return tokens[0] if self._looks_like_path(tokens[0]) else None
        for token in tokens[1:] + tokens[:1]:  # skip the language token first
            cleaned = self._strip_kv(token)
            if self._looks_like_path(cleaned):
                return cleaned
        return None

    def _path_from_first_line(self, code: str) -> str | None:
        """Detect a path comment on the first non-empty line of the block body."""
        for line in code.splitlines():
            if line.strip():
                return self._path_from_comment(line)
        return None

    def _path_from_comment(self, line: str) -> str | None:
        for pattern in (self._LABEL_RE, self._COMMENT_RE, self._HTML_RE, self._DJANGO_RE):
            match = pattern.match(line)
            if match and self._looks_like_path(match.group("path")):
                return match.group("path")
        return None

    def _path_from_heading(self, line: str) -> str | None:
        """Detect a path on the line immediately preceding the fence."""
        if not line:
            return None
        stripped = line.strip().strip("*`")
        heading = self._HEADING_RE.match(stripped)
        if heading and self._looks_like_path(heading.group("path")):
            return heading.group("path")
        label = re.match(r"^\s*(?:file|path)\s*[:=]\s*(?P<path>\S+)\s*$", stripped, re.IGNORECASE)
        if label and self._looks_like_path(label.group("path")):
            return label.group("path")
        if self._looks_like_path(stripped):
            return stripped
        return None

    def _strip_marker(self, code: str) -> str:
        """Drop the leading ``# path`` marker line from the file body if present."""
        lines = code.splitlines()
        for position, line in enumerate(lines):
            if not line.strip():
                continue
            if self._path_from_comment(line) is not None:
                return "\n".join(lines[position + 1:]).lstrip("\n")
            return code
        return code

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _next_expected(expected: list[str], result: dict[str, str]) -> str | None:
        for candidate in expected:
            if candidate not in result:
                return candidate
        return None

    def _looks_like_path(self, token: str | None) -> bool:
        cleaned = self._clean(token)
        if not cleaned or cleaned.lower() in self.LANG_WORDS:
            return False
        # A real path is a single token: reject prose (e.g. a heading line like
        # "Here is the code for the `blog/models.py` file") that merely contains a
        # slash. Such lines must fall through to the expected_files fallback instead
        # of becoming a garbage-named file.
        if any(ch.isspace() for ch in cleaned) or len(cleaned) > 120:
            return False
        if "/" in cleaned:
            return True
        return PurePosixPath(cleaned).suffix.lower() in self.CODE_SUFFIXES

    @staticmethod
    def _strip_kv(token: str) -> str:
        """Reduce ``title="blog/models.py"`` style tokens to their value."""
        if "=" in token:
            token = token.split("=", 1)[1]
        return token

    @staticmethod
    def _clean(token: str | None) -> str:
        if not token:
            return ""
        cleaned = token.strip().strip("`'\"<>:,").replace("\\", "/").strip()
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if not cleaned or cleaned.startswith("/"):
            return ""
        if ".." in PurePosixPath(cleaned).parts:
            return ""
        return cleaned
