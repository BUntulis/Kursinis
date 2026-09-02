"""Tool-using code generation node (agentic read / write / execute loop)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.benchmark import BenchmarkTask
from src.llm import LLMBackend

from ..AgentState import AgentState
from ..TelemetryAccumulator import TelemetryAccumulator
from ..tools import TEXT_PROTOCOL, TOOL_SCHEMAS, WorkspaceTools, dispatch, parse_text_actions
from .AgentNode import AgentNode


class ToolCoderNode(AgentNode):
    """
    **Purpose:** Generates Django files as an autonomous tool-using agent.

    **Objective:** Let the model read, write, and execute (shell + tests) inside its run
    workspace over a bounded number of tool steps, verifying its own work before finishing.

    Works with every model via a hybrid mechanism: native function-calling for tool-capable
    backends, and a universal text protocol (code blocks + READ/RUN/RUN_TESTS/DONE) otherwise.
    """

    #: Best-practice system prompt shared by both mechanisms.
    SYSTEM = (
        "You are an expert Django developer in an ALREADY-INITIALISED Django project, working as an "
        "autonomous agent with TOOLS (write/read files, run shell, run tests).\n\n"
        "Critical rules:\n"
        "- The project is already set up. Do NOT run `startapp`, create directories, or scaffold a "
        "project. Just write the app's source files directly with the write tool.\n"
        "- ACT immediately: your first action must WRITE the requested file(s). Never reply with only "
        "a prose plan or step list.\n"
        "- File paths are SHORT relative paths like `blog/models.py` — never a sentence, description, "
        "or shell command. Put the FULL file contents (real newlines).\n"
        "- Import everything you use (e.g. `from django.core.exceptions import ValidationError`, "
        "`from django.conf import settings`). Use Django 5.x.\n"
        "- Do NOT write your own test files — the benchmark's tests are run for you by run_tests.\n"
        "- After writing, run_tests to VERIFY. Read each failure and FIX the code. Repeat until the "
        "tests pass, then finish. State any assumptions briefly, then proceed."
    )

    #: First user message template.
    USER_TEMPLATE = (
        "Task: {title}\n\n"
        "{description}\n\n"
        "Plan:\n{plan}\n\n"
        "{assumptions_section}"
        "{rag_section}"
        "{feedback_section}"
        "Write EXACTLY these files now (full contents): {expected_files}\n\n"
        "The project is already set up; any files from previous attempts are present (use list_files / "
        "read_file to inspect them). Write the file(s), run_tests, fix any failures, then finish. "
        "Begin by writing the file(s) — do not describe your plan first."
    )

    TEMPERATURE = 0.2
    MAX_TOKENS = 3000
    DEFAULT_MAX_TOOL_STEPS = 12
    DEFAULT_SHELL_TIMEOUT = 180

    def __init__(
        self,
        llm: LLMBackend,
        *,
        system_prompt: str | None = None,
        user_template: str | None = None,
        max_tool_steps: int | None = None,
        shell_timeout: int | None = None,
    ) -> None:
        self.llm = llm
        self.system_prompt = system_prompt or self.SYSTEM
        self.user_template = user_template or self.USER_TEMPLATE
        self.max_tool_steps = max_tool_steps or self.DEFAULT_MAX_TOOL_STEPS
        self.shell_timeout = shell_timeout or self.DEFAULT_SHELL_TIMEOUT

    # ------------------------------------------------------------------ run
    def run(self, state: AgentState) -> dict:
        workspace = Path(state.get("workspace_path") or tempfile.mkdtemp(prefix="agent_ws_"))
        task = BenchmarkTask(
            id=state.get("task_id", "task"),
            title=state.get("task_title", ""),
            difficulty="",
            category="",
            description=state.get("task_description", ""),
            expected_files=state.get("expected_files", []),
            reference_tests=state.get("reference_tests", ""),
        )
        tools = WorkspaceTools(workspace, task=task, shell_timeout=self.shell_timeout)
        expected = state.get("expected_files", [])

        system = self._system_prompt()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._user_prompt(state)},
        ]

        telemetry = {k: state.get(k, 0) for k in (
            "total_prompt_tokens", "total_completion_tokens", "total_latency_sec", "llm_calls",
        )}
        log: list[str] = []
        tool_calls_made = 0
        finished = False
        resp = None

        for _step in range(max(1, self.max_tool_steps)):
            resp = self.llm.chat(
                messages,
                tools=TOOL_SCHEMAS if getattr(self.llm, "supports_tools", False) else None,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
            )
            telemetry = TelemetryAccumulator.increment(telemetry, resp)

            if getattr(self.llm, "supports_tools", False) and resp.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": resp.text or "",
                    "tool_calls": [
                        {"function": {"name": c.get("name"), "arguments": c.get("arguments", {})}}
                        for c in resp.tool_calls
                    ],
                })
                for call in resp.tool_calls:
                    name = call.get("name") or ""
                    try:
                        observation = dispatch(tools, name, call.get("arguments") or {})
                    except Exception as exc:  # pragma: no cover - defensive
                        observation = f"ERROR running {name}: {exc}"
                    tool_calls_made += 1
                    log.append(self._log_line(name, call.get("arguments") or {}, observation))
                    messages.append({"role": "tool", "content": observation})
                    if name == "finish":
                        finished = True
                if finished:
                    break
                continue

            # ---- text protocol ----
            actions = parse_text_actions(resp.text, expected_files=expected)
            messages.append({"role": "assistant", "content": resp.text or ""})
            if not actions.has_actions():
                # No tool directives: treat as final answer (its code blocks, if any, were written).
                break

            observations: list[str] = []
            for path, content in actions.writes.items():
                obs = tools.write_file(path, content)
                tool_calls_made += 1
                log.append(self._log_line("write_file", {"path": path}, obs))
                observations.append(obs)
            for path in actions.reads:
                obs = tools.read_file(path)
                tool_calls_made += 1
                log.append(self._log_line("read_file", {"path": path}, obs))
                observations.append(f"READ {path}:\n{obs}")
            for command in actions.runs:
                obs = tools.run_shell(command)
                tool_calls_made += 1
                log.append(self._log_line("run_shell", {"command": command}, obs))
                observations.append(f"RUN {command}:\n{obs}")
            if actions.run_tests:
                obs = tools.run_tests()
                tool_calls_made += 1
                log.append(self._log_line("run_tests", {}, obs))
                observations.append(f"RUN_TESTS:\n{obs}")

            messages.append({"role": "user", "content": "TOOL RESULTS:\n\n" + "\n\n".join(observations)})
            if actions.done:
                finished = True
                break

        files = tools.collected_files()
        updates = {
            **state,
            "generated_files": files,
            "workspace_path": str(workspace),
            "tool_calls_made": tool_calls_made,
            "tool_log": "\n".join(log[-40:]),
            **telemetry,
        }
        if not files:
            last_text = (resp.text if resp else "") or ""
            updates["coder_error"] = (
                "The tool-using coder produced no source files. "
                f"Last model output:\n{last_text[:500]}"
            )
        return updates

    # ------------------------------------------------------------------ helpers
    def _system_prompt(self) -> str:
        if getattr(self.llm, "supports_tools", False):
            extra = (
                "\n\nUse the provided tools: write_file, read_file, list_files, run_shell, run_tests, finish. "
                "Call run_tests to verify before calling finish."
            )
        else:
            extra = "\n\n" + TEXT_PROTOCOL
        return self.system_prompt + extra

    def _user_prompt(self, state: AgentState) -> str:
        assumptions = (state.get("assumptions") or "").strip()
        rag = (state.get("retrieved_context") or "").strip()
        feedback = (state.get("critic_feedback") or "").strip()
        return self.user_template.format(
            title=state.get("task_title", ""),
            description=state.get("task_description", ""),
            plan=state.get("plan", "(none)"),
            assumptions_section=f"Assumptions to follow:\n{assumptions}\n\n" if assumptions else "",
            rag_section=f"Relevant Django documentation:\n{rag}\n\n" if rag else "",
            feedback_section=f"IMPORTANT — the previous attempt failed. Fix these issues:\n{feedback}\n\n" if feedback else "",
            expected_files=", ".join(state.get("expected_files", [])) or "(infer from the task)",
        )

    @staticmethod
    def _log_line(name: str, args: dict, observation: str) -> str:
        detail = args.get("path") or args.get("command") or ""
        head = (observation or "").splitlines()[0] if observation else ""
        return f"{name}({detail}) -> {head[:120]}"


def tool_coder_node(state: AgentState, llm: LLMBackend) -> dict:
    return ToolCoderNode(llm).run(state)
