"""CSV ataskaitos rašymo klasė."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import ClassVar

from scripts.TaskMetadata import TaskMetadata


class CSVReportWriter:
    """
    **Kam skirtas:** Sugeneruoja CSV ataskaitą iš benchmark rezultatų.

    **Tikslas:** Pateikti vieną eilutę kiekvienai `(run, task)` porai tolimesnei analizei su Excel ar `pandas`.

    **Argumentai:** Klasė papildomų konstruktoriaus argumentų nenaudoja.

    **Grąžinama:** `CSVReportWriter` instanciją, galinčią vykdyti `write()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    CSVReportWriter().write(Path("results/report.csv"), runs, meta)
    ```
    """

    #: **Kam skirtas:** Saugo CSV antraščių sąrašą.
    #: **Tikslas:** Užtikrinti stabilų stulpelių išdėstymą visose sugeneruotose ataskaitose.
    HEADERS: ClassVar[list[str]] = [
        "run",
        "task_id",
        "category",
        "difficulty",
        "passed",
        "tests_passed",
        "tests_failed",
        "iterations",
        "prompt_tokens",
        "completion_tokens",
        "wall_time_sec",
    ]

    def write(self, path: Path, runs: dict[str, dict], meta: TaskMetadata) -> None:
        """
        **Kam skirtas:** Įrašo CSV ataskaitą į diską.

        **Tikslas:** Paversti visų run'ų rezultatus plokščia lentele analizės įrankiams.

        **Argumentai:** `path` nurodo CSV failą, `runs` yra rezultatų žemėlapis, o `meta` pateikia kategorijų ir sudėtingumų informaciją.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        writer.write(Path("results/report.csv"), runs, meta)
        ```
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.HEADERS)
            for run_name, data in runs.items():
                for result in data.get("results", []):
                    writer.writerow(self._row(run_name, result, meta))

    @staticmethod
    def _row(run_name: str, result: dict, meta: TaskMetadata) -> list:
        """
        **Kam skirtas:** Paverčia vieną rezultatą į CSV eilutės masyvą.

        **Tikslas:** Vienoje vietoje apibrėžti laukų transformaciją iš JSON į CSV.

        **Argumentai:** `run_name` identifikuoja eksperimentą, `result` yra vieno task'o rezultatas, o `meta` pateikia kategorijų ir sudėtingumų lookup'us.

        **Grąžinama:** `list` su CSV eilutės reikšmėmis.

        **Panaudojimo pavyzdžiai:**
        ```python
        row = CSVReportWriter._row("baseline", result, meta)
        ```
        """
        task_id = result.get("task_id", "")
        return [
            run_name,
            task_id,
            meta.by_category.get(task_id, ""),
            meta.by_difficulty.get(task_id, ""),
            int(bool(result.get("passed"))),
            result.get("tests_passed", 0),
            result.get("tests_failed", 0),
            result.get("iterations", 0),
            result.get("prompt_tokens", 0),
            result.get("completion_tokens", 0),
            round(result.get("wall_time_sec") or result.get("generation_time_sec", 0), 2),
        ]
