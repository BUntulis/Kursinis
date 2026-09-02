"""Rezultatų palyginimo lentelės klasė."""
from __future__ import annotations

from typing import ClassVar

from rich.table import Table

from scripts.RunData import RunData


class ComparisonTable:
    """
    **Kam skirtas:** Konstruoja `rich.Table` objektą iš eksperimentų rezultatų.

    **Tikslas:** Gražiai atvaizduoti pagrindines benchmark metrikas terminale.

    **Argumentai:** Klasė papildomų konstruktoriaus argumentų nenaudoja.

    **Grąžinama:** `ComparisonTable` instanciją, galinčią vykdyti `build()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    table = ComparisonTable().build(runs)
    ```
    """

    #: **Kam skirtas:** Saugo lentelės antraštę.
    #: **Tikslas:** Vienodai įvardyti palyginimo vaizdą CLI išvestyje.
    TITLE: ClassVar[str] = "Modelių palyginimas"

    def build(self, runs: list[RunData]) -> Table:
        """
        **Kam skirtas:** Sukuria `rich.Table` iš eksperimento run'ų.

        **Tikslas:** Suagreguoti pagrindines metrikas į terminalui pritaikytą lentelę.

        **Argumentai:** `runs` yra eksperimentų sąrašas su jau užkrautais JSON duomenimis.

        **Grąžinama:** `Table` objektą.

        **Panaudojimo pavyzdžiai:**
        ```python
        table = ComparisonTable().build(runs)
        ```
        """
        table = Table(title=self.TITLE)
        table.add_column("Setup", style="cyan")
        table.add_column("Pass@1", style="green")
        table.add_column("Užduotys", justify="right")
        table.add_column("Vid. laikas (s)", justify="right")
        table.add_column("Vid. tokens (in/out)", justify="right")

        for run in runs:
            self._add_row(table, run)
        return table

    @staticmethod
    def _add_row(table: Table, run: RunData) -> None:
        """
        **Kam skirtas:** Prideda vieno eksperimentinio run'o eilutę į lentelę.

        **Tikslas:** Apskaičiuoti agreguotas metrikas ir įrašyti jas į `rich.Table`.

        **Argumentai:** `table` yra pildoma lentelė, o `run` yra vieno eksperimento duomenys.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        ComparisonTable._add_row(table, run)
        ```
        """
        results = run.results
        total = len(results)
        if total == 0:
            return
        passed = sum(1 for item in results if item.get("passed"))
        avg_time = sum(item.get("generation_time_sec", 0) for item in results) / total
        avg_in = sum(item.get("prompt_tokens", 0) for item in results) / total
        avg_out = sum(item.get("completion_tokens", 0) for item in results) / total
        table.add_row(
            run.name,
            f"{passed / total:.1%}",
            f"{passed}/{total}",
            f"{avg_time:.1f}",
            f"{avg_in:.0f} / {avg_out:.0f}",
        )
