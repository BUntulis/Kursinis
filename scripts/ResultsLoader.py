"""Rezultatų failų kroviklis palyginimui."""
from __future__ import annotations

import json
from pathlib import Path

from config import settings

from scripts.RunData import RunData


class ResultsLoader:
    """
    **Kam skirtas:** Nuskaito rezultatų katalogą ir paverčia JSON failus į `RunData` objektus.

    **Tikslas:** Centralizuoti failų filtravimą, JSON parsinimą ir netinkamų failų praleidimą.

    **Argumentai:** Konstruktorius priima pasirenkamą katalogo kelią.

    **Grąžinama:** `ResultsLoader` instanciją, galinčią vykdyti `load()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    runs = ResultsLoader().load()
    ```
    """

    #: **Kam skirtas:** Saugo rezultatų failų paieškos šabloną.
    #: **Tikslas:** Užtikrinti, kad būtų skaitomi tik JSON failai.
    GLOB = "*.json"

    #: **Kam skirtas:** Saugo ignoruojamų failų vardus.
    #: **Tikslas:** Neįtraukti generuotų ataskaitinių JSON failų.
    EXCLUDED_NAMES = {"report.json"}

    #: **Kam skirtas:** Saugo rezultatų katalogą.
    #: **Tikslas:** Leisti vieną kroviklio instanciją naudoti tam pačiam aplankui.
    directory: Path

    def __init__(self, directory: Path | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja kroviklį su rezultatų katalogu.

        **Tikslas:** Nustatyti, iš kur bus skaitomi eksperimentų JSON failai.

        **Argumentai:** `directory` leidžia override'inti numatytą `settings.results_dir`.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        loader = ResultsLoader(directory=Path("results"))
        ```
        """
        self.directory = directory or settings.results_dir

    def load(self) -> list[RunData]:
        """
        **Kam skirtas:** Užkrauna visus tinkamus rezultatų JSON failus.

        **Tikslas:** Grąžinti paruoštą `RunData` sąrašą lyginimo lentelei.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `list[RunData]` su visais nuskaitytais run'ais.

        **Panaudojimo pavyzdžiai:**
        ```python
        runs = loader.load()
        ```
        """
        runs: list[RunData] = []
        for path in sorted(self.directory.glob(self.GLOB)):
            if path.name in self.EXCLUDED_NAMES:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"[warn] negaliu parse'inti {path}")
                continue
            if "results" in data:
                runs.append(RunData(name=path.stem, raw=data))
        return runs
