"""Benchmark loader'io fasadas."""
from __future__ import annotations

from pathlib import Path

from .BenchmarkLoader import BenchmarkLoader
from .BenchmarkTask import BenchmarkTask


def load_all_tasks(directory: Path | None = None) -> list[BenchmarkTask]:
    return BenchmarkLoader(directory).load_all()


def load_task(task_id: str, directory: Path | None = None) -> BenchmarkTask:
    return BenchmarkLoader(directory).load(task_id)
