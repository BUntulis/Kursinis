"""Benchmark užduočių kroviklis."""
from __future__ import annotations

from pathlib import Path

from config import settings

from .BenchmarkTask import BenchmarkTask


class BenchmarkLoader:
    """
    **Kam skirtas:** Skenuoja benchmark užduočių katalogą ir grąžina tipizuotas užduotis.

    **Tikslas:** Centralizuoti YAML failų paiešką ir konvertavimą į `BenchmarkTask` objektus.

    **Argumentai:** Konstruktorius priima pasirenkamą katalogo kelią.

    **Grąžinama:** `BenchmarkLoader` instanciją, galinčią krauti visas arba vieną užduotį.

    **Panaudojimo pavyzdžiai:**
    ```python
    loader = BenchmarkLoader()
    tasks = loader.load_all()
    task = loader.load("001_blog_model")
    ```
    """

    #: **Kam skirtas:** Saugo YAML failų paieškos šabloną.
    #: **Tikslas:** Leisti vienoje vietoje valdyti, kurie failai laikomi benchmark užduotimis.
    YAML_GLOB = "*.yaml"

    #: **Kam skirtas:** Saugo katalogą, iš kurio kraunamos užduotys.
    #: **Tikslas:** Leisti pakartotinai naudoti tą pačią loader'io instanciją su tuo pačiu šaltiniu.
    directory: Path

    def __init__(self, directory: Path | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja užduočių kroviklį su katalogu.

        **Tikslas:** Nustatyti, iš kokios vietos bus kraunami benchmark YAML failai.

        **Argumentai:** `directory` leidžia override'inti numatytą kelią iš projekto nustatymų.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        loader = BenchmarkLoader(directory=Path("tmp/tasks"))
        ```
        """
        self.directory = directory or settings.benchmark_dir

    def load_all(self) -> list[BenchmarkTask]:
        """
        **Kam skirtas:** Užkrauna visas kataloge rastas benchmark užduotis.

        **Tikslas:** Grąžinti deterministiškai surikiuotą užduočių sąrašą eksperimentų paleidimui.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `list[BenchmarkTask]` sąrašą, surikiuotą pagal `task.id`.

        **Panaudojimo pavyzdžiai:**
        ```python
        tasks = BenchmarkLoader().load_all()
        ```
        """
        return sorted(
            (BenchmarkTask.from_yaml(path) for path in self.directory.glob(self.YAML_GLOB)),
            key=lambda task: task.id,
        )

    def load(self, task_id: str) -> BenchmarkTask:
        """
        **Kam skirtas:** Užkrauna vieną benchmark užduotį pagal identifikatorių.

        **Tikslas:** Leisti CLI ir testams pasirinkti tik vieną konkretų benchmark failą.

        **Argumentai:** `task_id` gali būti pilnas YAML stem'as arba trumpa pabaiga, kuri sutampa su stem'u.

        **Grąžinama:** `BenchmarkTask` objektą, atitinkantį nurodytą ID.

        **Panaudojimo pavyzdžiai:**
        ```python
        task = BenchmarkLoader().load("blog_model")
        ```
        """
        for path in self.directory.glob(self.YAML_GLOB):
            if path.stem == task_id or path.stem.endswith(task_id):
                return BenchmarkTask.from_yaml(path)
        raise FileNotFoundError(f"Užduotis '{task_id}' nerasta {self.directory}")
