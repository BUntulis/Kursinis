"""Pass@k metrikos fasadas."""
from __future__ import annotations

from .PassAtKMetric import PassAtKMetric
from .TaskResult import TaskResult


def pass_at_k(n: int, c: int, k: int) -> float:
    return PassAtKMetric.for_task(n, c, k)


def aggregate_pass_at_k(results: list[TaskResult], k: int) -> float:
    return PassAtKMetric.aggregate(results, k)
