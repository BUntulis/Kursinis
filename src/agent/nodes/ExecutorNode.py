"""Sandbox vykdymo mazgas."""
from __future__ import annotations

from src.benchmark import BenchmarkTask
from src.eval.sandbox import DjangoSandbox

from ..AgentState import AgentState
from .AgentNode import AgentNode


class ExecutorNode(AgentNode):
    """
    **Kam skirtas:** Paleidžia sugeneruotus failus sandbox'e ir įrašo rezultatą į būseną.

    **Tikslas:** Susieti agento sugeneruotą kodą su benchmark testų vykdymu.

    **Argumentai:** Konstruktorius priima pasirenkamą `DjangoSandbox` instanciją.

    **Grąžinama:** `ExecutorNode` instanciją, kuri per `run()` užpildo sandbox rezultatų laukus.

    **Panaudojimo pavyzdžiai:**
    ```python
    executor = ExecutorNode()
    updates = executor.run(state)
    ```
    """

    #: **Kam skirtas:** Saugo sandbox vykdymo objektą.
    #: **Tikslas:** Leisti testuose injektuoti alternatyvų sandbox'ą arba mock'ą.
    sandbox: DjangoSandbox

    def __init__(self, sandbox: DjangoSandbox | None = None, sandbox_timeout: int | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja executor mazgą su sandbox priklausomybe.

        **Tikslas:** Paruošti mazgą testų vykdymui per vieną pasirinktą sandbox realizaciją.

        **Argumentai:** `sandbox` leidžia perduoti konkrečią sandbox instanciją; jei ji nepateikta, sukuriama numatytoji su `sandbox_timeout` (sekundėmis; `None` reiškia numatytą `settings.sandbox_timeout_sec`).

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        executor = ExecutorNode(sandbox_timeout=3600)
        ```
        """
        self.sandbox = sandbox or DjangoSandbox(timeout_sec=sandbox_timeout)

    def run(self, state: AgentState) -> dict:
        """
        **Kam skirtas:** Paleidžia benchmark testus prieš sugeneruotą kodą.

        **Tikslas:** Užpildyti būseną struktūruotais sandbox vykdymo rezultatais.

        **Argumentai:** `state` turi turėti užduoties laukus ir `generated_files`.

        **Grąžinama:** `dict` su `sandbox_passed`, `sandbox_stdout`, `sandbox_stderr`, `tests_passed` ir `tests_failed`.

        **Panaudojimo pavyzdžiai:**
        ```python
        updates = ExecutorNode().run(state)
        ```
        """
        task = BenchmarkTask(
            id=state["task_id"],
            title=state["task_title"],
            difficulty="",
            category="",
            description=state["task_description"],
            expected_files=state.get("expected_files", []),
            reference_tests=state["reference_tests"],
        )
        result = self.sandbox.run(task, state.get("generated_files", {}))
        return {
            **state,
            "sandbox_passed": result.passed,
            "sandbox_stdout": result.stdout,
            "sandbox_stderr": result.stderr,
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
        }


def executor_node(state: AgentState) -> dict:
    return ExecutorNode().run(state)
