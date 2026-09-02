"""Ataskaitos run duomenų kroviklis."""
from __future__ import annotations

import json
from pathlib import Path

from config import settings


class _RunsLoader:
    """
    **Kam skirtas:** Nuskaito rezultatų JSON failus ataskaitos generatoriui.

    **Tikslas:** Centralizuoti rezultatų failų paiešką ir paruošti paprastą `dict[run_name, data]` struktūrą.

    **Argumentai:** Konstruktorius priima pasirenkamą katalogo kelią.

    **Grąžinama:** `_RunsLoader` instanciją, galinčią vykdyti `load()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    runs = _RunsLoader().load()
    ```
    """

    #: **Kam skirtas:** Saugo ignoruojamų JSON failų vardus.
    #: **Tikslas:** Neįtraukti ataskaitinių failų į pačią ataskaitą.
    EXCLUDED = {"report.json"}

    #: **Kam skirtas:** Saugo rezultatų katalogą.
    #: **Tikslas:** Nustatyti, iš kur reikia krauti run duomenis.
    directory: Path

    def __init__(self, directory: Path | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja run kroviklį su rezultatų katalogu.

        **Tikslas:** Paruošti objektą rezultatų JSON failų nuskaitymui.

        **Argumentai:** `directory` leidžia override'inti numatytą `settings.results_dir`.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        loader = _RunsLoader(directory=Path("results"))
        ```
        """
        self.directory = directory or settings.results_dir

    def load(self) -> dict[str, dict]:
        """
        **Kam skirtas:** Užkrauna rezultatų JSON failus į vieną žemėlapį.

        **Tikslas:** Pateikti `ReportGenerator` klasei paprastą run'ų duomenų struktūrą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `dict[str, dict]`, kuriame raktas yra run vardas, o reikšmė yra pilnas JSON turinys.

        **Panaudojimo pavyzdžiai:**
        ```python
        runs = loader.load()
        ```
        """
        runs: dict[str, dict] = {}
        for path in sorted(self.directory.glob("*.json")):
            if path.name in self.EXCLUDED:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if "results" in data:
                runs[path.stem] = data
        return runs
