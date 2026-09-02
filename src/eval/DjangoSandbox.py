"""Django sandbox vykdymo klasė."""
from __future__ import annotations

import tempfile
from pathlib import Path

from config import settings
from src.benchmark import BenchmarkTask

from .DjangoProjectScaffolder import DjangoProjectScaffolder
from .GeneratedCodeAnalyzer import GeneratedCodeAnalyzer
from .PytestRunner import PytestRunner
from .SandboxResult import SandboxResult


class DjangoSandbox:
    """
    **Kam skirtas:** Paleidžia sugeneruotą Django kodą izoliuotame laikino projekto sandbox'e.

    **Tikslas:** Viename viešame API sujungti kodo analizę, projekto sugeneravimą ir testų vykdymą.

    **Argumentai:** Konstruktorius priima pasirenkamą timeout'ą sekundėmis.

    **Grąžinama:** `DjangoSandbox` instanciją, galinčią vykdyti `run()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    sandbox = DjangoSandbox(timeout_sec=60)
    result = sandbox.run(task, generated_files)
    ```
    """

    #: **Kam skirtas:** Saugo vieno sandbox vykdymo timeout'ą.
    #: **Tikslas:** Valdyti, kiek laiko galima skirti testų vykdymui.
    timeout_sec: int

    def __init__(self, timeout_sec: int | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja sandbox'ą su timeout reikšme.

        **Tikslas:** Naudoti numatytą projekto timeout'ą arba testuose jį override'inti.

        **Argumentai:** `timeout_sec` leidžia aiškiai nurodyti vykdymo limitą sekundėmis.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        sandbox = DjangoSandbox()
        sandbox = DjangoSandbox(timeout_sec=120)
        ```
        """
        self.timeout_sec = timeout_sec or settings.sandbox_timeout_sec

    def run(
        self,
        task: BenchmarkTask,
        generated_files: dict[str, str],
    ) -> SandboxResult:
        """
        **Kam skirtas:** Paleidžia testus prieš sugeneruotą failų rinkinį.

        **Tikslas:** Paversti `generated_files` ir benchmark užduotį į `SandboxResult`.

        **Argumentai:** `task` pateikia referencinius testus, o `generated_files` pateikia agento sugeneruotą sprendimą.

        **Grąžinama:** `SandboxResult` objektą su testų baigtimi ir logais.

        **Panaudojimo pavyzdžiai:**
        ```python
        result = DjangoSandbox().run(task, {"blog/models.py": "..."})
        ```
        """
        if not generated_files:
            return SandboxResult(
                passed=False,
                returncode=-2,
                stdout="",
                stderr="Nesugeneruotas joks kodas (failai tušti).",
            )

        analyzer = GeneratedCodeAnalyzer(generated_files)

        with tempfile.TemporaryDirectory(prefix="kursinis_sandbox_") as tmp:
            root = Path(tmp)
            DjangoProjectScaffolder(root, analyzer, task).scaffold()
            return PytestRunner(self.timeout_sec).run(root)
