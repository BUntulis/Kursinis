"""Ataskaitos užduočių metaduomenų klasė."""
from __future__ import annotations

from dataclasses import dataclass

from src.benchmark import BenchmarkLoader, BenchmarkTask


@dataclass
class TaskMetadata:
    """
    **Kam skirtas:** Saugo benchmark užduočių kategorijų ir sudėtingumų žemėlapius ataskaitų generavimui.

    **Tikslas:** Vienoje vietoje sukaupti visus metaduomenis, kurių reikia rezultatų grupavimui.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `TaskMetadata` instancija su kategorijų, sudėtingumų ir užduočių sąrašu.

    **Panaudojimo pavyzdžiai:**
    ```python
    meta = TaskMetadata.from_loader()
    print(meta.by_category)
    ```
    """

    #: **Kam skirtas:** Saugo žemėlapį `task_id -> category`.
    #: **Tikslas:** Leisti greitai grupuoti rezultatus pagal kategoriją.
    by_category: dict[str, str]

    #: **Kam skirtas:** Saugo žemėlapį `task_id -> difficulty`.
    #: **Tikslas:** Leisti greitai grupuoti rezultatus pagal sudėtingumą.
    by_difficulty: dict[str, str]

    #: **Kam skirtas:** Saugo pilną benchmark užduočių sąrašą.
    #: **Tikslas:** Naudoti jį detalioms ataskaitų lentelėms.
    tasks: list[BenchmarkTask]

    @classmethod
    def from_loader(cls, loader: BenchmarkLoader | None = None) -> "TaskMetadata":
        """
        **Kam skirtas:** Sukuria `TaskMetadata` objektą iš benchmark loader'io.

        **Tikslas:** Automatizuoti metaduomenų surinkimą iš benchmark YAML failų.

        **Argumentai:** `loader` leidžia perduoti alternatyvų benchmark kroviklį.

        **Grąžinama:** `TaskMetadata` instanciją.

        **Panaudojimo pavyzdžiai:**
        ```python
        meta = TaskMetadata.from_loader(BenchmarkLoader())
        ```
        """
        loader = loader or BenchmarkLoader()
        tasks = loader.load_all()
        return cls(
            by_category={task.id: task.category for task in tasks},
            by_difficulty={task.id: task.difficulty for task in tasks},
            tasks=tasks,
        )
