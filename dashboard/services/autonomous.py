"""Autonomous benchmark mode: config resolution, pre-run planning, bottleneck analysis.

Realises the three requested "modes" as concrete mechanisms:
  * Planning  -> ``build_execution_plan`` (hardware snapshot + per-model install
                 status + task spec + iteration/timeout/recovery policy), logged
                 before any model runs.
  * Thinking  -> the dynamic agent's own router/plan steps + this pre-run analysis and
                 the runtime ``detect_bottlenecks`` reasoning.
  * Goal Pursuit -> the dynamic, model-routed tool loop (the model chooses plan/read/grep/
                 write/edit/run/test each step) run until the tests pass or the step budget
                 is hit, plus the large timeout cap + recovery retries driven by the config.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from .ollama_models import installed_models, model_installed


_OLLAMA_RUN_RE = re.compile(r'^\s*(?:"?ollama(?:\.exe)?"?)\s+run\s+(?P<model>[^\s]+)', re.IGNORECASE)


def extract_ollama_tag(command: str) -> str | None:
    """Return the model tag from an ``ollama run <tag>`` command, else None."""
    match = _OLLAMA_RUN_RE.search(command or "")
    if not match:
        return None
    return match.group("model").strip().strip("'\"")


def run_settings(benchmark) -> dict:
    """Merge the benchmark_template settings with the per-run settings (run wins)."""
    merged: dict = {}
    template = getattr(benchmark, "benchmark_template", None)
    if template is not None and isinstance(getattr(template, "settings", None), dict):
        merged.update(template.settings)
    if isinstance(getattr(benchmark, "settings", None), dict):
        merged.update(benchmark.settings)
    return merged


def autonomous_config(benchmark) -> dict:
    """Resolve the effective per-run configuration (defaults <- template <- run)."""
    from config import settings as cfg

    merged = run_settings(benchmark)
    # Default OFF when no flag is present (programmatic / restart runs). The run pages
    # set it explicitly — the "Autonomous mode" checkbox defaults checked, so
    # UI-created runs are autonomous unless the user unticks it.
    autonomous = bool(merged.get("autonomous", False))
    return {
        "autonomous": autonomous,
        "auto_install": bool(merged.get("auto_install", autonomous)),
        "monitor": bool(merged.get("monitor", autonomous)),
        "timeout_cap_sec": int(merged.get("timeout_cap_sec", cfg.autonomous_timeout_cap_sec)),
        "sandbox_timeout_sec": int(merged.get("sandbox_timeout_sec", cfg.autonomous_sandbox_timeout_sec)),
        "monitor_interval_sec": float(merged.get("monitor_interval_sec", cfg.autonomous_monitor_interval_sec)),
        "generation_retries": int(merged.get("generation_retries", cfg.autonomous_generation_retries)),
        # existing agent knobs (kept here so callers read settings in one place)
        "use_agent": bool(merged.get("use_agent", True)),
        "use_rag": bool(merged.get("use_rag", False)),
        "max_iterations": int(merged.get("max_iterations") or cfg.agent_max_iterations),
        "prompt_overrides": merged.get("prompt_overrides") or {},
        # tool-using agent (read/write/execute)
        "enable_tools": bool(merged.get("enable_tools", cfg.agent_enable_tools)),
        "max_tool_steps": int(merged.get("max_tool_steps", cfg.agent_max_tool_steps)),
        "shell_timeout": int(merged.get("shell_timeout", cfg.agent_shell_timeout_sec)),
    }


# --------------------------------------------------------------------- hardware
def gpu_info() -> list[dict]:
    """Return [{name, vram_total_mb, vram_used_mb}] via nvidia-smi, or []."""
    executable = shutil.which("nvidia-smi") or "nvidia-smi"
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    gpus: list[dict] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = float(parts[1])
            used = float(parts[2])
        except ValueError:
            continue
        gpus.append({"name": parts[0], "vram_total_mb": total, "vram_used_mb": used})
    return gpus


def hardware_snapshot() -> dict:
    """Snapshot CPU cores, RAM, and GPUs (best-effort; missing pieces are None/[])."""
    snap: dict = {"cpu_count": None, "ram_total_gb": None, "gpus": gpu_info()}
    try:
        import psutil

        snap["cpu_count"] = psutil.cpu_count(logical=True)
        snap["ram_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass
    return snap


# ------------------------------------------------------------------------- plan
def build_execution_plan(benchmark, models, config: dict, expected_files=None, test_suite_count: int = 0) -> str:
    """Build the human-readable pre-run execution plan (Planning Mode output)."""
    hw = hardware_snapshot()
    try:
        installed = installed_models()
    except Exception:
        installed = set()

    lines: list[str] = []
    lines.append("Autonomous mode ACTIVE — Thinking · Goal Pursuit · Planning")
    lines.append("")
    lines.append("Hardware:")
    if hw["cpu_count"]:
        lines.append(f"  CPU: {hw['cpu_count']} logical cores")
    if hw["ram_total_gb"]:
        lines.append(f"  RAM: {hw['ram_total_gb']} GB")
    if hw["gpus"]:
        for gpu in hw["gpus"]:
            total_gb = gpu["vram_total_mb"] / 1024
            used_gb = gpu["vram_used_mb"] / 1024
            lines.append(f"  GPU: {gpu['name']} ({total_gb:.1f} GB VRAM, {used_gb:.1f} GB used)")
    else:
        lines.append("  GPU: none detected (nvidia-smi unavailable) — CPU inference")
    lines.append("")

    lines.append(f"Models (run one-by-one, {len(models)}):")
    for index, model in enumerate(models, start=1):
        tag = extract_ollama_tag(getattr(model, "execution_command", "") or "")
        if tag:
            if model_installed(tag, installed):
                status = "installed"
            elif config.get("auto_install"):
                status = "will auto-pull"
            else:
                status = "not installed → fallback"
            lines.append(f"  {index}. {model.name} → {tag} [{status}]")
        else:
            lines.append(f"  {index}. {model.name} [non-ollama / API]")
    lines.append("")

    lines.append("Task:")
    if expected_files:
        lines.append(f"  expected files: {', '.join(expected_files)}")
    lines.append(f"  connected test suites: {test_suite_count}")
    lines.append("")

    lines.append("Policy:")
    lines.append(
        "  goal-pursuit: dynamic agent — the model chooses each step "
        f"(plan / retrieve / read / grep / write / edit / run / test), up to "
        f"{config.get('max_tool_steps', '?')} tool steps until the tests pass"
    )
    lines.append(f"  timeout cap: {config['timeout_cap_sec']}s generation / {config['sandbox_timeout_sec']}s sandbox (no early stop on slowness)")
    lines.append(f"  auto-install missing models: {'on' if config['auto_install'] else 'off'}")
    lines.append(f"  recovery: up to {config['generation_retries']} retries + fallback to an installed model")
    lines.append(f"  live monitoring: {'on' if config['monitor'] else 'off'} (CPU/RAM/GPU/VRAM/temp/tokens-per-sec)")
    return "\n".join(lines)


# ------------------------------------------------------------------ bottlenecks
def detect_bottlenecks(summary: dict, model_run) -> list[str]:
    """Return human-readable bottleneck warnings from a monitor summary + model run."""
    warnings: list[str] = []
    vram_used = summary.get("vram_used_mb_peak")
    vram_total = summary.get("vram_total_mb")
    if vram_used and vram_total and vram_used / vram_total >= 0.92:
        warnings.append(
            f"GPU VRAM saturated ({vram_used/1024:.1f}/{vram_total/1024:.1f} GB peak) — the model is likely "
            "spilling to CPU/RAM, which is much slower. A smaller installed model would run faster."
        )
    cpu_avg = summary.get("cpu_percent_avg")
    gpu_avg = summary.get("gpu_percent_avg")
    if cpu_avg and (gpu_avg is None or gpu_avg < 15) and cpu_avg >= 70:
        warnings.append(
            f"Compute is CPU-bound (CPU ~{cpu_avg:.0f}%, GPU ~{(gpu_avg or 0):.0f}%) — inference is not using the GPU."
        )
    tps = getattr(model_run, "tokens_per_second", None)
    if tps is not None and tps and tps < 5:
        warnings.append(f"Low inference throughput ({tps:.1f} tok/s).")
    return warnings
