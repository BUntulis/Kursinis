"""Background system-resource sampler for autonomous benchmark runs.

A daemon thread samples CPU / RAM / GPU / VRAM / temperatures / free disk every
``interval`` seconds while one model run executes, persists each tick as a
``ResourceSample`` (powering the live charts), tracks peaks/averages onto the
``ModelRun``, and emits a compact status line to the job feed periodically.

GPU/VRAM/temperature come from ``nvidia-smi`` (same approach as
``recommend_models._detect_gpus``); CPU/RAM/disk from ``psutil``. On machines
without an NVIDIA GPU the GPU fields are simply ``None``. ``psutil``'s CPU
temperature is usually unavailable on Windows, so ``cpu_temp_c`` may stay null.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable

from django.db import close_old_connections

from dashboard.models import ModelRun, ResourceSample


LogCb = Callable[[str], None]


def query_gpu() -> tuple[float | None, float | None, float | None, float | None]:
    """Return ``(util_pct, vram_used_mb, vram_total_mb, temp_c)`` via nvidia-smi, all None if unavailable."""
    executable = shutil.which("nvidia-smi") or "nvidia-smi"
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None, None, None, None
    if result.returncode != 0:
        return None, None, None, None
    line = (result.stdout.strip().splitlines() or [""])[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None, None, None, None

    def _f(value: str) -> float | None:
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    return _f(parts[0]), _f(parts[1]), _f(parts[2]), _f(parts[3])


class ResourceMonitor:
    """Sample system resources for one ModelRun on a background thread."""

    def __init__(self, model_run_id: int, interval: float = 1.5, log_cb: LogCb | None = None, status_every: float = 15.0) -> None:
        self.model_run_id = model_run_id
        self.interval = max(0.25, float(interval))
        self.log_cb = log_cb
        self.status_every = status_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"resmon-{model_run_id}", daemon=True)
        self._started_at: float | None = None
        self._last_tps: float | None = None
        # running aggregates
        self._cpu_sum = 0.0
        self._cpu_n = 0
        self._gpu_sum = 0.0
        self._gpu_n = 0
        self._peak = {"gpu": None, "vram": None, "gpu_temp": None, "cpu_temp": None}
        self._vram_total: float | None = None
        self._last_disk_free: float | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self._started_at = time.perf_counter()
        self._thread.start()

    def record_tokens_per_second(self, tps: float | None) -> None:
        """Called by the agent runner after a coder step so samples carry live tok/s."""
        if tps is not None:
            self._last_tps = float(tps)

    def stop_and_summarize(self) -> dict:
        """Stop sampling, persist peaks/averages onto the ModelRun, return a summary."""
        self._stop.set()
        self._thread.join(timeout=self.interval * 3 + 2)
        summary = {
            "gpu_percent_avg": (self._gpu_sum / self._gpu_n) if self._gpu_n else None,
            "gpu_percent_peak": self._peak["gpu"],
            "vram_used_mb_peak": self._peak["vram"],
            "vram_total_mb": self._vram_total,
            "gpu_temp_c_peak": self._peak["gpu_temp"],
            "cpu_temp_c_peak": self._peak["cpu_temp"],
            "disk_free_gb": self._last_disk_free,
            "cpu_percent_avg": (self._cpu_sum / self._cpu_n) if self._cpu_n else None,
        }
        update_fields = {
            "gpu_percent_avg": summary["gpu_percent_avg"],
            "gpu_percent_peak": summary["gpu_percent_peak"],
            "vram_used_mb_peak": summary["vram_used_mb_peak"],
            "vram_total_mb": summary["vram_total_mb"],
            "gpu_temp_c_peak": summary["gpu_temp_c_peak"],
            "cpu_temp_c_peak": summary["cpu_temp_c_peak"],
            "disk_free_gb": summary["disk_free_gb"],
        }
        # Persist the system-wide CPU average into the existing cpu_percent field
        # (the agent path has no per-process sample otherwise).
        if summary["cpu_percent_avg"] is not None:
            update_fields["cpu_percent"] = summary["cpu_percent_avg"]
        try:
            close_old_connections()
            ModelRun.objects.filter(id=self.model_run_id).update(**update_fields)
        except Exception:
            pass
        return summary

    # ------------------------------------------------------------------ thread
    def _loop(self) -> None:
        close_old_connections()
        try:
            import psutil

            psutil.cpu_percent(interval=None)  # prime the rolling % counter
        except Exception:
            psutil = None  # type: ignore[assignment]
        next_status = self.status_every
        try:
            while not self._stop.wait(self.interval):
                sample = self._sample(psutil)
                self._persist(sample)
                self._aggregate(sample)
                elapsed = sample["elapsed_seconds"]
                if self.log_cb and elapsed >= next_status:
                    next_status = elapsed + self.status_every
                    try:
                        self.log_cb(self._status_line(sample))
                    except Exception:
                        # A failed status log (e.g. transient DB error) must not
                        # kill the sampler or leak the thread's DB connection.
                        pass
        finally:
            close_old_connections()

    def _sample(self, psutil) -> dict:
        elapsed = time.perf_counter() - (self._started_at or time.perf_counter())
        cpu = ram_used = ram_total = disk_free = cpu_temp = None
        if psutil is not None:
            try:
                cpu = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu = None
            try:
                vm = psutil.virtual_memory()
                ram_used = vm.used / (1024 * 1024)
                ram_total = vm.total / (1024 * 1024)
            except Exception:
                pass
            try:
                from django.conf import settings as dj_settings

                disk_free = psutil.disk_usage(str(dj_settings.BASE_DIR)).free / (1024 ** 3)
            except Exception:
                disk_free = None
            cpu_temp = _cpu_temperature(psutil)
        gpu, vram_used, vram_total, gpu_temp = query_gpu()
        return {
            "elapsed_seconds": round(elapsed, 2),
            "cpu_percent": cpu,
            "ram_used_mb": ram_used,
            "ram_total_mb": ram_total,
            "gpu_percent": gpu,
            "vram_used_mb": vram_used,
            "vram_total_mb": vram_total,
            "gpu_temp_c": gpu_temp,
            "cpu_temp_c": cpu_temp,
            "disk_free_gb": disk_free,
            "tokens_per_second": self._last_tps,
        }

    def _persist(self, sample: dict) -> None:
        try:
            ResourceSample.objects.create(model_run_id=self.model_run_id, **sample)
        except Exception:
            # Tolerate transient SQLite locks; a missed sample is not fatal.
            pass

    def _aggregate(self, sample: dict) -> None:
        if sample["cpu_percent"] is not None:
            self._cpu_sum += sample["cpu_percent"]
            self._cpu_n += 1
        if sample["gpu_percent"] is not None:
            self._gpu_sum += sample["gpu_percent"]
            self._gpu_n += 1
            self._peak["gpu"] = _max(self._peak["gpu"], sample["gpu_percent"])
        if sample["vram_used_mb"] is not None:
            self._peak["vram"] = _max(self._peak["vram"], sample["vram_used_mb"])
        if sample["vram_total_mb"] is not None:
            self._vram_total = sample["vram_total_mb"]
        if sample["gpu_temp_c"] is not None:
            self._peak["gpu_temp"] = _max(self._peak["gpu_temp"], sample["gpu_temp_c"])
        if sample["cpu_temp_c"] is not None:
            self._peak["cpu_temp"] = _max(self._peak["cpu_temp"], sample["cpu_temp_c"])
        if sample["disk_free_gb"] is not None:
            self._last_disk_free = sample["disk_free_gb"]

    def _status_line(self, sample: dict) -> str:
        bits = [f"t={sample['elapsed_seconds']:.0f}s"]
        if sample["cpu_percent"] is not None:
            bits.append(f"CPU {sample['cpu_percent']:.0f}%")
        if sample["ram_used_mb"] and sample["ram_total_mb"]:
            bits.append(f"RAM {sample['ram_used_mb']/1024:.1f}/{sample['ram_total_mb']/1024:.1f}GB")
        if sample["gpu_percent"] is not None:
            bits.append(f"GPU {sample['gpu_percent']:.0f}%")
        if sample["vram_used_mb"] is not None and sample["vram_total_mb"]:
            bits.append(f"VRAM {sample['vram_used_mb']/1024:.1f}/{sample['vram_total_mb']/1024:.1f}GB")
        if sample["gpu_temp_c"] is not None:
            bits.append(f"{sample['gpu_temp_c']:.0f}°C")
        if sample["tokens_per_second"]:
            bits.append(f"{sample['tokens_per_second']:.1f} tok/s")
        return "[monitor] " + " · ".join(bits)


def _cpu_temperature(psutil) -> float | None:
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        readings = sensors()
    except Exception:
        return None
    for entries in (readings or {}).values():
        for entry in entries:
            if getattr(entry, "current", None):
                return float(entry.current)
    return None


def _max(current: float | None, value: float) -> float:
    return value if current is None else max(current, value)
