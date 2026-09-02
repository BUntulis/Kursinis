"""Run the DYNAMIC, model-routed LangGraph agent for one dashboard ModelRun and
stream every tool step into the chat-style UI.

Instead of a fixed Planner→Retriever→Coder→Executor→Critic pipeline, the agent now
decides its own next action each step (plan / retrieve / read / grep / list / write /
edit / shell / test / finish). Each tool step the model chooses is its own LangGraph
node, so ``agent.stream(stream_mode="updates")`` yields one chunk per action — recorded
as a ``CommandLog`` (the chat feed's data source, tagged with ``agent`` = the tool name
so the timeline renders a live Read/Grep/Write/Test tool-row) and as a ``JobLog`` (the
job-level stream). ``write``/``edit`` steps embed the file as a fenced ``# path`` block so
the workbench streams it straight into the Files panel; generated files are also written to
the permanent model workspace AND to ``workspaces/run_<model_run_id>/`` via ``WorkspaceWriter``,
indexed, and stored (with content + iteration) in the DB.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from django.utils import timezone

from dashboard.models import CommandLog, GeneratedFile, ModelRun, RunStatus, TestResult
from dashboard.models.JobLog import JobLog


class TurnCancelled(Exception):
    """Raised inside the agent stream when the user stops a running chat turn."""

from .file_index import index_generated_files
from .generated_code import expected_files_for_benchmark
from .workspace_writer import WorkspaceWriter
from .workspaces import safe_workspace_path


OLLAMA_RUN_RE = re.compile(r'^\s*(?:"?ollama(?:\.exe)?"?)\s+run\s+(?P<model>[^\s]+)', re.IGNORECASE)

#: Maps a dynamic tool node name to the CommandLog kind used for its chat row.
#: ``write``/``edit`` are MODEL so the workbench mines their fenced ``# path`` block and
#: streams the file into the Files panel; ``test`` is TEST so it groups with the test
#: bubbles; everything else is SYSTEM (inspection / thinking). ``finish`` is not a node.
NODE_KIND = {
    "router": CommandLog.Kind.SYSTEM,
    "tasks": CommandLog.Kind.SYSTEM,
    "plan": CommandLog.Kind.SYSTEM,
    "retrieve": CommandLog.Kind.SYSTEM,
    "list": CommandLog.Kind.SYSTEM,
    "read": CommandLog.Kind.SYSTEM,
    "grep": CommandLog.Kind.SYSTEM,
    "shell": CommandLog.Kind.SYSTEM,
    "write": CommandLog.Kind.MODEL,
    "edit": CommandLog.Kind.MODEL,
    "test": CommandLog.Kind.TEST,
}

#: Maximum characters of any single node's text we copy into a chat bubble.
TEXT_BUDGET = 2000


def extract_ollama_model(command: str) -> str | None:
    """Return the model tag from an ``ollama run <tag>`` command, if present."""
    match = OLLAMA_RUN_RE.search(command or "")
    if not match:
        return None
    return match.group("model").strip().strip("'\"")


def load_prompt_overrides(benchmark) -> dict:
    """
    Build the ``prompt_overrides`` dict passed to ``build_agent`` from the
    benchmark's template settings.

    ``PromptTemplate`` in this project stores the *task* prompt, not per-node
    agent prompts, so node-level overrides (if any) live in
    ``BenchmarkTemplate.settings['prompt_overrides']`` keyed by
    ``planner_system`` / ``planner_user`` / ``coder_system`` / ``coder_user`` /
    ``critic_system`` / ``critic_user``. Returns ``{}`` when none are configured,
    in which case the nodes keep their built-in English prompts.
    """
    settings = _template_settings(benchmark)
    raw = settings.get("prompt_overrides") or {}
    allowed = {
        # dynamic router persona (preferred); coder_system is still honoured as a fallback
        "router_system", "coder_system",
        # legacy keys (accepted but no longer wired into the dynamic graph)
        "planner_system", "planner_user", "coder_user",
        "critic_system", "critic_user",
    }
    return {key: value for key, value in raw.items() if key in allowed and value}


def resolve_task_spec(benchmark) -> tuple[list[str], str, str, str]:
    """
    Resolve ``(expected_files, reference_tests, description, title)`` for the agent.

    Pulls ``expected_files`` and ``reference_tests`` from the linked YAML
    benchmark task when one is resolvable (via the template slug), otherwise
    falls back to the BenchmarkRun's own ``reference_tests`` / active test suites.
    """
    expected_files = list(expected_files_for_benchmark(benchmark))
    reference_tests = (benchmark.reference_tests or "").strip()
    description = benchmark.prompt or ""

    task = _load_yaml_task(benchmark)
    if task is not None:
        if not expected_files:
            expected_files = list(task.expected_files)
        if not reference_tests:
            reference_tests = task.reference_tests or ""

    if not reference_tests:
        suites = list(benchmark.test_suites.filter(is_active=True).order_by("sort_order", "name"))
        reference_tests = "\n\n".join(s.test_content for s in suites if (s.test_content or "").strip())

    title = benchmark.display_title()
    return expected_files, reference_tests, description, title


def _load_yaml_task(benchmark):
    from src.benchmark import BenchmarkLoader
    from .generated_code import _task_id_candidates

    loader = BenchmarkLoader()
    for candidate in _task_id_candidates(benchmark):
        try:
            return loader.load(candidate)
        except FileNotFoundError:
            continue
    return None


def _template_settings(benchmark) -> dict:
    """Merged per-run settings (benchmark_template.settings <- BenchmarkRun.settings)."""
    from .autonomous import run_settings

    return run_settings(benchmark)


class AgentModelRunner:
    """Drive the compiled agent graph for a single ModelRun, streaming each node."""

    def __init__(self, orchestrator) -> None:
        #: The owning BenchmarkOrchestrator; reused for JobLog + ranking helpers.
        self.orchestrator = orchestrator

    def run(self, benchmark, model_run, workspace: Path, ollama_model: str, config: dict | None = None) -> dict:
        """Execute the full agent loop and persist its results into the ORM."""
        from src.agent.graph import build_agent
        from src.agent.state import TelemetryAccumulator
        from src.llm.OllamaBackend import OllamaBackend
        from .autonomous import autonomous_config
        from .ollama_models import ensure_model

        if config is None:
            config = autonomous_config(benchmark)

        expected_files, reference_tests, description, title = resolve_task_spec(benchmark)
        overrides = load_prompt_overrides(benchmark)
        use_rag = bool(config["use_rag"])
        max_iter = int(config["max_iterations"])
        sandbox_timeout = config["sandbox_timeout_sec"] if config["autonomous"] else None
        retries = config["generation_retries"] if config["autonomous"] else 0
        enable_tools = bool(config.get("enable_tools", True))
        max_tool_steps = config.get("max_tool_steps")
        shell_timeout = config.get("shell_timeout")

        self._job_log(f"Agent starting for {model_run.model_name} (ollama tag: {ollama_model})")

        # Autonomous auto-install: pull a missing model, fall back only if the pull fails.
        resolved_tag = ollama_model
        if config["autonomous"] and config["auto_install"]:
            resolved_tag, status = ensure_model(
                ollama_model, log_cb=self._job_log, auto_install=True, timeout=config["timeout_cap_sec"]
            )
            if status != "present":
                self._command(
                    model_run, CommandLog.Kind.SYSTEM, "system",
                    command=f"ensure model {ollama_model}",
                    stdout=f"Model '{ollama_model}': {status} → using '{resolved_tag}'.",
                )

        backend = OllamaBackend(model=resolved_tag)
        if backend.model != resolved_tag:
            self._command(
                model_run, CommandLog.Kind.SYSTEM, "system",
                command=f"resolve model {resolved_tag}",
                stdout=f"Requested model '{resolved_tag}' is not installed; using fallback '{backend.model}'.",
            )

        agent = build_agent(
            llm=backend, use_rag=use_rag, prompt_overrides=overrides, sandbox_timeout=sandbox_timeout,
            enable_tools=enable_tools, max_tool_steps=max_tool_steps, shell_timeout=shell_timeout,
        )
        if enable_tools:
            self._job_log(f"Tools enabled for {model_run.model_name} "
                          f"({'native' if backend.supports_tools else 'text-protocol'}): read/write/execute")

        base_state = {
            "task_id": _task_id(benchmark),
            "task_title": title,
            "task_description": description,
            "expected_files": expected_files,
            "reference_tests": reference_tests,
            "workspace_path": str(workspace),
            "iteration": 0,
            "max_iterations": max_iter,
            **TelemetryAccumulator.initial(),
        }

        attempt = 0
        while True:
            attempt += 1
            start = time.perf_counter()
            final_state, exc = self._stream(agent, dict(base_state), model_run)
            if exc is None:
                self._finalize(benchmark, model_run, final_state, time.perf_counter() - start)
                return final_state
            if attempt > retries:
                self._command(
                    model_run, CommandLog.Kind.SYSTEM, "system",
                    command="agent error", stdout=str(exc), exit_code=-1,
                )
                self._job_log(f"Agent crashed for {model_run.model_name} after {attempt} attempt(s): {exc}")
                index_generated_files(model_run)
                model_run.mark_finished(RunStatus.FAILED, -5, str(exc)[:500])
                return final_state
            self._job_log(f"[recovery] agent attempt {attempt} failed ({exc}); retrying ({attempt}/{retries})…")
            time.sleep(min(2 ** attempt, 10))

    def _stream(self, agent, initial_state: dict, model_run) -> tuple[dict, Exception | None]:
        """Run one full agent stream; return (final_state, exception_or_None)."""
        from src.agent.AgentGraphBuilder import AgentGraphBuilder

        final_state: dict = dict(initial_state)
        last = time.perf_counter()
        # Chat turns can be stopped mid-run: the stop endpoint flips the ModelRun to CANCELLED,
        # and we cooperatively abort between agent steps (benchmark runs have no chat_id → unaffected).
        is_chat = bool(getattr(model_run, "chat_id", None))
        try:
            for chunk in agent.stream(
                initial_state,
                config={"recursion_limit": AgentGraphBuilder.RECURSION_LIMIT},
                stream_mode="updates",
            ):
                if is_chat and ModelRun.objects.filter(id=model_run.id, status=RunStatus.CANCELLED).exists():
                    raise TurnCancelled()
                for node_name, node_output in chunk.items():
                    if not isinstance(node_output, dict):
                        continue
                    final_state.update(node_output)
                    now = time.perf_counter()
                    duration = now - last
                    last = now
                    self._handle_node(node_name, node_output, model_run, duration)
        except Exception as exc:  # pragma: no cover - surfaces agent crashes to the UI
            from src.llm.LLMBackend import LLMCancelled

            if isinstance(exc, LLMCancelled):
                # Stop pressed mid-generation: the backend aborted the LLM call — same clean
                # "Stopped by user" path as the between-steps check above.
                return final_state, TurnCancelled()
            return final_state, exc
        return final_state, None

    # ------------------------------------------------------------------ nodes
    def _handle_node(self, node_name: str, output: dict, model_run, duration: float) -> None:
        handler = {
            "router": self._on_router,
            "tasks": self._on_tasks,
            "plan": self._on_plan,
            "retrieve": self._on_retrieve,
            "list": self._on_inspect,
            "read": self._on_inspect,
            "grep": self._on_inspect,
            "shell": self._on_inspect,
            "write": self._on_write,
            "edit": self._on_write,
            "test": self._on_test,
        }.get(node_name)
        if handler is None:
            return
        handler(node_name, output, model_run, duration)

    def _feed_tps(self, output) -> None:
        """Feed live tokens/sec to the resource monitor so the chart has a series."""
        completion = output.get("total_completion_tokens") or 0
        latency = output.get("total_latency_sec") or 0
        tps = (completion / latency) if latency else None
        monitor = getattr(self.orchestrator, "_active_monitor", None)
        if tps and monitor is not None:
            monitor.record_tokens_per_second(tps)

    def _on_router(self, node_name, output, model_run, duration) -> None:
        # Only the router visits that actually called the LLM are a "thinking" step;
        # the ones that merely dequeue a pending action emit nothing (keeps the feed clean).
        if not output.get("did_think"):
            return
        self._feed_tps(output)
        self._command(
            model_run, CommandLog.Kind.SYSTEM, "router",
            command="", stdout="Deciding the next action…", duration=duration,
        )

    def _on_tasks(self, node_name, output, model_run, duration) -> None:
        """Emit the model's task list as a JSON payload so the chat checklist card + the workbench
        Tasks tab render the current todo/doing/done state (the API surfaces the latest one)."""
        import json

        tasks = output.get("tasks") or []
        done = sum(1 for t in tasks if t.get("status") == "completed")
        self._command(
            model_run, CommandLog.Kind.SYSTEM, "tasks",
            command=f"{done}/{len(tasks)} done", stdout=json.dumps(tasks), duration=duration,
        )
        self._job_log(f"[tasks] {done}/{len(tasks)} done")

    def _on_plan(self, node_name, output, model_run, duration) -> None:
        plan = (output.get("plan") or "").strip()
        assumptions = (output.get("assumptions") or "").strip()
        body = plan if not assumptions else f"{plan}\n\nASSUMPTIONS:\n{assumptions}"
        self._feed_tps(output)
        self._command(model_run, CommandLog.Kind.SYSTEM, "plan", command="", stdout=body[:TEXT_BUDGET], duration=duration)
        self._job_log(f"[plan] produced a plan ({len(plan)} chars){' + assumptions' if assumptions else ''}")

    def _on_retrieve(self, node_name, output, model_run, duration) -> None:
        ctx = output.get("retrieved_context") or ""
        target = (output.get("last_action") or {}).get("target", "")
        if ctx:
            summary = f"Retrieved {len(ctx)} chars of Django docs" + (f" for “{target}”." if target else ".")
        else:
            summary = output.get("last_observation") or "No documentation retrieved."
        self._command(
            model_run, CommandLog.Kind.SYSTEM, "retrieve",
            command=target, stdout=summary[:TEXT_BUDGET], duration=duration,
        )
        self._job_log(f"[retrieve] {target}".strip())

    def _on_inspect(self, node_name, output, model_run, duration) -> None:
        """read / grep / list / shell — show the tool call + its output as a code block."""
        target = (output.get("last_action") or {}).get("target", "")
        obs = output.get("last_observation") or ""
        is_err = obs.startswith("ERROR")
        lang = "python" if node_name == "read" and str(target).endswith(".py") else ""
        body = f"```{lang}\n{obs[:TEXT_BUDGET]}\n```" if obs else ""
        command = "" if node_name == "list" else target
        self._command(
            model_run, NODE_KIND.get(node_name, CommandLog.Kind.SYSTEM), node_name,
            command=command, stdout=body, exit_code=1 if is_err else 0, duration=duration,
        )
        self._job_log(f"[{node_name}] {target}".strip())

    def _on_write(self, node_name, output, model_run, duration) -> None:
        """write / edit — embed the file as a fenced ``# path`` block so it streams live."""
        action = output.get("last_action") or {}
        path = action.get("target", "")
        files = output.get("generated_files") or {}
        # The tool node carries the exact written content (keyed identically to `path`), so we
        # don't re-look-it-up by a filesystem-canonicalised key (which can differ in casing).
        content = action.get("content")
        if content is None:
            content = files.get(path, "")
        obs = output.get("last_observation") or ""
        is_err = obs.startswith("ERROR") or not content
        if is_err:
            body = obs
        else:
            lang = _lang_hint(path)
            body = f"```{lang}\n# {path}\n{content}\n```"
        self._feed_tps(output)
        self._command(
            model_run, CommandLog.Kind.MODEL, node_name,
            command=f"{node_name} {path}", stdout=body,
            exit_code=1 if is_err else 0, duration=duration,
        )
        if files and not is_err:
            self._persist_files(model_run, files, int(output.get("iteration", 1) or 1))
            self._job_log(f"[{node_name}] {path} ({len(content)} bytes)")
        else:
            self._job_log(f"[{node_name}] {path} failed: {obs[:120]}".strip())

    def _on_test(self, node_name, output, model_run, duration) -> None:
        passed = int(output.get("tests_passed", 0) or 0)
        failed = int(output.get("tests_failed", 0) or 0)
        sandbox_passed = bool(output.get("sandbox_passed"))
        stdout = output.get("sandbox_stdout")
        stderr = output.get("sandbox_stderr") or ""
        ran = "sandbox_passed" in output  # False only on the "no files written yet" error path
        body = stdout if stdout is not None else (output.get("last_observation") or "")
        self._command(
            model_run, CommandLog.Kind.TEST, "test",
            command="run tests",
            stdout=(body or "")[-TEXT_BUDGET:], stderr=stderr[-TEXT_BUDGET:],
            exit_code=0 if sandbox_passed else 1, duration=duration,
        )
        if ran:
            # Refresh TestResult each run so the goal-pill updates live.
            self._save_test_result(model_run, passed, failed, sandbox_passed, stdout or "", stderr, duration)
        self._job_log(f"[test] passed={passed} failed={failed} ({'OK' if sandbox_passed else 'FAIL'})")

    # ------------------------------------------------------------------ persist
    def _persist_files(self, model_run, files: dict[str, str], iteration: int) -> None:
        """Write generated files to the model workspace + permanent workspace, index, store content."""
        if not files:
            return
        workspace = Path(model_run.workspace_path)
        for relative_path, content in files.items():
            try:
                target = safe_workspace_path(workspace, relative_path)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # Permanent, model_run-keyed copy (JOB 2C).
        try:
            WorkspaceWriter().write(model_run.id, files)
        except Exception:
            pass
        # Index disk -> GeneratedFile rows + metrics, then store text + iteration.
        index_generated_files(model_run)
        for relative_path, content in files.items():
            normalized = str(relative_path).replace("\\", "/")
            GeneratedFile.objects.filter(model_run=model_run, relative_path=normalized).update(
                content=content, iteration=iteration
            )

    def _save_test_result(self, model_run, passed, failed, sandbox_passed, stdout, stderr, duration) -> None:
        total = passed + failed
        TestResult.objects.update_or_create(
            model_run=model_run,
            defaults={
                "command": ["pytest", "-v", "--tb=short"],
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": 0 if sandbox_passed else 1,
                "total": total,
                "passed": passed,
                "failed": failed,
                "skipped": 0,
                "duration_seconds": duration,
                "failure_details": [],
            },
        )

    def _finalize(self, benchmark, model_run, final_state: dict, wall: float) -> None:
        passed = int(final_state.get("tests_passed", 0) or 0)
        failed = int(final_state.get("tests_failed", 0) or 0)
        sandbox_passed = bool(final_state.get("sandbox_passed"))
        index_generated_files(model_run)

        latency = final_state.get("total_latency_sec", 0.0) or 0.0
        completion_tokens = int(final_state.get("total_completion_tokens", 0) or 0)
        model_run.generation_duration_seconds = latency
        model_run.testing_duration_seconds = max(wall - latency, 0.0)
        parsed = {"total": passed + failed, "passed": passed, "failed": failed}
        model_run.error_count = failed
        model_run.prompt_tokens = int(final_state.get("total_prompt_tokens", 0) or 0)
        model_run.completion_tokens = completion_tokens
        model_run.tokens_per_second = (completion_tokens / latency) if latency else None
        model_run.ranking_score = self.orchestrator._ranking_score(
            {"total": parsed["total"], "passed": passed, "failed": failed}, model_run
        )
        model_run.save(update_fields=[
            "generation_duration_seconds", "testing_duration_seconds",
            "error_count", "ranking_score", "prompt_tokens", "completion_tokens",
            "tokens_per_second", "updated_at",
        ])

        status = RunStatus.SUCCEEDED if sandbox_passed else RunStatus.FAILED
        summary = "" if sandbox_passed else _short(
            final_state.get("sandbox_stderr") or final_state.get("sandbox_stdout")
            or final_state.get("coder_error") or final_state.get("stop_reason") or ""
        )
        model_run.mark_finished(status, 0 if sandbox_passed else 1, summary)
        self._job_log(f"Finished model: {model_run.model_name} ({status}) tests={passed}/{passed + failed}")

    # ------------------------------------------------------------------ logging
    def _command(self, model_run, kind, agent, command="", stdout="", stderr="", exit_code=None, duration=None) -> CommandLog:
        now = timezone.now()
        return CommandLog.objects.create(
            model_run=model_run,
            kind=kind,
            agent=agent,
            command=[command] if command else [],
            cwd=model_run.workspace_path,
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=exit_code,
            duration_seconds=duration,
            started_at=now,
            finished_at=now,
        )

    def _job_log(self, message: str) -> None:
        self.orchestrator._job_log(message)


def _task_id(benchmark) -> str:
    from .generated_code import _task_id_candidates

    candidates = _task_id_candidates(benchmark)
    return candidates[0] if candidates else f"benchmark_{benchmark.id}"


def _short(text: str, limit: int = 500) -> str:
    from .commands import compact_command_output

    return compact_command_output(text, limit=limit)


def _lang_hint(path: str) -> str:
    """Best-effort highlight.js language token for a fenced code block, from the file extension."""
    ext = (path.rsplit(".", 1)[-1] if "." in path else "").lower()
    return {
        "py": "python", "html": "html", "htm": "html", "css": "css",
        "js": "javascript", "json": "json", "md": "markdown", "yaml": "yaml", "yml": "yaml",
    }.get(ext, "")
