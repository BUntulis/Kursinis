"""Benchmark užduoties rezultato klasė."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskResult:
    """
    **Kam skirtas:** Saugo vienos užduoties agreguotą bandymų rezultatą.

    **Tikslas:** Pateikti tipizuotą įvestį `PassAtKMetric` skaičiavimams.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `TaskResult` instancija su bandymų, sėkmių ir telemetrijos duomenimis.

    **Panaudojimo pavyzdžiai:**
    ```python
    result = TaskResult(task_id="001", attempts=5, successes=3)
    ```
    """

    #: **Kam skirtas:** Saugo užduoties identifikatorių.
    #: **Tikslas:** Leisti metrikoms ir ataskaitoms susieti rezultatą su konkrečia užduotimi.
    task_id: str

    #: **Kam skirtas:** Saugo visų bandymų skaičių.
    #: **Tikslas:** Reikalinga `pass@k` formulei.
    attempts: int

    #: **Kam skirtas:** Saugo sėkmingų bandymų skaičių.
    #: **Tikslas:** Reikalinga `pass@k` tikimybei apskaičiuoti.
    successes: int

    #: **Kam skirtas:** Saugo vidutinę generavimo trukmę.
    #: **Tikslas:** Leisti papildomai analizuoti modelio našumą.
    avg_latency_sec: float = 0.0

    #: **Kam skirtas:** Saugo vidutinį įvesties tokenų kiekį.
    #: **Tikslas:** Leisti vertinti užklausų sąnaudas.
    avg_prompt_tokens: float = 0.0

    #: **Kam skirtas:** Saugo vidutinį išvesties tokenų kiekį.
    #: **Tikslas:** Leisti vertinti generacijos apimtį.
    avg_completion_tokens: float = 0.0
