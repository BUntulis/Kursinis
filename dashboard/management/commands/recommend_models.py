"""Recommend Ollama / API model tags that fit the detected hardware.

Usage:
    python manage.py recommend_models

Detects available GPU VRAM (via ``nvidia-smi``) and total system RAM, then prints
a ranked, role-grouped table of recommended models. When ``OPENAI_API_KEY`` is
set it also prints an API-based recommendation. Finally it checks the benchmark
AIModel seed data and suggests any recommended models that are missing.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from django.core.management.base import BaseCommand


# Local (Ollama) recommendations, grouped by agent role. Sized for a 4 GB GPU.
LOCAL_RECOMMENDATIONS = [
    ("Planner", "qwen2.5-coder:7b-instruct", "~4 GB Q4", "fits RTX 3060"),
    ("Coder", "qwen2.5-coder:7b-instruct", "~4 GB Q4", "already configured"),
    ("Critic", "qwen2.5-coder:3b", "~2 GB Q4", "fast feedback"),
    ("Embedding", "nomic-embed-text", "CPU only", "already configured"),
]

# API recommendations, used when OPENAI_API_KEY is configured.
API_RECOMMENDATIONS = [
    ("Planner+Coder", "claude-haiku-4-5-20251001", "fast + affordable"),
    ("Coder (best)", "claude-sonnet-4-6", "best quality/cost balance"),
    ("Critic", "claude-haiku-4-5-20251001", "sufficient for critique"),
]

# Generation models worth having in the AIModel seed data. (nomic-embed-text is an
# embedding model configured via EMBED_MODEL, not an AIModel, so it is excluded.)
SEED_SUGGESTIONS = [
    "qwen2.5-coder:7b-instruct",
    "qwen2.5-coder:3b",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
]

SEED_FILE = "dashboard/services/seed_data.py"
SEED_COMMAND = "dashboard/management/commands/seed_benchmark_data.py"


class Command(BaseCommand):
    """Recommend models for the detected hardware."""

    help = "Recommend Ollama/API model tags that fit the available GPU VRAM and system RAM."

    def handle(self, *args, **options) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Detected hardware"))
        gpus = self._detect_gpus()
        ram_gb = self._detect_ram_gb()
        self._report_hardware(gpus, ram_gb)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Recommended local models (Ollama)"))
        self._print_table(
            ["Role", "Recommended model", "VRAM needed", "Notes"],
            LOCAL_RECOMMENDATIONS,
        )
        self._print_vram_advice(gpus)

        if os.getenv("OPENAI_API_KEY"):
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("Recommended API models (OPENAI_API_KEY detected)"))
            self._print_table(
                ["Role", "Recommended model", "Notes"],
                API_RECOMMENDATIONS,
            )
        else:
            self.stdout.write("")
            self.stdout.write(
                "API workflow: set OPENAI_API_KEY to also see API model recommendations "
                "(claude-haiku-4-5-20251001 / claude-sonnet-4-6)."
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Seed data suggestions"))
        self._report_seed_suggestions()

    # ----- hardware detection -------------------------------------------------

    def _detect_gpus(self) -> list[tuple[str, int | None]] | None:
        """Return [(gpu_name, vram_mb), ...] via nvidia-smi, or None if unavailable."""
        executable = shutil.which("nvidia-smi") or "nvidia-smi"
        try:
            result = subprocess.run(
                [executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        gpus: list[tuple[str, int | None]] = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                vram_mb: int | None = int(float(parts[1]))
            except ValueError:
                vram_mb = None
            gpus.append((parts[0], vram_mb))
        return gpus or None

    def _detect_ram_gb(self) -> float | None:
        """Return total system RAM in GB, or None if it cannot be detected."""
        try:
            import psutil

            return psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            return None

    def _report_hardware(self, gpus, ram_gb) -> None:
        if gpus:
            for name, vram_mb in gpus:
                vram = f"{vram_mb} MB (~{vram_mb / 1024:.1f} GB)" if vram_mb else "unknown VRAM"
                self.stdout.write(f"  GPU: {name} - {vram}")
        else:
            self.stdout.write("  GPU: no NVIDIA GPU detected (nvidia-smi unavailable).")
        if ram_gb is not None:
            self.stdout.write(f"  RAM: ~{ram_gb:.1f} GB total")
        else:
            self.stdout.write("  RAM: unknown (psutil not available).")

    def _max_vram_mb(self, gpus) -> int | None:
        if not gpus:
            return None
        values = [vram for _name, vram in gpus if vram is not None]
        return max(values) if values else None

    def _print_vram_advice(self, gpus) -> None:
        vram_mb = self._max_vram_mb(gpus)
        if vram_mb is None:
            self.stdout.write(
                "  Note: no GPU detected - local models run on CPU/RAM (slow). "
                "Prefer small tags (qwen2.5-coder:3b) or the API workflow."
            )
            return
        if vram_mb < 4000:
            self.stdout.write(
                f"  Note: only ~{vram_mb / 1024:.1f} GB VRAM - 7B Q4 may not fit; "
                "prefer qwen2.5-coder:3b for all local roles."
            )
        elif vram_mb < 8000:
            self.stdout.write(
                f"  Note: ~{vram_mb / 1024:.1f} GB VRAM - the 7B Q4 recommendation is the right fit."
            )
        else:
            self.stdout.write(
                f"  Note: ~{vram_mb / 1024:.1f} GB VRAM - qwen2.5-coder:14b (~9 GB Q4) "
                "is also viable for higher quality."
            )

    # ----- seed data ----------------------------------------------------------

    def _seed_blob(self) -> str:
        try:
            from dashboard.services.seed_data import MODELS

            return " ".join(f"{name} {command}" for name, _type, _active, _order, command in MODELS).lower()
        except Exception:
            return ""

    def _report_seed_suggestions(self) -> None:
        blob = self._seed_blob()
        missing = [tag for tag in SEED_SUGGESTIONS if tag.lower() not in blob]
        if not blob:
            self.stdout.write("  Could not read the seed data; skipping seed suggestions.")
            return
        if not missing:
            self.stdout.write(self.style.SUCCESS("  All recommended models are already present in the seed data."))
            return
        self.stdout.write(
            f"  The following recommended models are NOT in the AIModel seed data ({SEED_FILE}):"
        )
        for tag in missing:
            self.stdout.write(f"    - {tag}")
        self.stdout.write(
            f"  Consider adding them to the MODELS list in {SEED_FILE} (seeded by "
            f"{SEED_COMMAND}), then run: python manage.py seed_benchmark_data --overwrite"
        )

    # ----- helpers ------------------------------------------------------------

    def _print_table(self, headers, rows) -> None:
        widths = [len(header) for header in headers]
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(str(cell)))
        gap = "  "
        self.stdout.write("  " + gap.join(header.ljust(widths[index]) for index, header in enumerate(headers)))
        self.stdout.write("  " + gap.join("-" * widths[index] for index in range(len(headers))))
        for row in rows:
            self.stdout.write(
                "  " + gap.join(str(cell).ljust(widths[index]) for index, cell in enumerate(row))
            )
