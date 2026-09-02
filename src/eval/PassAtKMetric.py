"""Pass@k metrikos klasė."""
from __future__ import annotations

import math

from .TaskResult import TaskResult


class PassAtKMetric:
    """
    **Kam skirtas:** Skaičiuoja `pass@k` metriką vienai arba kelioms užduotims.

    **Tikslas:** Pateikti HumanEval stiliaus kokybės metriką kodų generavimo eksperimentams.

    **Argumentai:** Klasė naudojama kaip statinis namespace ir papildomų konstruktoriaus argumentų nenaudoja.

    **Grąžinama:** Per metodus grąžina `float` tipo metrikas.

    **Panaudojimo pavyzdžiai:**
    ```python
    score = PassAtKMetric.for_task(n=10, c=3, k=1)
    overall = PassAtKMetric.aggregate(results, k=1)
    ```
    """

    @staticmethod
    def for_task(n: int, c: int, k: int) -> float:
        """
        **Kam skirtas:** Apskaičiuoja `pass@k` vienai užduočiai.

        **Tikslas:** Įvertinti tikimybę, kad bent vienas iš `k` atsitiktinai pasirinktų bandymų bus sėkmingas.

        **Argumentai:** `n` yra visų bandymų skaičius, `c` yra sėkmingų bandymų skaičius, o `k` yra pasirenkamų bandymų riba.

        **Grąžinama:** `float` reikšmę intervale nuo `0.0` iki `1.0`.

        **Panaudojimo pavyzdžiai:**
        ```python
        pass_at_1 = PassAtKMetric.for_task(5, 2, 1)
        ```
        """
        if n - c < k:
            return 1.0
        return 1.0 - math.comb(n - c, k) / math.comb(n, k)

    @classmethod
    def aggregate(cls, results: list[TaskResult], k: int) -> float:
        """
        **Kam skirtas:** Suskaičiuoja vidutinę `pass@k` reikšmę per visas užduotis.

        **Tikslas:** Gauti vieną bendrą eksperimento kokybės rodiklį.

        **Argumentai:** `results` yra užduočių rezultatų sąrašas, o `k` yra norima `pass@k` riba.

        **Grąžinama:** `float` vidurkį per visas užduotis.

        **Panaudojimo pavyzdžiai:**
        ```python
        average = PassAtKMetric.aggregate(results, k=1)
        ```
        """
        if not results:
            return 0.0
        return sum(cls.for_task(result.attempts, result.successes, k) for result in results) / len(results)
