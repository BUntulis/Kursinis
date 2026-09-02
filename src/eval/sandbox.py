"""Django sandbox fasadas."""
from __future__ import annotations

from src.benchmark import BenchmarkTask

from .DjangoProjectScaffolder import DjangoProjectScaffolder
from .DjangoSandbox import DjangoSandbox
from .GeneratedCodeAnalyzer import GeneratedCodeAnalyzer
from .PytestRunner import PytestRunner
from .SandboxResult import SandboxResult


def run_in_sandbox(
    task: BenchmarkTask,
    generated_files: dict[str, str],
) -> SandboxResult:
    return DjangoSandbox().run(task, generated_files)
