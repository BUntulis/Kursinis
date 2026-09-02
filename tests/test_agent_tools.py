"""Unit tests for the tool-using agent (WorkspaceTools + ToolCoderNode, no live model)."""
from __future__ import annotations

import pytest

from src.agent.nodes.ToolCoderNode import ToolCoderNode
from src.agent.tools import WorkspaceTools, parse_text_actions
from src.llm.LLMBackend import LLMBackend
from src.llm.LLMResponse import LLMResponse
from src.llm.OllamaBackend import OllamaBackend


# --------------------------------------------------------------------- backends
class FakeTextBackend(LLMBackend):
    """Non-tool backend that replays scripted text turns (text tool-protocol)."""

    name = "fake-text"
    supports_tools = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.model = "fake-text"

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        text = self.responses.pop(0) if self.responses else "DONE"
        return LLMResponse(text=text)


class FakeNativeBackend(LLMBackend):
    """Tool-capable backend that replays scripted tool_calls turns (native protocol)."""

    name = "fake-native"
    supports_tools = True

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.model = "llama3.2"

    def complete(self, prompt, *, system=None, temperature=0.2, max_tokens=2048):
        return LLMResponse(text="")

    def chat(self, messages, *, tools=None, temperature=0.2, max_tokens=2048):
        return self.scripted.pop(0) if self.scripted else LLMResponse(text="", tool_calls=[{"name": "finish", "arguments": {}}])


# --------------------------------------------------------------------- tests
def test_ollama_supports_tools_detection():
    backend = OllamaBackend.__new__(OllamaBackend)  # bypass __init__ (no ollama list call)
    cases = {
        "llama3.2:latest": True,
        "mistral:latest": True,
        "qwen2.5-coder:7b-instruct": True,
        "codellama:latest": False,
        "deepseek-coder:latest": False,
        "starcoder2:latest": False,
        "llama3:latest": False,
    }
    for tag, expected in cases.items():
        backend.model = tag
        assert backend.supports_tools is expected, tag


def test_resolve_model_picks_an_actually_installed_tag():
    """A different tag of the same family must resolve to the INSTALLED tag, not the 404-ing one."""
    backend = OllamaBackend.__new__(OllamaBackend)  # bypass __init__ (no ollama list call)

    # exact tag installed → keep it
    backend._installed_models = lambda: ["qwen2.5-coder:7b-instruct", "codellama:latest"]
    assert backend._resolve_model("qwen2.5-coder:7b-instruct") == "qwen2.5-coder:7b-instruct"

    # same family, different tag installed → use the installed tag (the bug: it returned the
    # requested :7b-instruct, which Ollama 404s because only :14b is pulled)
    backend._installed_models = lambda: ["qwen2.5-coder:14b", "llama3.1:8b"]
    assert backend._resolve_model("qwen2.5-coder:7b-instruct") == "qwen2.5-coder:14b"

    # no same-family tag → fall back to a preferred installed coder model
    backend._installed_models = lambda: ["codellama:latest", "mistral:latest"]
    assert backend._resolve_model("qwen2.5-coder:7b-instruct") == "codellama:latest"

    # listing failed (server down) → return the requested tag unchanged (best effort)
    def _boom():
        raise RuntimeError("no server")

    backend._installed_models = _boom
    assert backend._resolve_model("qwen2.5-coder:7b-instruct") == "qwen2.5-coder:7b-instruct"


def test_parse_ask_tolerates_real_model_formatting():
    """The exact prose-y format a weak model emitted (ordinal + 'ASK -' + 'A:'/'B:' options + DONE)
    must parse into ONE interactive ask with its options — not be treated as a finish summary."""
    from src.agent.tools import parse_actions_ordered

    text = (
        "Sure! I can help you with that by asking these clarifying questions:\n"
        "1st ASK - What should be considered an \"unclear\" decision for this task planner?\n"
        "- A: Yes – Users create tasks with statuses like not started / started / cancelled\n"
        "- B: No – Users only view a read-only board\n"
        "- C: Yes – Track each person's remaining days until due\n"
        "- D: No – Assign departments to positions only\n"
        "DONE"
    )
    actions = parse_actions_ordered(text, interactive=True)
    asks = [a for a in actions if a["tool"] == "ask"]
    assert len(asks) == 1
    assert "unclear" in asks[0]["args"]["question"].lower()
    assert len(asks[0]["args"]["options"]) == 4
    assert asks[0]["args"]["options"][0].startswith("Yes")
    # asking + DONE in one turn → the co-emitted finish is dropped (ask blocks for the answer first)
    assert not any(a["tool"] == "finish" for a in actions)


def test_named_write_directive_creates_a_file_fenced():
    """A weak model's 'WRITE: path' + fenced body (instead of the '# path' convention) must still
    produce a write action — otherwise its files are silently never created."""
    from src.agent.tools import parse_actions_ordered

    text = (
        "* PLAN: Create an HTML page to review the planner.\n"
        "* WRITE: `templates/index.html`\n\n"
        "```html\n<!DOCTYPE html>\n<html><body><h1>Task Planner</h1></body></html>\n```\n"
        "DONE"
    )
    actions = parse_actions_ordered(text, interactive=True)
    writes = [a for a in actions if a["tool"] == "write"]
    assert [w["args"]["path"] for w in writes] == ["templates/index.html"]
    assert "<!DOCTYPE html>" in writes[0]["args"]["content"]


def test_named_write_directive_creates_a_file_raw_body():
    """`WRITE: path` followed by RAW (unfenced) markup is captured up to the next directive."""
    from src.agent.tools import parse_actions_ordered

    text = (
        "WRITE: templates/home.html\n"
        "<!DOCTYPE html>\n<html><body><h1>Home</h1></body></html>\n"
        "RUN: python manage.py runserver"
    )
    actions = parse_actions_ordered(text, interactive=True)
    writes = [a for a in actions if a["tool"] == "write"]
    assert [w["args"]["path"] for w in writes] == ["templates/home.html"]
    assert writes[0]["args"]["content"].startswith("<!DOCTYPE html>")
    assert "runserver" not in writes[0]["args"]["content"]  # the next directive ends the capture


def test_bulleted_directives_are_parsed():
    """Weak models bullet their directives ('* RUN:', '* PLAN:') — those must still be recognised."""
    from src.agent.tools import parse_actions_ordered

    actions = parse_actions_ordered("* PLAN: do it\n* RUN: python manage.py migrate", interactive=True)
    assert any(a["tool"] == "plan" for a in actions)
    assert [a["args"]["command"] for a in actions if a["tool"] == "shell"] == ["python manage.py migrate"]


def test_named_write_inside_a_fence_is_not_a_spurious_file():
    """A literal 'WRITE: other.py' that is part of a generated file's CONTENT must not spawn a file."""
    from src.agent.tools import parse_actions_ordered

    text = (
        "```python\n# docs/example.py\n"
        "# Tip: to add a file, type WRITE: other.py then the body.\n"
        "print('hi')\n```"
    )
    actions = parse_actions_ordered(text, interactive=True)
    paths = sorted(a["args"]["path"] for a in actions if a["tool"] == "write")
    assert paths == ["docs/example.py"]  # NOT "other.py"


def test_prose_about_writing_does_not_create_a_file():
    """A sentence merely mentioning a file path must NOT be parsed as a write."""
    from src.agent.tools import parse_actions_ordered

    actions = parse_actions_ordered("I will write templates/index.html with a nice design soon.", interactive=True)
    assert not [a for a in actions if a["tool"] == "write"]


def test_bulleted_done_recap_does_not_finish_the_run():
    """A weak model's bulleted progress recap ending in '* DONE' must NOT finish (only a bare DONE
    line does) — otherwise a mid-build recap prematurely terminates the agent loop."""
    from src.agent.tools import parse_actions_ordered

    recap = parse_actions_ordered("Progress:\n* Created the Post model\n* DONE", interactive=False)
    assert not any(a["tool"] == "finish" for a in recap)
    # a real, bare DONE still finishes
    assert any(a["tool"] == "finish" for a in parse_actions_ordered("All set.\nDONE", interactive=False))


def test_directives_inside_a_fence_do_not_execute():
    """A RUN:/READ:/DONE line that is part of a generated file's CONTENT must not become an action."""
    from src.agent.tools import parse_actions_ordered

    text = "```markdown\n# README.md\n* RUN: rm -rf /\nDONE\n```"
    actions = parse_actions_ordered(text, interactive=False)
    assert not any(a["tool"] in ("shell", "finish") for a in actions)
    assert [a["args"]["path"] for a in actions if a["tool"] == "write"] == ["README.md"]
    # a genuine out-of-fence RUN still executes
    assert [a["args"]["command"] for a in parse_actions_ordered("RUN: python manage.py migrate") if a["tool"] == "shell"] == ["python manage.py migrate"]


def test_raw_write_body_stops_at_trailing_prose():
    """A raw (unfenced) WRITE body must not swallow a chatty sign-off into the file."""
    from src.agent.tools import _parse_named_writes

    py = _parse_named_writes(
        "WRITE: x.py\nimport os\n\ndef f():\n    return 1\n\nThis function returns one. Hope that helps!"
    )
    assert py["x.py"].endswith("return 1")
    assert "Hope that helps" not in py["x.py"]

    html = _parse_named_writes(
        "WRITE: templates/home.html\n<!DOCTYPE html>\n<html><body><h1>Home</h1></body></html>\n\nI hope this looks good!"
    )
    assert "hope" not in html["templates/home.html"].lower()


def test_raw_write_body_preserves_code_with_blank_lines():
    """Internal blank lines + non-_CODE_START_RE continuation lines (urlpatterns, return) are kept."""
    from src.agent.tools import _parse_named_writes

    out = _parse_named_writes(
        "WRITE: urls.py\nfrom django.urls import path\n\nurlpatterns = [\n    path('', views.home),\n]"
    )
    assert "urlpatterns = [" in out["urls.py"]
    assert "path('', views.home)," in out["urls.py"]


def test_parse_ask_still_handles_strict_format():
    from src.agent.tools import parse_actions_ordered

    actions = parse_actions_ordered("ASK: What fields?\n- Title only\n- Title and body", interactive=True)
    asks = [a for a in actions if a["tool"] == "ask"]
    assert len(asks) == 1
    assert asks[0]["args"]["options"] == ["Title only", "Title and body"]


def test_native_tool_calls_coalesce_multiple_asks_into_one_batch():
    """Tool-capable models (the default) emit one ask_user tool_call per question — these must be
    COALESCED into a single batched ask so the questions show as one paginated card, not one-by-one."""
    from src.agent.nodes.DynamicNodes import RouterNode
    from src.llm.LLMResponse import LLMResponse

    node = RouterNode(FakeNativeBackend([]), allowed_tools={"ask"}, interactive=True)
    resp = LLMResponse(text="", tool_calls=[
        {"name": "ask_user", "arguments": {"question": "Q1?", "options": ["A", "B"]}},
        {"name": "ask_user", "arguments": {"question": "Q2?", "options": ["X", "Y"]}},
        {"name": "ask_user", "arguments": {"question": "Q3?", "options": ["P"]}},
    ])
    actions, _ = node._decide(resp, {})
    asks = [a for a in actions if a["tool"] == "ask"]
    assert len(asks) == 1
    assert len(asks[0]["args"]["questions"]) == 3
    assert asks[0]["args"]["questions"][0]["question"] == "Q1?"


def test_native_single_ask_stays_single():
    """One native ask_user keeps the simple single-question shape (no needless batch wrapper)."""
    from src.agent.nodes.DynamicNodes import RouterNode
    from src.llm.LLMResponse import LLMResponse

    node = RouterNode(FakeNativeBackend([]), allowed_tools={"ask"}, interactive=True)
    resp = LLMResponse(text="", tool_calls=[{"name": "ask_user", "arguments": {"question": "Q?", "options": ["A"]}}])
    actions, _ = node._decide(resp, {})
    asks = [a for a in actions if a["tool"] == "ask"]
    assert len(asks) == 1
    assert asks[0]["args"].get("question") == "Q?"
    assert "questions" not in asks[0]["args"]


def test_router_require_ask_reprompts_a_model_that_finishes_first():
    """Refiner gate: a model that dumps prose + DONE without asking is re-prompted until it ASKs."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "Sure, I understand the idea. Let's proceed.\nDONE",          # premature finish, no ASK
        "ASK: Who manages tasks?\n- Department managers\n- Admins",   # forced re-prompt → a real ASK
    ])
    node = RouterNode(llm, allowed_tools={"ask"}, require_ask=True, interactive=True)
    out = node.run({"task_title": "t", "task_description": "Build a planner", "asked": False, "messages": []})
    assert not out.get("finished")
    assert (out.get("next_action") or {}).get("tool") == "ask"


def test_router_caps_questions_and_forces_the_spec():
    """Refiner ceiling: once at the question cap, a further ASK is stripped and the model is forced
    to produce the spec — preventing the weak-model 'ask trivia forever' loop."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "ASK: Who can view the task tags?\n- A: Anyone\n- B: Admins",   # over cap → stripped + re-prompted
        "## Planner\n**Overview:** A task planner.\nDONE",               # forced spec + finish
    ])
    node = RouterNode(llm, allowed_tools={"ask"}, require_ask=True, max_questions=2, interactive=True)
    out = node.run({"task_title": "t", "task_description": "Build it", "ask_count": 2, "messages": []})
    assert out.get("finished")
    assert (out.get("next_action") or {}).get("tool") != "ask"  # the over-cap question was NOT routed


def test_router_forces_a_spec_when_finishing_empty():
    """Refiner finish-spec gate: a model that asked, then calls finish with NO spec (just 'DONE')
    is re-prompted to actually write the structured prompt — else the hand-off stores nothing."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "DONE",                                                                    # empty finish
        "## Planner\n**Overview:** A task planner.\n**Features:**\n- track tasks\nDONE",  # forced spec
    ])
    node = RouterNode(llm, allowed_tools={"ask"}, require_ask=True, max_questions=6, interactive=True)
    out = node.run({"task_title": "t", "task_description": "Build it", "ask_count": 2, "messages": []})
    assert out.get("finished")
    msgs = out.get("messages") or []
    assert any("Overview" in str(m.get("content", "")) for m in msgs)  # the spec was produced


def test_router_without_require_ask_finishes_normally():
    """The gate is scoped: a normal agent (require_ask=False) still finishes on DONE in one call."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend(["All done.\nDONE"])
    node = RouterNode(llm, interactive=True)  # require_ask defaults False
    out = node.run({"task_title": "t", "task_description": "x", "messages": []})
    assert out.get("finished")


def test_workspace_tools_read_write_list_run(tmp_path):
    tools = WorkspaceTools(tmp_path)
    assert "Wrote" in tools.write_file("blog/models.py", "x = 1\n")
    assert (tmp_path / "blog" / "models.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "x = 1" in tools.read_file("blog/models.py")
    assert "blog/models.py" in tools.list_files()
    out = tools.run_shell('python -c "print(40 + 2)"')
    assert "42" in out and "exit=0" in out
    assert "blog/models.py" in tools.collected_files()


def test_workspace_tools_reject_traversal(tmp_path):
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ValueError):
        tools._safe("../escape.py")
    # read of an unsafe path is reported, not raised, to the model:
    assert "ERROR" in tools.read_file("../escape.py")


def test_workspace_tools_bad_write_returns_error_not_raises(tmp_path):
    """A bad write (empty/root/traversal path) must not raise — it crashed a live run."""
    tools = WorkspaceTools(tmp_path)
    assert tools.write_file("", "x").startswith("ERROR")        # empty path
    assert tools.write_file(".", "x").startswith("ERROR")       # resolves to the root dir
    assert tools.write_file("../e.py", "x").startswith("ERROR")  # traversal
    assert "Wrote" in tools.write_file("ok.py", "y\n")           # valid write still works


def test_parse_text_actions():
    text = "Plan.\n```python\n# blog/models.py\nclass Tag: pass\n```\nREAD: blog/models.py\nRUN: ls -a\nRUN_TESTS\nDONE"
    actions = parse_text_actions(text, expected_files=["blog/models.py"])
    assert "blog/models.py" in actions.writes
    assert actions.reads == ["blog/models.py"]
    assert actions.runs == ["ls -a"]
    assert actions.run_tests is True
    assert actions.done is True
    assert actions.has_actions() is True


def test_text_protocol_prose_heading_maps_to_expected_file():
    """A prose line containing a slash (e.g. a 'Here is the code for blog/models.py' heading)
    must NOT become a garbage file path — the block falls back to expected_files."""
    text = (
        "Here is the code for the `blog/models.py` file:\n"
        "```python\nfrom django.db import models\n\nclass Tag(models.Model):\n"
        "    name = models.CharField(max_length=50)\n```"
    )
    actions = parse_text_actions(text, expected_files=["blog/models.py"])
    assert list(actions.writes.keys()) == ["blog/models.py"]
    assert "class Tag" in actions.writes["blog/models.py"]


def test_tool_coder_text_protocol(tmp_path):
    backend = FakeTextBackend([
        "I'll create the model.\n```python\n# blog/models.py\nclass Tag:\n    pass\n```\n",
        "All set.\nDONE",
    ])
    node = ToolCoderNode(backend, max_tool_steps=5)
    state = {
        "task_id": "t", "task_title": "Blog", "task_description": "make models",
        "expected_files": ["blog/models.py"], "reference_tests": "", "workspace_path": str(tmp_path),
    }
    out = node.run(state)
    assert "blog/models.py" in out["generated_files"]
    assert out["tool_calls_made"] >= 1
    assert (tmp_path / "blog" / "models.py").exists()


def test_collected_files_excludes_framework_files(tmp_path):
    tools = WorkspaceTools(tmp_path)
    tools.write_file("blog/models.py", "x = 1\n")
    tools.write_file("prompt.txt", "the prompt")            # framework-owned name -> excluded
    tools.write_file("logs/model_response.md", "junk")      # excluded even in a subdirectory
    files = tools.collected_files()
    assert "blog/models.py" in files
    assert "prompt.txt" not in files
    assert "logs/model_response.md" not in files


def test_planner_split_assumptions():
    from src.agent.nodes.PlannerNode import PlannerNode

    plan, asm = PlannerNode._split_assumptions("Plan:\n- a\n- b\nASSUMPTIONS:\n- x\n- y")
    assert "Plan:" in plan and "ASSUMPTIONS" not in plan
    assert "x" in asm and "y" in asm
    # mid-prose 'assumptions:' must NOT trigger a split
    plan2, asm2 = PlannerNode._split_assumptions("Document your assumptions: carefully.\nMore plan.")
    assert asm2 == "" and "Document your assumptions" in plan2
    # absent marker
    plan3, asm3 = PlannerNode._split_assumptions("Just a plan.")
    assert plan3 == "Just a plan." and asm3 == ""


def test_tool_coder_native_protocol(tmp_path):
    backend = FakeNativeBackend([
        LLMResponse(text="", tool_calls=[{"name": "write_file", "arguments": {"path": "blog/models.py", "content": "class Tag:\n    pass\n"}}]),
        LLMResponse(text="", tool_calls=[{"name": "finish", "arguments": {}}]),
    ])
    node = ToolCoderNode(backend, max_tool_steps=5)
    state = {
        "task_id": "t", "task_title": "Blog", "task_description": "make models",
        "expected_files": ["blog/models.py"], "reference_tests": "", "workspace_path": str(tmp_path),
    }
    out = node.run(state)
    assert "blog/models.py" in out["generated_files"]
    assert out["tool_calls_made"] >= 2  # write_file + finish
    assert (tmp_path / "blog" / "models.py").exists()


def test_parse_actions_json_tool_call_dumps():
    """Native-tool models sometimes emit the call AS TEXT (optionally fenced) — it must become
    a real action (else the router sees nothing actionable and prematurely finishes)."""
    import json as _json

    from src.agent.tools import parse_actions_ordered

    dump = "```json\n" + _json.dumps({
        "name": "update_tasks",
        "arguments": {"tasks": [{"content": "Prototype", "status": "in_progress"},
                                 {"content": "Django", "status": "pending"}]},
    }) + "\n```"
    acts = parse_actions_ordered(dump)
    assert [a["tool"] for a in acts] == ["tasks"]
    assert len(acts[0]["args"]["tasks"]) == 2

    # bare (unfenced) dumps, several in one turn, keep document order
    two = ('{"name": "write_file", "arguments": {"path": "index.html", "content": "<h1>hi</h1>"}}\n'
           '{"name": "finish", "arguments": {"summary": "done"}}')
    acts2 = parse_actions_ordered(two)
    assert [a["tool"] for a in acts2] == ["write", "finish"]
    assert acts2[0]["args"] == {"path": "index.html", "content": "<h1>hi</h1>"}

    # double-encoded arguments (arguments is a JSON string)
    dbl = _json.dumps({"name": "update_tasks",
                       "arguments": _json.dumps({"tasks": [{"content": "x", "status": "pending"}]})})
    acts3 = parse_actions_ordered(dbl)
    assert acts3[0]["tool"] == "tasks" and acts3[0]["args"]["tasks"][0]["content"] == "x"

    # a JSON ask joins the ask path and suppresses a co-emitted DONE
    ask = ('{"name": "ask_user", "arguments": {"question": "Approve?", "options": ["Yes", "No"]}}\nDONE')
    acts4 = parse_actions_ordered(ask)
    assert [a["tool"] for a in acts4] == ["ask"]


def test_parse_actions_json_inside_write_fence_is_content_not_action():
    """A JSON tool call INSIDE a `# path` write fence is file content — it must neither execute
    nor be blanked out of the written file (a generated notes/config file must stay intact)."""
    from src.agent.tools import parse_actions_ordered

    text = '```\n# notes.json\n{"name": "run_shell", "arguments": {"command": "rm -rf /"}}\n```'
    acts = parse_actions_ordered(text)
    assert [a["tool"] for a in acts] == ["write"]
    assert '"run_shell"' in acts[0]["args"]["content"]

    html = '```html\n<!-- index.html -->\n<script>let x = {"name": "finish", "arguments": {}}</script>\n```'
    acts2 = parse_actions_ordered(html)
    assert [a["tool"] for a in acts2] == ["write"]
    assert '"finish"' in acts2[0]["args"]["content"]


def test_extract_json_tool_calls_leaves_unknown_json_alone():
    from src.agent.tools import _extract_json_tool_calls

    calls, blanked = _extract_json_tool_calls('{"name": "unknown_tool", "arguments": {}} prose')
    assert calls == []
    assert '"unknown_tool"' in blanked


def test_router_task_gate_blocks_finish_with_unfinished_tasks():
    """Task gate: a model that calls finish while its own task list is 0/N done is nudged to
    bring the list up to date first — the resent TASKS block is routed before any finish."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "All the work is complete.\nDONE",                       # premature finish, list untouched
        "TASKS:\n- [x] Create the model\n- [x] Wire the urls\nDONE",  # nudged → truthful update + finish
    ])
    node = RouterNode(llm, interactive=True)
    state = {
        "task_title": "t", "task_description": "Build it", "messages": [],
        "tasks": [
            {"content": "Create the model", "status": "in_progress"},
            {"content": "Wire the urls", "status": "pending"},
        ],
    }
    out = node.run(state)
    assert not out.get("finished")
    assert (out.get("next_action") or {}).get("tool") == "tasks"  # the update runs first
    # the queued finish survives behind it, so the turn still terminates afterwards
    assert any(a.get("tool") == "finish" for a in out.get("pending_actions") or [])


def test_router_task_gate_gives_up_on_a_stubborn_model():
    """The gate is bounded: a model that keeps answering bare DONE still finishes (no loop/brick)."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend(["DONE", "DONE", "DONE"])
    node = RouterNode(llm, interactive=True)
    out = node.run({
        "task_title": "t", "task_description": "Build it", "messages": [],
        "tasks": [{"content": "Only task", "status": "pending"}],
    })
    assert out.get("finished")


def test_router_task_gate_lets_queued_work_through():
    """Real work batched WITH a premature finish survives — only the finish is stripped."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "```python\n# blog/models.py\nclass Tag:\n    pass\n```\nDONE",
    ])
    node = RouterNode(llm, interactive=True)
    out = node.run({
        "task_title": "t", "task_description": "Build it", "messages": [],
        "tasks": [{"content": "Create the model", "status": "in_progress"}],
    })
    assert not out.get("finished")
    assert (out.get("next_action") or {}).get("tool") == "write"
    assert not any(a.get("tool") == "finish" for a in out.get("pending_actions") or [])


def test_router_task_gate_inert_when_all_tasks_done():
    """A truthful, fully-completed list does not block finish."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend(["Everything shipped.\nDONE"])
    node = RouterNode(llm, interactive=True)
    out = node.run({
        "task_title": "t", "task_description": "Build it", "messages": [],
        "tasks": [{"content": "Create the model", "status": "completed"}],
    })
    assert out.get("finished")


def test_router_finish_gate_vetoes_then_lets_the_fix_through():
    """Finish gate: a finish over an app with no reachable pages is vetoed with the gate's
    message; after the model reacts (writes the fix), the re-checked gate lets work proceed."""
    from src.agent.nodes.DynamicNodes import RouterNode

    calls = {"n": 0}

    def gate():
        calls["n"] += 1
        return "" if calls["n"] > 1 else "STOP — no page is reachable, wire urls.py first."

    llm = FakeTextBackend([
        "Everything is complete.\nDONE",                                   # vetoed finish
        "```python\n# notes/urls.py\nurlpatterns = []\n```\nDONE",         # the fix + finish
    ])
    node = RouterNode(llm, interactive=True, finish_gate=gate)
    out = node.run({"task_title": "t", "task_description": "Build it", "messages": []})
    assert not out.get("finished")
    assert (out.get("next_action") or {}).get("tool") == "write"           # the fix runs first
    assert any(a.get("tool") == "finish" for a in out.get("pending_actions") or [])
    assert calls["n"] == 2                                                  # re-checked after the nudge


def test_router_finish_gate_gives_up_on_a_stubborn_model():
    """The gate is bounded by the shared attempts counter — endless bare DONEs still terminate."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend(["DONE", "DONE", "DONE", "DONE"])
    node = RouterNode(llm, interactive=True, finish_gate=lambda: "STOP — not reachable.")
    out = node.run({"task_title": "t", "task_description": "Build it", "messages": []})
    assert out.get("finished")


def test_router_task_gate_sees_tasks_seeded_from_an_earlier_turn():
    """Cross-turn continuity: tasks seeded into the state (from the chat's previous turns) arm
    the task gate even though THIS turn never called update_tasks."""
    from src.agent.nodes.DynamicNodes import RouterNode

    llm = FakeTextBackend([
        "All done.\nDONE",
        "TASKS:\n- [x] Create the model\n- [x] Wire the urls\nDONE",
    ])
    node = RouterNode(llm, interactive=True)
    out = node.run({
        "task_title": "t", "task_description": "Continue the build", "messages": [],
        "tasks": [{"content": "Create the model", "status": "completed"},
                  {"content": "Wire the urls", "status": "pending"}],
    })
    assert not out.get("finished")
    assert (out.get("next_action") or {}).get("tool") == "tasks"


def test_run_shell_hints_when_manage_py_is_missing(tmp_path):
    """A failed `python manage.py ...` with no manage.py present gets an actionable hint —
    the observation tells the model to create the skeleton, not to interrogate the user."""
    tools = WorkspaceTools(tmp_path)
    out = tools.run_shell("python manage.py makemigrations")
    assert "manage.py" in out
    assert "django-admin startproject" in out
    assert "Do NOT ask the user" in out
