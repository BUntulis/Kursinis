"""Benchmark rezultatų JSON saugykla."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import settings as project_settings


class ResultsStore:
    """
    Kam skirtas:
    Skaityti benchmark rezultatų JSON ir sugeneruotų ataskaitų failus.

    Tikslas:
    Web sąsajai pateikti eksperimento suvestines be papildomo CLI paleidimo.

    Argumentai:
    Konstruktorius priima pasirenkamą rezultatų katalogą.

    Grąžinama:
    `ResultsStore` instancija.

    Panaudojimo pavyzdžiai:
    ```python
    results = ResultsStore().list_runs()
    ```
    """

    #: Rezultatų katalogas.
    directory: Path

    def __init__(self, directory: Path | None = None) -> None:
        """Inicializuoja rezultatų saugyklą."""
        self.directory = directory or project_settings.results_dir

    def list_runs(self) -> list[dict[str, Any]]:
        """
        Kam skirtas:
        Grąžinti visų rezultatų JSON suvestines.

        Tikslas:
        Dashboard'e parodyti benchmark rezultatus ir pass rodiklius.

        Argumentai:
        Metodas argumentų nepriima.

        Grąžinama:
        Sąrašas su failų vardais, bendromis metrikomis ir rezultatais.

        Panaudojimo pavyzdžiai:
        ```python
        runs = ResultsStore().list_runs()
        ```
        """
        runs: list[dict[str, Any]] = []
        if not self.directory.exists():
            return runs
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            results = data.get("results", [])
            total = len(results)
            passed = sum(1 for item in results if item.get("passed"))
            runs.append(
                {
                    "name": path.stem,
                    "file": path.name,
                    "backend": data.get("backend", path.stem),
                    "total": data.get("total", total),
                    "passed": data.get("passed", passed),
                    "pass_rate": (passed / total) if total else 0,
                    "results": results,
                }
            )
        return runs

    def reports(self) -> dict[str, str]:
        """
        Kam skirtas:
        Grąžinti sugeneruotų report failų turinį.

        Tikslas:
        Leisti Web sąsajoje peržiūrėti `report.md` ir žinoti CSV vietą.

        Argumentai:
        Metodas argumentų nepriima.

        Grąžinama:
        Žodynas su markdown tekstu ir CSV kelio informacija.

        Panaudojimo pavyzdžiai:
        ```python
        report = ResultsStore().reports()
        ```
        """
        md_path = self.directory / "report.md"
        csv_path = self.directory / "report.csv"
        return {
            "markdown": md_path.read_text(encoding="utf-8") if md_path.exists() else "",
            "csv_path": str(csv_path) if csv_path.exists() else "",
        }
