"""Ollama model presence checks + autonomous auto-install (``ollama pull``).

Consolidates the install-check logic that previously lived in two places
(``benchmark._installed_ollama_models`` and ``OllamaBackend._installed_models``)
and adds an autonomous ``ensure_model`` that downloads a missing model, falling
back to an installed coder model only if the pull fails.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from src.llm.OllamaBackend import OllamaBackend


LogCb = Callable[[str], None]

#: Milestone lines worth surfacing from `ollama pull` progress (the rest is noise).
_PULL_MILESTONE_RE = re.compile(
    r"pulling|manifest|verifying|writing|downloading|success|error|already exists", re.IGNORECASE
)


def installed_models() -> set[str]:
    """Return the set of installed ollama model tags (e.g. ``{'codellama:latest'}``)."""
    proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"ollama list exited with {proc.returncode}")
    tags: set[str] = set()
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            tags.add(parts[0])
    return tags


def model_installed(tag: str, installed: set[str] | None = None) -> bool:
    """True if ``tag`` (or any tag sharing its base name) is installed."""
    pool = installed if installed is not None else installed_models()
    if tag in pool:
        return True
    base = tag.split(":", 1)[0]
    return any(candidate.split(":", 1)[0] == base for candidate in pool)


def pick_fallback(installed: set[str]) -> str | None:
    """Pick an installed coder model using OllamaBackend's preference order."""
    for preference in OllamaBackend.FALLBACK_PREFERENCES:
        for candidate in installed:
            if candidate.split(":", 1)[0].startswith(preference) or preference in candidate:
                return candidate
    return next(iter(sorted(installed)), None)


def pull_model(tag: str, log_cb: LogCb | None = None, timeout: int | None = None) -> bool:
    """Stream ``ollama pull <tag>`` (milestone lines only); return True on success."""
    from .commands import run_command

    if log_cb:
        log_cb(f"Auto-install: pulling Ollama model '{tag}' (may take a while / several GB)…")

    last_keyword: list[str] = [""]

    def _forward(_stream: str, text: str) -> None:
        if not log_cb:
            return
        line = text.strip()
        if not line:
            return
        match = _PULL_MILESTONE_RE.search(line)
        # Dedupe by milestone keyword so repeated progress ticks for the same phase
        # ("pulling … 12%", "pulling … 47%") only log once.
        if not match or match.group(0).lower() == last_keyword[0]:
            return
        last_keyword[0] = match.group(0).lower()
        log_cb(f"  ollama pull: {line[:160]}")

    result = run_command(
        f'ollama pull "{tag}"',
        Path.cwd(),
        timeout_seconds=timeout or 0,  # 0 -> no timeout; downloads can be long
        output_callback=_forward if log_cb else None,
    )
    ok = result.exit_code == 0
    if log_cb:
        log_cb(f"Auto-install {'succeeded' if ok else 'FAILED'} for '{tag}' (exit {result.exit_code}).")
    return ok


def installed_models_detailed() -> list[dict]:
    """Installed models with size + modified age, parsed from ``ollama list`` columns.

    Output shape: ``NAME  ID  SIZE  MODIFIED`` where SIZE is two tokens ("4.9 GB") and
    MODIFIED is the trailing words ("11 months ago").
    """
    proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"ollama list exited with {proc.returncode}")
    rows = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        rows.append({
            "tag": parts[0],
            "id": parts[1],
            "size": " ".join(parts[2:4]),
            "modified": " ".join(parts[4:]),
        })
    return rows


def remove_model(tag: str) -> tuple[bool, str]:
    """``ollama rm <tag>`` — returns ``(ok, message)``."""
    try:
        proc = subprocess.run(["ollama", "rm", tag], capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()[:300]


# ---------------------------------------------------------------- background pulls (Models page)
#: tag -> {"status": "pulling"|"done"|"failed", "line": last progress line, "started": ts}
_PULLS: dict[str, dict] = {}
_PULLS_LOCK = threading.Lock()


def start_background_pull(tag: str) -> tuple[bool, str]:
    """Start ``ollama pull <tag>`` in a daemon thread; progress is polled via pull_states().

    Returns ``(started, message)`` — False when a pull for this tag is already running.
    """
    with _PULLS_LOCK:
        cur = _PULLS.get(tag)
        if cur and cur["status"] == "pulling":
            return False, "Already downloading."
        _PULLS[tag] = {"status": "pulling", "line": "starting download…", "started": time.time()}

    def _set(**kw):
        with _PULLS_LOCK:
            _PULLS.setdefault(tag, {}).update(kw)

    def _run():
        try:
            proc = subprocess.Popen(
                ["ollama", "pull", tag],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            for raw in proc.stdout or []:
                line = raw.strip()
                # `ollama pull` redraws progress with \r; keep the last fragment of the line.
                if "\r" in raw:
                    line = raw.split("\r")[-1].strip()
                if line:
                    _set(line=line[:200])
            code = proc.wait()
            _set(status="done" if code == 0 else "failed",
                 line="download complete" if code == 0 else f"pull failed (exit {code})")
        except Exception as exc:  # CLI missing / killed
            _set(status="failed", line=str(exc)[:200])

    threading.Thread(target=_run, daemon=True).start()
    return True, "Download started."


def pull_states() -> dict[str, dict]:
    """Snapshot of all known pulls (finished ones are reported once, then expire)."""
    with _PULLS_LOCK:
        snap = {t: dict(s) for t, s in _PULLS.items()}
        # drop finished entries older than 5 minutes so the map doesn't grow forever
        cutoff = time.time() - 300
        for t in [t for t, s in _PULLS.items() if s["status"] != "pulling" and s["started"] < cutoff]:
            del _PULLS[t]
    return snap


def ensure_model(
    tag: str,
    log_cb: LogCb | None = None,
    auto_install: bool = True,
    timeout: int | None = None,
) -> tuple[str, str]:
    """Ensure an ollama tag is runnable, optionally pulling it.

    Returns ``(resolved_tag, status)`` where status is one of:
      * ``'present'``    – already installed (verified)
      * ``'unverified'`` – could not check (ollama list failed); use the tag as-is
      * ``'pulled'``     – downloaded just now
      * ``'fallback'``   – not installable, substituted an installed model
      * ``'missing'``    – nothing usable (returns the requested tag as last resort)
    """
    try:
        installed = installed_models()
    except Exception as exc:  # ollama not running / CLI missing
        if log_cb:
            log_cb(f"Could not list installed Ollama models ({exc}); using '{tag}' as-is.")
        return tag, "unverified"

    if model_installed(tag, installed):
        return tag, "present"

    if auto_install and pull_model(tag, log_cb=log_cb, timeout=timeout):
        return tag, "pulled"

    fallback = pick_fallback(installed)
    if fallback:
        if log_cb:
            log_cb(f"Using installed fallback model '{fallback}' instead of '{tag}'.")
        return fallback, "fallback"
    return tag, "missing"
