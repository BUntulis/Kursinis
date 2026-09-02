"""Command rendering and execution helpers."""
from __future__ import annotations

import re
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SPINNER_CHARS_RE = re.compile(r"[\u2800-\u28ff]+")
PROGRESS_NOISE_RE = re.compile(r"^[\s\u2800-\u28ff|/\\\-_.]+$")
OLLAMA_RUN_RE = re.compile(r'^\s*(?:"?ollama(?:\.exe)?"?)\s+run\s+(?P<model>[^\s]+)', re.IGNORECASE)


@dataclass(frozen=True)
class CommandResult:
    """Captured command execution result."""

    command: str
    cwd: str
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    cpu_percent: float | None = None
    ram_mb: float | None = None
    peak_ram_mb: float | None = None


OutputCallback = Callable[[str, str], None]


def render_command(command: str, context: dict[str, object]) -> str:
    """Render supported benchmark placeholders in an admin-defined command."""
    rendered = command
    for key, value in context.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def run_command(
    command: str,
    cwd: Path,
    stdin_text: str = "",
    timeout_seconds: int = 300,
    output_callback: OutputCallback | None = None,
) -> CommandResult:
    """Run a shell command in a workspace and capture stdout/stderr."""
    started = time.perf_counter()
    env = _execution_env()
    psutil_process = None
    max_rss_mb: float | None = None
    cpu_samples: list[float] = []
    output_events: queue.Queue[tuple[str, str]] = queue.Queue()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        popen_kwargs = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": True,
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
        if process.stdin is not None:
            try:
                process.stdin.write(stdin_text)
            except OSError:
                pass
            try:
                process.stdin.close()
            except OSError:
                pass

        readers = [
            threading.Thread(
                target=_read_process_stream,
                args=(process.stdout, "stdout", stdout_chunks, output_events),
                daemon=True,
            ),
            threading.Thread(
                target=_read_process_stream,
                args=(process.stderr, "stderr", stderr_chunks, output_events),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        try:
            import psutil

            psutil_process = psutil.Process(process.pid)
            psutil_process.cpu_percent(interval=None)
        except Exception:
            psutil_process = None

        while process.poll() is None:
            _drain_output_events(output_events, output_callback)
            # ``timeout_seconds`` of None/0 means "no timeout" (autonomous mode may
            # pass a very large cap, or None to never kill on slowness).
            if timeout_seconds and time.perf_counter() - started > timeout_seconds:
                _terminate_process_tree(process, psutil_process)
                for reader in readers:
                    reader.join(timeout=1)
                _drain_output_events(output_events, output_callback)
                stdout = strip_terminal_control("".join(stdout_chunks))
                stderr = strip_terminal_control("".join(stderr_chunks))
                timeout_stderr = _timeout_message(command, timeout_seconds, stdout, stderr)
                _emit_output(output_callback, "stderr", f"\n{timeout_stderr}")
                duration = time.perf_counter() - started
                return CommandResult(
                    command=command,
                    cwd=str(cwd),
                    stdout=stdout or "",
                    stderr=timeout_stderr,
                    exit_code=-1,
                    duration_seconds=duration,
                    timed_out=True,
                    cpu_percent=_average(cpu_samples),
                    ram_mb=max_rss_mb,
                    peak_ram_mb=max_rss_mb,
                )
            if psutil_process is not None:
                try:
                    mem_mb = psutil_process.memory_info().rss / (1024 * 1024)
                    max_rss_mb = max(max_rss_mb or 0, mem_mb)
                    cpu_samples.append(float(psutil_process.cpu_percent(interval=None)))
                except Exception:
                    pass
            time.sleep(0.1)

        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        _drain_output_events(output_events, output_callback)
        stdout = strip_terminal_control("".join(stdout_chunks))
        stderr = strip_terminal_control("".join(stderr_chunks))
        duration = time.perf_counter() - started
        return CommandResult(
            command=command,
            cwd=str(cwd),
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=process.returncode,
            duration_seconds=duration,
            cpu_percent=_average(cpu_samples),
            ram_mb=max_rss_mb,
            peak_ram_mb=max_rss_mb,
        )
    except Exception as exc:
        duration = time.perf_counter() - started
        return CommandResult(
            command=command,
            cwd=str(cwd),
            stdout="",
            stderr=str(exc),
            exit_code=-1,
            duration_seconds=duration,
        )


_PATH_TOKEN_RE = re.compile(r"""(?:path|file)=["']?(?P<path>[^"'\s]+)["']?""")


def materialize_path_marked_code_blocks(response_text: str, workspace: Path) -> list[str]:
    """Write only explicitly path-marked fenced code blocks to the workspace."""
    from .workspaces import safe_workspace_path

    written: list[str] = []
    current_path: str | None = None
    buffer: list[str] = []

    for raw_line in response_text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("```"):
            if current_path is not None:
                target = safe_workspace_path(workspace, current_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(buffer).rstrip() + "\n", encoding="utf-8")
                written.append(current_path.replace("\\", "/"))
                current_path = None
                buffer = []
                continue

            meta = line[3:].strip()
            path = _extract_fence_path(meta)
            if path:
                current_path = path
                buffer = []
            continue

        if current_path is not None:
            buffer.append(line)

    return written


def _extract_fence_path(meta: str) -> str | None:
    if not meta:
        return None
    match = _PATH_TOKEN_RE.search(meta)
    if match:
        return _clean_path(match.group("path"))

    tokens = [token.strip() for token in meta.split() if token.strip()]
    # Handle ``language:path`` info strings, e.g. ```python:blog/models.py
    if len(tokens) == 1 and ":" in tokens[0]:
        _language, _, rest = tokens[0].partition(":")
        cleaned = _clean_path(rest)
        if rest and ("/" in cleaned or "." in Path(cleaned).name):
            return cleaned

    candidates = tokens if len(tokens) == 1 else tokens[1:]
    for token in candidates:
        cleaned = _clean_path(token)
        name = Path(cleaned).name
        if "/" in cleaned or "\\" in cleaned or "." in name:
            return cleaned
    return None


def _clean_path(value: str) -> str:
    return value.strip().strip("`'\"").replace("\\", "/")


def _average(values: list[float]) -> float | None:
    usable = [value for value in values if value >= 0]
    if not usable:
        return None
    return sum(usable) / len(usable)


def strip_terminal_control(output: str) -> str:
    """Remove terminal control/progress noise while preserving meaningful text."""
    text = ANSI_RE.sub("", output or "")
    text = SPINNER_CHARS_RE.sub("", text)
    text = text.replace("\r", "\n")
    text = CONTROL_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        if _is_progress_noise(line):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def compact_command_output(output: str, limit: int = 1200) -> str:
    """Return a readable tail suitable for summaries and UI error messages."""
    text = strip_terminal_control(output)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    return f"...\n{text[-limit:]}"


def _is_progress_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in {"K", "[K"}:
        return True
    return bool(PROGRESS_NOISE_RE.fullmatch(stripped))


def _timeout_message(command: str, timeout_seconds: int, stdout: str, stderr: str) -> str:
    message = f"Command timed out after {timeout_seconds} seconds and was stopped."
    ollama_model = _extract_ollama_model(command)
    if ollama_model:
        message += (
            f" Ollama model '{ollama_model}' did not finish before the configured timeout. "
            "Increase this model's timeout, choose a faster installed model, or run the command manually to inspect it."
        )
    else:
        message += " Increase the timeout or inspect the command manually if this runtime is expected."

    tail = compact_command_output(stderr or stdout, limit=1200)
    if tail and "Command timed out after" not in tail:
        message += f"\n\nLast output:\n{tail}"
    return message


def _extract_ollama_model(command: str) -> str | None:
    match = OLLAMA_RUN_RE.search(command)
    if not match:
        return None
    return match.group("model").strip().strip("'\"")


def _terminate_process_tree(process: subprocess.Popen, psutil_process) -> None:
    if psutil_process is not None:
        try:
            import psutil

            processes = psutil_process.children(recursive=True) + [psutil_process]
            for item in processes:
                try:
                    item.kill()
                except Exception:
                    pass
            psutil.wait_procs(processes, timeout=5)
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            return
        except Exception:
            pass

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()

    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        finally:
            process.wait()


def _read_process_stream(pipe, stream: str, chunks: list[str], events: queue.Queue[tuple[str, str]]) -> None:
    if pipe is None:
        return
    try:
        while True:
            text = pipe.readline()
            if not text:
                break
            chunks.append(text)
            events.put((stream, text))
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _drain_output_events(events: queue.Queue[tuple[str, str]], callback: OutputCallback | None) -> None:
    while True:
        try:
            stream, text = events.get_nowait()
        except queue.Empty:
            break
        _emit_output(callback, stream, text)


def _emit_output(callback: OutputCallback | None, stream: str, text: str) -> None:
    if callback is None or not text:
        return
    try:
        callback(stream, text)
    except Exception:
        pass


def _execution_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    root = Path(__file__).resolve().parents[2]
    local_site_packages = root / "Lib" / "site-packages"
    parts = [str(root)]
    if local_site_packages.exists():
        parts.append(str(local_site_packages))
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env
