"""Benchmark užduočių YAML saugykla."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from config import settings as project_settings
from src.benchmark.BenchmarkTask import DEFAULT_SCORING_CRITERIA


class TaskStore:
    """
    Kam skirtas:
    Skaityti ir keisti benchmark užduočių YAML failus.

    Tikslas:
    Suteikti vieną API Web sąsajai ir CLI, kad užduotys būtų valdomos iš to paties katalogo kaip esami benchmark'ai.

    Argumentai:
    Konstruktorius priima pasirenkamą užduočių katalogą.

    Grąžinama:
    `TaskStore` instancija.

    Panaudojimo pavyzdžiai:
    ```python
    store = TaskStore()
    tasks = store.list()
    ```
    """

    #: Leidžiami YAML failų vardų simboliai.
    SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    #: Užduočių katalogas.
    directory: Path

    def __init__(self, directory: Path | None = None) -> None:
        """
        Kam skirtas:
        Inicializuoti užduočių saugyklą.

        Tikslas:
        Nustatyti katalogą, kuriame saugomi benchmark YAML failai.

        Argumentai:
        `directory` yra pasirenkamas katalogas; jei nepateiktas, naudojamas projekto nustatymas.

        Grąžinama:
        `None`.

        Panaudojimo pavyzdžiai:
        ```python
        store = TaskStore(Path("benchmark/tasks"))
        ```
        """
        self.directory = directory or project_settings.benchmark_dir

    def list(self) -> list[dict[str, Any]]:
        """
        Kam skirtas:
        Grąžinti visas benchmark užduotis.

        Tikslas:
        Web sąsajai pateikti pilną valdomų užduočių sąrašą.

        Argumentai:
        Metodas argumentų nepriima.

        Grąžinama:
        Sąrašas su YAML duomenimis ir papildomu `_file` lauku.

        Panaudojimo pavyzdžiai:
        ```python
        tasks = store.list()
        ```
        """
        return [self._read(path) for path in sorted(self.directory.glob("*.yaml"))]

    def get(self, task_id: str) -> dict[str, Any]:
        """
        Kam skirtas:
        Grąžinti vienos užduoties YAML duomenis.

        Tikslas:
        Leisti redagavimo formai užkrauti konkretų benchmark.

        Argumentai:
        `task_id` yra YAML failo stem arba užduoties ID.

        Grąžinama:
        Užduoties duomenų žodynas.

        Panaudojimo pavyzdžiai:
        ```python
        task = store.get("001_blog_model")
        ```
        """
        return self._read(self._path_for_existing(task_id))

    def save(self, data: dict[str, Any], original_id: str | None = None) -> dict[str, Any]:
        """
        Kam skirtas:
        Sukurti arba atnaujinti benchmark užduotį.

        Tikslas:
        Įrašyti Web formoje pakeistus duomenis atgal į YAML failą.

        Argumentai:
        `data` yra užduoties duomenys, o `original_id` nurodo keičiamą seną failą.

        Grąžinama:
        Įrašytos užduoties duomenys.

        Panaudojimo pavyzdžiai:
        ```python
        saved = store.save({"id": "013_new", ...})
        ```
        """
        task_id = str(data.get("id", "")).strip()
        if not task_id or not self.SAFE_ID_RE.match(task_id):
            raise ValueError("Užduoties ID gali turėti tik raides, skaičius, '_' ir '-'.")

        payload = {
            "id": task_id,
            "title": data.get("title", ""),
            "difficulty": data.get("difficulty", "unknown"),
            "category": data.get("category", "general"),
            "description": data.get("description", ""),
            "expected_files": list(data.get("expected_files", [])),
            "reference_tests": data.get("reference_tests", ""),
            "expected_implementation_summary": data.get("expected_implementation_summary", ""),
            "unit_test_plan": list(data.get("unit_test_plan") or []),
            "scoring_criteria": dict(data.get("scoring_criteria") or DEFAULT_SCORING_CRITERIA),
        }

        if original_id and original_id != task_id:
            old_path = self._path_for_existing(original_id)
            if old_path.exists():
                old_path.unlink()

        path = self.directory / f"{task_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return self._read(path)

    def delete(self, task_id: str) -> None:
        """
        Kam skirtas:
        Pašalinti benchmark užduoties YAML failą.

        Tikslas:
        Leisti Web ir CLI valdyti užduočių rinkinį.

        Argumentai:
        `task_id` nurodo šalinamą užduotį.

        Grąžinama:
        `None`.

        Panaudojimo pavyzdžiai:
        ```python
        store.delete("013_new")
        ```
        """
        self._path_for_existing(task_id).unlink()

    def _path_for_existing(self, task_id: str) -> Path:
        """
        Kam skirtas:
        Rasti YAML failo kelią pagal užduoties ID.

        Tikslas:
        Palaikyti ir pilną failo stem, ir trumpą pabaigos ID.

        Argumentai:
        `task_id` yra ieškomos užduoties identifikatorius.

        Grąžinama:
        `Path` iki YAML failo.

        Panaudojimo pavyzdžiai:
        ```python
        path = store._path_for_existing("blog_model")
        ```
        """
        for path in self.directory.glob("*.yaml"):
            if path.stem == task_id or path.stem.endswith(task_id):
                return path
        raise FileNotFoundError(f"Užduotis '{task_id}' nerasta.")

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        """Perskaito vieną YAML failą ir prideda failo vardą."""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("expected_implementation_summary", "")
        data.setdefault("unit_test_plan", [])
        data.setdefault("scoring_criteria", dict(DEFAULT_SCORING_CRITERIA))
        data["_file"] = path.name
        return data
