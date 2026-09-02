"""Rezultatų palyginimo fasado klasė."""
from __future__ import annotations

from rich.console import Console

from scripts.ComparisonTable import ComparisonTable
from scripts.ResultsLoader import ResultsLoader


class ResultsComparator:
    """
    **Kam skirtas:** Apjungia rezultatų nuskaitymą, lentelės generavimą ir atvaizdavimą.

    **Tikslas:** Pateikti vieną aukšto lygio API terminaliniam rezultatų palyginimui.

    **Argumentai:** Konstruktorius priima pasirenkamą loader'į, lentelės generatorių ir `Console`.

    **Grąžinama:** `ResultsComparator` instanciją, galinčią vykdyti `render()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    ResultsComparator().render()
    ```
    """

    #: **Kam skirtas:** Saugo rezultatų kroviklį.
    #: **Tikslas:** Leisti testuose arba kitose integracijose pakeisti duomenų šaltinį.
    loader: ResultsLoader

    #: **Kam skirtas:** Saugo lentelės generatorių.
    #: **Tikslas:** Leisti pakeisti atvaizdavimo strategiją nekeičiat fasado logikos.
    table_builder: ComparisonTable

    #: **Kam skirtas:** Saugo `rich` konsolės objektą.
    #: **Tikslas:** Leisti centralizuotai spausdinti lentelę į terminalą.
    console: Console

    def __init__(
        self,
        loader: ResultsLoader | None = None,
        table_builder: ComparisonTable | None = None,
        console: Console | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja rezultatų palyginimo fasadą.

        **Tikslas:** Paruošti visas priklausomybes rezultatų lentelės atvaizdavimui.

        **Argumentai:** `loader`, `table_builder` ir `console` leidžia įšvirkšti alternatyvias realizacijas testams ar kitam pateikimui.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        comparator = ResultsComparator()
        ```
        """
        self.loader = loader or ResultsLoader()
        self.table_builder = table_builder or ComparisonTable()
        self.console = console or Console()

    def render(self) -> None:
        """
        **Kam skirtas:** Atvaizduoja galutinę rezultatų lentelę terminale.

        **Tikslas:** Nuskaityti run'us, paversti juos lentele ir išspausdinti rezultatą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        ResultsComparator().render()
        ```
        """
        runs = self.loader.load()
        if not runs:
            print("Nerasta rezultatų. Paleisk run_baseline.py / run_agent.py")
            return
        table = self.table_builder.build(runs)
        self.console.print(table)
