"""Palyginimo scenarijaus run duomenų klasė."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunData:
    """
    **Kam skirtas:** Saugo vieno rezultatų JSON failo duomenis patogesniu tipu.

    **Tikslas:** Leisti lentelių generatoriams ir lygintuvams dirbti su aiškia `name` ir `raw` struktūra.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `RunData` instancija su failo vardu ir pilnu JSON turiniu.

    **Panaudojimo pavyzdžiai:**
    ```python
    run = RunData(name="baseline_ollama", raw={"results": []})
    print(run.results)
    ```
    """

    #: **Kam skirtas:** Saugo eksperimento vardą.
    #: **Tikslas:** Atvaizduoti rezultatą lentelėse ir ataskaitose.
    name: str

    #: **Kam skirtas:** Saugo pilną JSON turinį.
    #: **Tikslas:** Leisti iš jo ištraukti papildomus laukus, jei jų prireiktų.
    raw: dict

    @property
    def results(self) -> list[dict]:
        """
        **Kam skirtas:** Grąžina vieno run'o rezultatų sąrašą.

        **Tikslas:** Sutrumpinti prieigą prie `raw["results"]` ir turėti saugų fallback'ą.

        **Argumentai:** Savybė papildomų argumentų nepriima.

        **Grąžinama:** `list[dict]` su užduočių rezultatais.

        **Panaudojimo pavyzdžiai:**
        ```python
        for item in run.results:
            print(item["task_id"])
        ```
        """
        return self.raw.get("results", [])
