"""Agent tools: read / write / execute inside a model's run workspace.

Powers the tool-using coder. Two delivery mechanisms share the same execution:
  * native Ollama/OpenAI tool calls (for tool-capable models) — see ``TOOL_SCHEMAS``;
  * a universal text protocol (for coder models without function calling) — files are
    written as fenced code blocks (``# path`` first line) and ``READ:`` / ``RUN:`` /
    ``RUN_TESTS`` / ``DONE`` directives are parsed from the model's text.

All file paths are confined to the workspace; ``run_shell`` executes with the workspace
as the working directory (the user opted into full shell access).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import ClassVar

from .CodeBlockExtractor import CodeBlockExtractor


#: Files the scaffold/orchestrator owns — never reported as model-generated source.
_NON_SOURCE = {
    "prompt.txt", "model_response.md", "settings.py", "urls_root.py",
    "manage.py", "pytest.ini", "conftest.py", "test_reference.py",
}
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "node_modules"}
_SOURCE_SUFFIXES = {".py", ".html", ".htm", ".css", ".js", ".json", ".txt", ".md", ".cfg", ".ini", ".yaml", ".yml"}

#: Native tool schemas (OpenAI/Ollama function-calling format).
#: This is the FULL action vocabulary of the dynamic agent — the model picks ONE of
#: these each turn. ``plan`` / ``retrieve_docs`` are handled by dedicated graph nodes
#: (they call the LLM / RAG); the rest execute inside the workspace via ``dispatch``.
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "plan",
            "description": (
                "Think through the task and produce (or revise) a short implementation plan: which "
                "files, classes, fields and methods are needed. Use when you need to orient before acting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Optional focus for the plan (e.g. 'how to model the M2M relation').",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_docs",
            "description": "Search the Django documentation for a query and return the most relevant excerpts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look up in the Django docs."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a clarifying question when the request is ambiguous and you cannot "
                "safely assume an answer. Offer concrete options when possible. Prefer asking a few "
                "sharp questions UP FRONT over guessing. Do not ask about things you can decide yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The single clarifying question."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 concrete answer choices (the UI also offers a free-text 'Other').",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files currently in the project workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the project (relative to the project root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents across the workspace for a regex/substring; returns matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex (or plain substring) to search for."},
                    "path": {"type": "string", "description": "Optional file or subdirectory to scope the search to."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a source file in the already-initialised Django project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "A SHORT relative file path like 'blog/models.py'. It MUST be a path, "
                            "never a sentence, description, or shell command."
                        ),
                    },
                    "content": {"type": "string", "description": "The full file contents (real newlines, not '\\n')."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Make a targeted edit to an existing file by replacing an exact snippet. Prefer this over "
                "rewriting the whole file for small changes. The 'search' text must match exactly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "search": {"type": "string", "description": "Exact existing text to find."},
                    "replace": {"type": "string", "description": "Text to replace it with."},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in the project workspace and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the benchmark's reference tests against the current code and return pass/fail output.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_tasks",
            "description": (
                "Maintain a visible task list (todo) for multi-step work. Send the FULL list every "
                "time (it REPLACES the previous one); mark exactly one task 'in_progress' and flip "
                "tasks to 'completed' as you finish them. Use it to plan the build up front and to "
                "show the user your progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "The complete, ordered task list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Short imperative task, e.g. 'Create the Post model'."},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Declare the task complete (call only after the code is written and tests pass).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

#: Maps native tool-call names to the agent's canonical action vocabulary (= graph node names).
CANONICAL_TOOL = {
    "plan": "plan",
    "retrieve_docs": "retrieve",
    "retrieve": "retrieve",
    "ask_user": "ask",
    "ask": "ask",
    "list_files": "list",
    "list": "list",
    "read_file": "read",
    "read": "read",
    "grep": "grep",
    "search": "grep",
    "write_file": "write",
    "write": "write",
    "edit_file": "edit",
    "edit": "edit",
    "run_shell": "shell",
    "shell": "shell",
    "run_tests": "test",
    "test": "test",
    "update_tasks": "tasks",
    "tasks": "tasks",
    "finish": "finish",
    "done": "finish",
}

#: Canonical task statuses; the parser/normaliser coerces anything else to these.
TASK_STATUSES = ("pending", "in_progress", "completed")


def normalize_tasks(raw) -> list[dict]:
    """Coerce a model-supplied task list into ``[{"content": str, "status": str}, ...]``.

    Accepts list items that are dicts (``{content, status}``) or bare strings (→ pending). Unknown
    statuses fall back to ``pending``. Empty-content items are dropped. Capped to keep the UI sane.
    """
    out: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, dict):
            content = str(item.get("content") or item.get("task") or item.get("title") or "").strip()
            status = str(item.get("status") or "pending").strip().lower().replace("-", "_")
        else:
            content = str(item or "").strip()
            status = "pending"
        if not content:
            continue
        if status in ("in_progress", "doing", "active", "in progress"):
            status = "in_progress"
        elif status in ("completed", "done", "complete", "finished"):
            status = "completed"
        else:
            status = "pending"
        out.append({"content": content[:200], "status": status})
    return out[:30]

#: Text-protocol instructions appended to the system prompt for non-tool models.
#: Describes the FULL dynamic toolset. After each turn the system runs the tool(s) the
#: model emitted and replies with the results; the model then decides the next action.
#: There is intentionally no fixed order — the model chooses what is most useful now.
TEXT_PROTOCOL = (
    "You ACT using TOOLS. Each turn, take the next action(s) that move the task forward; the\n"
    "system then runs them and replies with the results, and you continue. Emit the directive(s)\n"
    "for the tool(s) you want — decide what is most useful right now, there is no fixed order:\n"
    "  • Write a file: a fenced code block whose FIRST line is a comment with the SHORT file path\n"
    "    (a path like `# blog/models.py`, never a sentence):\n"
    "    ```python\n    # blog/models.py\n    <full file contents>\n    ```\n"
    "    (You may also write a line `WRITE: <path>` immediately followed by a fenced block of the "
    "file's contents. Either way, ALWAYS put the real file contents in a fenced block.)\n"
    "  • Edit a file (targeted replace):\n"
    "    EDIT: <path>\n"
    "    <<<<<<< SEARCH\n    <exact existing text to find>\n    =======\n    <replacement text>\n    >>>>>>> REPLACE\n"
    "  • Read a file:  a line `READ: <path>`\n"
    "  • Search file contents:  a line `GREP: <pattern>`\n"
    "  • List the project files:  a line `LIST`\n"
    "  • Run a shell command:  a line `RUN: <command>`\n"
    "  • Look up Django docs:  a line `SEARCH_DOCS: <query>`\n"
    "  • Make/revise a plan:  a line `PLAN: <optional focus>`\n"
    "  • Track a task list:  a `TASKS:` line, then one task per line — `- [ ] todo`, `- [>] doing`,\n"
    "    `- [x] done`. Send the FULL list each time; mark one task `[>]` and flip tasks to `[x]`.\n"
    "  • Ask the user a clarifying question (only if truly blocked): a line `ASK: <question>`,\n"
    "    optionally followed by `- option` lines for concrete choices.\n"
    "  • Run the benchmark tests:  a line `RUN_TESTS`\n"
    "  • Finish: when the code is written and the tests pass, a line `DONE`\n"
    "Do NOT write your own test files (the benchmark tests are run for you by RUN_TESTS)."
)

#: Optional leading list bullet ("- ", "* ", "• ") — weak models routinely bullet their directives
#: ("* RUN: …", "* PLAN: …"), which the strict `^\s*` anchors otherwise miss.
_B = r"(?:[-*•]\s*)?"
_READ_RE = re.compile(rf"^\s*{_B}READ:\s*(?P<path>\S+)\s*$", re.MULTILINE)
_RUN_RE = re.compile(rf"^\s*{_B}RUN:\s*(?P<cmd>\S.*?)\s*$", re.MULTILINE)
_RUN_TESTS_RE = re.compile(rf"^\s*{_B}RUN_TESTS\s*$", re.MULTILINE)
#: DONE is the only TERMINAL directive — it must NOT be bullet-tolerant: weak models bullet a
#: progress recap ending in "* DONE" while still mid-build, which would finish the run prematurely.
_DONE_RE = re.compile(r"^\s*DONE\s*$", re.MULTILINE)
_GREP_RE = re.compile(rf"^\s*{_B}GREP:\s*(?P<pat>\S.*?)\s*$", re.MULTILINE)
_SEARCH_DOCS_RE = re.compile(rf"^\s*{_B}SEARCH_DOCS:\s*(?P<q>\S.*?)\s*$", re.MULTILINE)
_LIST_RE = re.compile(rf"^\s*{_B}LIST(?:_FILES)?\s*$", re.MULTILINE)
_PLAN_RE = re.compile(rf"^\s*{_B}PLAN(?::\s*(?P<goal>.*?))?\s*$", re.MULTILINE)
_TASKS_RE = re.compile(rf"^\s*{_B}TASKS:\s*$", re.MULTILINE)
#: One task line: `- [ ] text` / `- [>] text` / `- [x] text` (marker char optional inside the box).
_TASK_ITEM_RE = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX>\-~/]?)\]\s*(?P<text>\S.*?)\s*$")
#: A named file write a weak model emits instead of the fenced `# path` convention:
#: "WRITE: path", "* CREATE FILE: `x.html`", "SAVE: x.py". Requires an extension to avoid prose.
_NAMED_WRITE_RE = re.compile(
    rf"^[ \t]*{_B}(?:WRITE|CREATE|SAVE|NEW|ADD)\s*(?:FILE)?\s*[:=]\s*[`'\"]?"
    r"(?P<path>[^\s`'\"]+\.[A-Za-z0-9]{1,6})[`'\"]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
#: A line that begins file-like content (so a raw, unfenced body after a WRITE: can be captured).
_CODE_START_RE = re.compile(r"^\s*(?:<!|<\?|<[a-zA-Z/]|\{%|\{\{|def |class |import |from |@|#!|/\*|//|--|\{)")
#: Code-structural characters — a line containing these is treated as code, never prose.
_CODE_CHARS_RE = re.compile(r"[=(){}\[\]:;<>]")
#: A line that looks like another directive (ends a raw-content capture).
_DIRECTIVE_LINE_RE = re.compile(
    rf"^\s*{_B}(?:WRITE|CREATE|SAVE|NEW|ADD|EDIT|READ|GREP|LIST|RUN|RUN_TESTS|PLAN|ASK|TASKS|DONE|SEARCH_DOCS)\b",
    re.IGNORECASE,
)
#: A search/replace edit block. `search`/`replace` bodies may span lines; each block is
#: terminated by its own `>>>>>>> REPLACE` so finditer matches them independently.
_EDIT_RE = re.compile(
    rf"^[ \t]*{_B}EDIT:[ \t]*(?P<path>\S+)[ \t]*\r?\n"
    r"[<]{3,}[ \t]*SEARCH[ \t]*\r?\n(?P<search>.*?)\r?\n[=]{3,}[ \t]*\r?\n"
    r"(?P<replace>.*?)\r?\n[>]{3,}[ \t]*REPLACE",
    # MULTILINE so ^EDIT: matches at the start of ANY line (not just the whole string),
    # i.e. a model can emit several EDIT blocks (with prose before the first) and all match.
    re.DOTALL | re.MULTILINE,
)


@dataclass
class TextActions:
    """Parsed actions from a text-protocol model turn."""

    writes: dict = field(default_factory=dict)
    reads: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    run_tests: bool = False
    done: bool = False

    def has_actions(self) -> bool:
        return bool(self.writes or self.reads or self.runs or self.run_tests)


def parse_text_actions(text: str, expected_files: list[str] | None = None) -> TextActions:
    """Parse code-block writes + READ/RUN/RUN_TESTS/DONE directives from model text."""
    try:
        writes = CodeBlockExtractor().extract(text, expected_files=expected_files)
    except ValueError:
        writes = {}
    reads = [m.group("path").strip().strip("`'\"") for m in _READ_RE.finditer(text)]
    runs = [m.group("cmd").strip() for m in _RUN_RE.finditer(text)]
    return TextActions(
        writes=writes,
        reads=reads,
        runs=runs,
        run_tests=bool(_RUN_TESTS_RE.search(text)),
        done=bool(_DONE_RE.search(text)),
    )


def parse_actions_ordered(text: str, expected_files: list[str] | None = None, interactive: bool = True) -> list[dict]:
    """Parse a text-protocol model turn into an ORDERED list of canonical actions.

    Each action is ``{"tool": <canonical name>, "args": {...}}`` using the dynamic agent's
    vocabulary (plan/retrieve/list/read/grep/write/edit/shell/test/finish). The router queues
    these and dispatches one per step, so a turn that writes two files then runs the tests
    yields three visible steps. Order is a sensible think→inspect→change→verify default
    (the document position of each directive is not tracked).
    """
    text = text or ""
    actions: list[dict] = []

    # JSON tool-call dumps: native-tool models sometimes emit the call AS TEXT (optionally
    # ```json-fenced) instead of via the function-calling API. Convert them to canonical
    # actions exactly like the native path would, and blank their spans so the JSON body is
    # never re-parsed as directives or file content.
    json_calls, text = _extract_json_tool_calls(text)

    # Directives (READ/RUN/PLAN/DONE/…) are parsed OUTSIDE fenced code: a `RUN:`/`DONE`/`READ:`
    # line that appears INSIDE a generated file is file content, not a command to execute — else a
    # generated README/notes file could trigger a shell run or prematurely finish the run. Writes
    # still see the full text (their content IS the fenced body).
    directive_text = _strip_code_fences(text)

    # ask_user is only meaningful in interactive (chat) mode. Multiple questions emitted in ONE
    # turn are COALESCED into a single batched ask so the UI can present them together (paginated,
    # answered in one go) instead of blocking on them one-by-one.
    if interactive:
        asks = _parse_ask(directive_text)
        # JSON-dump asks join the same batch so they present as one paginated card too.
        for tool, args in json_calls:
            if tool == "ask":
                if args.get("questions"):
                    asks.extend({"question": (q.get("question") or q.get("prompt") or ""),
                                 "options": q.get("options") or []} for q in args["questions"] if q)
                else:
                    asks.append({"question": (args.get("question") or args.get("prompt") or ""),
                                 "options": args.get("options") or []})
        asks = [a for a in asks if (a.get("question") or "").strip()]
        if len(asks) == 1:
            actions.append({"tool": "ask", "args": asks[0]})
        elif asks:
            actions.append({"tool": "ask", "args": {"questions": asks[:6]}})
    # Non-ask JSON calls execute next, in document order (a JSON dump is usually the
    # turn's entire intent — e.g. update_tasks, write_file, finish).
    for tool, args in json_calls:
        if tool != "ask":
            actions.append({"tool": tool, "args": args})
    pm = _PLAN_RE.search(directive_text)
    if pm:
        actions.append({"tool": "plan", "args": {"goal": (pm.group("goal") or "").strip()}})
    tasks = _parse_tasks(directive_text)
    if tasks:
        actions.append({"tool": "tasks", "args": {"tasks": tasks}})
    for m in _SEARCH_DOCS_RE.finditer(directive_text):
        actions.append({"tool": "retrieve", "args": {"query": m.group("q").strip()}})
    if _LIST_RE.search(directive_text):
        actions.append({"tool": "list", "args": {}})

    # Writes/edits come before reads/greps: the dominant multi-action turn is
    # "write the file(s), then verify" (read it back / grep / run tests), so producing
    # code first keeps a same-turn read/test acting on what was just written.
    try:
        writes = CodeBlockExtractor().extract(text, expected_files=expected_files)
    except ValueError:
        writes = {}
    # Recover writes a weaker model expressed as a `WRITE: <path>` line + (fenced or raw) body,
    # instead of the fenced `# path` convention — otherwise its files are silently never created.
    for path, content in _parse_named_writes(text).items():
        writes.setdefault(path, content)
    for path, content in writes.items():
        actions.append({"tool": "write", "args": {"path": path, "content": content}})
    for m in _EDIT_RE.finditer(directive_text):
        actions.append({"tool": "edit", "args": {
            "path": m.group("path").strip().strip("`'\""),
            "search": m.group("search"),
            "replace": m.group("replace"),
        }})

    for m in _READ_RE.finditer(directive_text):
        actions.append({"tool": "read", "args": {"path": m.group("path").strip().strip("`'\"")}})
    for m in _GREP_RE.finditer(directive_text):
        actions.append({"tool": "grep", "args": {"pattern": m.group("pat").strip()}})
    for m in _RUN_RE.finditer(directive_text):
        actions.append({"tool": "shell", "args": {"command": m.group("cmd").strip()}})
    if _RUN_TESTS_RE.search(directive_text):
        actions.append({"tool": "test", "args": {}})
    if _DONE_RE.search(directive_text):
        actions.append({"tool": "finish", "args": {}})
    # Asking and finishing in the same turn is contradictory (weak models often append DONE to a
    # turn where they actually asked a question) — the ask must block for the answer first, so drop
    # a co-emitted finish and let the model decide again after the user responds.
    if any(a["tool"] == "ask" for a in actions):
        actions = [a for a in actions if a["tool"] != "finish"]
    return actions


def _strip_code_fences(text: str) -> str:
    """Blank out fenced ```code``` regions so directive parsing ignores file content."""
    return re.sub(r"```[\s\S]*?```", "", text or "")


#: Likely starts of a JSON tool-call object emitted as text: {"name": …} / {"tool": …} /
#: {"function": {"name": …}}. Only these positions are fed to the (cheap but real) JSON decoder.
_JSON_CALL_START_RE = re.compile(r'\{\s*"(?:name|tool|function)"\s*:')
#: A fenced block whose FIRST body line is a `# path` / `<!-- path -->` / `{# path #}` marker is a
#: FILE WRITE (CodeBlockExtractor convention) — its body is file content and must never execute.
_WRITE_FENCE_RE = re.compile(
    r"```[a-zA-Z0-9_+\-]*[ \t]*\n[ \t]*(?:#|<!--|\{#)\s*[\w./\-]+\.\w+\s*(?:-->|#\})?[ \t]*\n[\s\S]*?```"
)


def _extract_json_tool_calls(text: str) -> tuple[list[tuple[str, dict]], str]:
    """Recover tool calls a native-tool model emitted AS TEXT instead of via function calling.

    Weak models often dump the call verbatim — optionally ```json-fenced — e.g.
    ``{"name": "update_tasks", "arguments": {"tasks": [...]}}``. Without this, the text
    protocol sees no actions, so the router treats the turn as a final answer and the run
    ends ("marks DONE") without ever executing the call — and the UI shows raw JSON instead
    of the tool's card.

    Returns ``([(canonical_tool, arguments), …] in document order, text_with_those_spans_blanked)``.
    The spans are blanked so the JSON body is never re-parsed as directives or file content.
    Unrecognized JSON (no known tool name) is left untouched — it may be legitimate file
    content — and anything inside a `# path` WRITE fence is file content by definition and is
    never extracted (a generated config/notes file containing such JSON must not execute).
    """
    text = text or ""
    write_fences = [m.span() for m in _WRITE_FENCE_RE.finditer(text)]

    def _in_write_fence(i: int) -> bool:
        return any(s <= i < e for s, e in write_fences)

    calls: list[tuple[str, dict]] = []
    spans: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        m = _JSON_CALL_START_RE.search(text, pos)
        if not m:
            break
        if _in_write_fence(m.start()):
            pos = m.start() + 1
            continue
        try:
            obj, end = decoder.raw_decode(text, m.start())
        except ValueError:
            pos = m.start() + 1
            continue
        pos = end
        if not isinstance(obj, dict):
            continue
        func = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = str(func.get("name") or func.get("tool") or "").strip().lower()
        tool = CANONICAL_TOOL.get(name)
        if not tool:
            continue
        args = (func.get("arguments") or func.get("parameters") or func.get("args")
                or func.get("input") or {})
        if isinstance(args, str):  # arguments occasionally double-encoded as a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append((tool, args))
        spans.append((m.start(), end))
    if spans:
        chars = list(text)
        for start, end in spans:
            chars[start:end] = " " * (end - start)
        text = "".join(chars)
    return calls, text


def _clean_rel_path(path: str) -> str:
    """Sanitize a model-supplied relative path (strip quotes/backticks, reject absolute / ``..``)."""
    cleaned = (path or "").strip().strip("`'\"<>:,").replace("\\", "/").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        return ""
    return cleaned


def _strip_leading_path_marker(content: str, path: str) -> str:
    """Drop a leading ``# path`` / ``<!-- path -->`` marker line if the model also included one."""
    if not content:
        return content
    head, _, rest = content.partition("\n")
    if head.strip() in (f"# {path}", f"// {path}", f"<!-- {path} -->", f"{{# {path} #}}"):
        return rest
    return content


def _looks_like_prose(line: str) -> bool:
    """A col-0 English remark (e.g. a chatty sign-off), NOT code — used to end a raw-body capture
    so trailing prose isn't baked into the file. Conservative: indented lines, comments, and any
    line with code-structural chars / a trailing comma are treated as code (kept)."""
    s = line.rstrip()
    if not s or s[:1] in (" ", "\t", "#"):       # blank, indented, or a comment → code continuation
        return False
    if _CODE_START_RE.match(line) or _DIRECTIVE_LINE_RE.match(line):
        return False
    if _CODE_CHARS_RE.search(s) or s.endswith(","):
        return False                              # has code structure
    return (" " in s) and s[-1:] in ".!?"         # a sentence: has a space and ends in . ! ?


def _parse_named_writes(text: str) -> dict:
    """Recover ``WRITE: <path>`` (also CREATE/SAVE/NEW/ADD) writes a weak model emits instead of the
    fenced ``# path`` convention, taking the following FENCED block or — if the body is raw — the
    contiguous file-like lines up to the next directive. Returns ``{path: content}``.
    """
    lines = (text or "").splitlines()
    writes: dict = {}
    n = len(lines)
    i = 0
    while i < n:
        # Skip whole fenced blocks not introduced by a WRITE: line — their interior is file content,
        # so a literal "WRITE: x.py" inside a generated file must NOT spawn a spurious write.
        if lines[i].lstrip().startswith("```"):
            i += 1
            while i < n and not lines[i].lstrip().startswith("```"):
                i += 1
            i += 1
            continue
        m = _NAMED_WRITE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        path = _clean_rel_path(m.group("path"))
        j = i + 1
        while j < n and not lines[j].strip():  # skip blank lines before the body
            j += 1
        content = ""
        if j < n and lines[j].lstrip().startswith("```"):       # fenced body
            j += 1
            body: list[str] = []
            while j < n and not lines[j].lstrip().startswith("```"):
                body.append(lines[j])
                j += 1
            j += 1  # consume the closing fence
            content = "\n".join(body)
        elif j < n and _CODE_START_RE.match(lines[j]):          # raw body up to the next directive
            body = []
            while j < n and not lines[j].lstrip().startswith("```") and not _DIRECTIVE_LINE_RE.match(lines[j]):
                if not lines[j].strip():                         # blank: peek past blanks…
                    k = j + 1
                    while k < n and not lines[k].strip():
                        k += 1
                    if k < n and _looks_like_prose(lines[k]):    # …blank then prose → end of body
                        break
                elif _looks_like_prose(lines[j]):                # inline trailing prose → end of body
                    break
                body.append(lines[j])
                j += 1
            content = "\n".join(body).rstrip("\n")
        else:
            i += 1
            continue
        content = content.strip("\n")
        if path and content:
            writes.setdefault(path, _strip_leading_path_marker(content, path))
        i = max(j, i + 1)
    return writes


def _parse_tasks(text: str) -> list[dict]:
    """Parse a ``TASKS:`` block (the contiguous ``- [ ] item`` lines that follow it).

    If a turn contains more than one ``TASKS:`` block, the LAST non-empty one wins — the model
    is told to resend the full list, so a later (corrected) block supersedes an earlier one,
    matching the cross-turn "newest is authoritative" rule in ``views._latest_tasks``.
    """
    lines = (text or "").splitlines()
    result: list[dict] = []
    for i, line in enumerate(lines):
        if not _TASKS_RE.match(line):
            continue
        items: list[dict] = []
        j = i + 1
        while j < len(lines):
            m = _TASK_ITEM_RE.match(lines[j])
            if not m:
                if lines[j].strip() == "":  # allow a single blank line between items
                    j += 1
                    if j < len(lines) and _TASK_ITEM_RE.match(lines[j]):
                        continue
                break
            mark = (m.group("mark") or "").strip().lower()
            status = "completed" if mark == "x" else ("in_progress" if mark in (">", "-", "~", "/") else "pending")
            items.append({"content": m.group("text").strip(), "status": status})
            j += 1
        if items:
            result = normalize_tasks(items)  # keep scanning — the last non-empty block wins
    return result


#: A question line in the interactive ASK protocol — tolerant of how weaker models actually phrase
#: it: "ASK: <q>", "ASK - <q>", "1st ASK – <q>", "Question 2: <q>" (case-insensitive). Requires a
#: separator after ASK/QUESTION so prose like "I will ask the user…" does not false-match.
_ASK_LINE_RE = re.compile(
    r"^\s*(?:\d+(?:st|nd|rd|th)?[.)]?\s+)?(?:ASK|QUESTION)\s*\d*\s*[:\-–—]\s*(?P<q>\S.*?)\s*$",
    re.IGNORECASE,
)
#: An answer-option line — tolerant of bullets and/or letter/number labels: "- text", "* text",
#: "• text", "- A: text", "A) text", "A. text", "1. text", "A - text".
_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*(?:[A-Za-z0-9][:.)\-–—]\s+)?|[A-Za-z0-9][:.)\-–—]\s+)(?P<opt>\S.*?)\s*$"
)


def _parse_ask(text: str) -> list[dict]:
    """Parse a clarifying question + its answer options from model text.

    Tolerant of the formats weak local models really emit (ordinals, ``-``/``–`` separators,
    ``A:``/``B)`` option labels), not just the strict ``ASK:`` + ``- option`` shape — otherwise a
    model that clearly asked (with A/B/C/D choices) gets treated as prose and never becomes an
    interactive question card.
    """
    lines = (text or "").splitlines()
    asks: list[dict] = []
    i = 0
    while i < len(lines):
        m = _ASK_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        options: list[str] = []
        j = i + 1
        while j < len(lines):
            if _ASK_LINE_RE.match(lines[j]):      # next question → stop collecting options
                break
            om = _OPTION_LINE_RE.match(lines[j])
            if not om:
                break
            options.append(om.group("opt").strip()[:200])
            j += 1
        asks.append({"question": m.group("q").strip()[:400], "options": options[:6]})
        i = max(j, i + 1)
    return asks


class WorkspaceTools:
    """Execute read/write/list/run/test tools inside one workspace directory."""

    OUTPUT_CAP: ClassVar[int] = 4000

    def __init__(self, workspace: Path, task=None, shell_timeout: int = 180) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.shell_timeout = shell_timeout
        #: Files the model has written this run (path -> content).
        self.written: dict[str, str] = {}

    # ------------------------------------------------------------------ paths
    def _safe(self, relative_path: str) -> Path:
        raw_str = str(relative_path or "").strip()
        if not raw_str:
            raise ValueError("empty path")
        # Reject prose/commands mistaken for a path (weak models sometimes pass a whole
        # sentence as the 'path' arg). A real path has no newlines, is reasonably short,
        # and has short path segments.
        if "\n" in raw_str or len(raw_str) > 200:
            raise ValueError(f"not a file path (too long / multi-line): {raw_str[:60]!r}…")
        rel = Path(raw_str.replace("\\", "/"))
        if any(len(part) > 100 for part in rel.parts):
            raise ValueError(f"not a file path (segment too long): {raw_str[:60]!r}")
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe path: {relative_path}")
        raw = self.workspace / rel
        if raw.is_symlink():  # defense-in-depth: don't follow symlinks out of the workspace
            raise ValueError(f"path is a symlink: {relative_path}")
        target = raw.resolve()
        root = self.workspace.resolve()
        if target == root:
            raise ValueError(f"path must be a file, not the project root: {relative_path!r}")
        if root not in target.parents:
            raise ValueError(f"path escapes the workspace: {relative_path}")
        return target

    # ------------------------------------------------------------------ tools
    def write_file(self, path: str, content: str) -> str:
        text = content if isinstance(content, str) else str(content or "")
        try:
            target = self._safe(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except (ValueError, OSError) as exc:
            return f"ERROR: cannot write {path!r}: {exc}"
        normalized = Path(str(path).strip().replace("\\", "/")).as_posix()
        self.written[normalized] = text
        return f"Wrote {normalized} ({len(text)} bytes)."

    def read_file(self, path: str) -> str:
        try:
            target = self._safe(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {path}"
        data = target.read_text(encoding="utf-8", errors="replace")
        return self._cap(data)

    def list_files(self) -> str:
        try:
            files = []
            root = self.workspace.resolve()
            for item in sorted(self.workspace.rglob("*")):
                if item.is_dir() or any(part in _SKIP_DIRS for part in item.parts):
                    continue
                files.append(item.resolve().relative_to(root).as_posix())
        except OSError as exc:
            return f"ERROR: {exc}"
        return "\n".join(files) if files else "(workspace is empty)"

    def _shell_env(self) -> dict:
        """Env for run_shell: if the workspace has a ready per-project venv, put its bin on PATH
        so bare ``python`` / ``pip`` resolve to the project venv (stdlib-only; marker convention).

        ALWAYS scrubs the dashboard's own Django vars: the parent process exports
        DJANGO_SETTINGS_MODULE=webapp.settings, and every sandbox ``manage.py`` uses
        ``os.environ.setdefault`` — so an inherited value makes ``python manage.py migrate``
        try to import the DASHBOARD's settings from the sandbox cwd and crash confusingly."""
        env = dict(os.environ)
        for var in ("DJANGO_SETTINGS_MODULE", "PYTHONPATH", "PYTHONHOME"):
            env.pop(var, None)
        venv = self.workspace / ".venv"
        if not (venv / ".kursinis_ready").exists():
            env.pop("VIRTUAL_ENV", None)
            return env
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        if bin_dir.exists():
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env

    def run_shell(self, command: str) -> str:
        if not command or not str(command).strip():
            return "ERROR: empty command."
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.workspace), env=self._shell_env(),
                capture_output=True, text=True, timeout=self.shell_timeout, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {self.shell_timeout}s: {command}"
        except Exception as exc:  # pragma: no cover - defensive
            return f"ERROR: {exc}"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        result = f"exit={proc.returncode}\n{out}".rstrip()
        # Actionable observation for the single most common weak-model dead-end: running
        # `python manage.py ...` before any project skeleton exists. Without this hint models
        # spiral into asking the user "did you create the project correctly?" in a loop.
        if proc.returncode != 0 and "manage.py" in str(command) and not (self.workspace / "manage.py").exists():
            result += (
                "\n\nHINT: `manage.py` DOES NOT EXIST in this project yet — that is why the command "
                "failed. Do NOT ask the user about it. Create the project skeleton first: run "
                "`django-admin startproject config .` (note the trailing dot) or write manage.py + "
                "settings yourself, then retry this command."
            )
        return self._cap(result)

    def run_tests(self) -> str:
        if self.task is None:
            return "ERROR: no benchmark task is attached to this run."
        files = self.collected_files()
        if not files:
            return "ERROR: no source files written yet — write code before running tests."
        try:
            from src.eval.sandbox import DjangoSandbox

            result = DjangoSandbox().run(self.task, files)
        except Exception as exc:  # pragma: no cover - sandbox setup failure
            return f"ERROR: could not run tests: {exc}"
        verdict = "PASSED" if result.passed else "FAILED"
        body = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
        summary = f"{verdict} — passed={result.tests_passed} failed={result.tests_failed} (exit {result.returncode})"
        return self._cap(f"{summary}\n{body}".rstrip())

    def grep(self, pattern: str, path: str | None = None) -> str:
        """Search source-file contents for a regex (or plain substring) and return matches.

        Each hit is ``relpath:lineno: <line>``. ``path`` optionally scopes the search to a
        file or sub-directory. Falls back to a substring search if ``pattern`` is not valid regex.
        """
        pat = str(pattern or "").strip()
        if not pat:
            return "ERROR: empty pattern."
        try:
            rx = re.compile(pat)
        except re.error:
            rx = None  # treat as a plain substring search

        if path:
            try:
                scope = self._safe(path)
            except ValueError as exc:
                return f"ERROR: {exc}"
            if scope.is_file():
                candidates = [scope]
            elif scope.is_dir():
                candidates = [p for p in scope.rglob("*") if p.is_file()]
            else:
                return f"ERROR: path not found: {path}"
        else:
            candidates = [p for p in self.workspace.rglob("*") if p.is_file()]

        root = self.workspace.resolve()
        matches: list[str] = []
        for item in sorted(candidates):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            if item.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            try:
                text = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = item.resolve().relative_to(root).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                hit = rx.search(line) if rx is not None else (pat in line)
                if hit:
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= 100:
                        matches.append("… [more matches truncated]")
                        return self._cap("\n".join(matches))
        return self._cap("\n".join(matches)) if matches else f"(no matches for {pat!r})"

    def edit_file(self, path: str, search: str, replace: str) -> str:
        """Replace an exact snippet in an existing file (str_replace-style)."""
        try:
            target = self._safe(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not target.exists() or not target.is_file():
            return f"ERROR: file not found: {path} (use write_file to create it)."
        if not isinstance(search, str) or search == "":
            return "ERROR: edit_file needs a non-empty 'search' snippet to locate the text to replace."
        repl = replace if isinstance(replace, str) else str(replace or "")
        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: cannot read {path}: {exc}"
        occurrences = original.count(search)
        if occurrences == 0:
            return f"ERROR: the 'search' text was not found in {path}; read_file it and copy the exact snippet."
        updated = original.replace(search, repl)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: cannot write {path}: {exc}"
        normalized = Path(str(path).strip().replace("\\", "/")).as_posix()
        self.written[normalized] = updated
        return f"Edited {normalized}: replaced {occurrences} occurrence(s) ({len(original)}→{len(updated)} bytes)."

    # ------------------------------------------------------------------ collect
    def collected_files(self) -> dict[str, str]:
        """Return ``{relpath: content}`` of model-generated source files in the workspace."""
        files: dict[str, str] = {}
        root = self.workspace.resolve()
        for item in self.workspace.rglob("*"):
            if not item.is_file() or any(part in _SKIP_DIRS for part in item.parts):
                continue
            rel = item.resolve().relative_to(root).as_posix()
            if Path(rel).name in _NON_SOURCE or item.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            try:
                files[rel] = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        # Explicitly written files always count (still excluding framework-owned names).
        for path, content in self.written.items():
            if Path(path).name not in _NON_SOURCE:
                files.setdefault(path, content)
        return files

    def _cap(self, text: str) -> str:
        if len(text) <= self.OUTPUT_CAP:
            return text
        return text[: self.OUTPUT_CAP] + f"\n… [truncated, {len(text) - self.OUTPUT_CAP} more chars]"


def dispatch(tools: WorkspaceTools, name: str, arguments: dict) -> str:
    """Execute a native tool call by name; return the observation string."""
    arguments = arguments or {}
    if name == "write_file":
        return tools.write_file(arguments.get("path", ""), arguments.get("content", ""))
    if name == "read_file":
        return tools.read_file(arguments.get("path", ""))
    if name == "list_files":
        return tools.list_files()
    if name == "grep":
        return tools.grep(arguments.get("pattern", ""), arguments.get("path"))
    if name == "edit_file":
        return tools.edit_file(
            arguments.get("path", ""), arguments.get("search", ""), arguments.get("replace", "")
        )
    if name == "run_shell":
        return tools.run_shell(arguments.get("command", ""))
    if name == "run_tests":
        return tools.run_tests()
    if name == "finish":
        return "Acknowledged finish."
    return f"ERROR: unknown tool '{name}'."
