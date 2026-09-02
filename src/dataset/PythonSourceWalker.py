"""Python failų vaikštyklė dataset statybai."""
from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, Iterator, Pattern


class PythonSourceWalker:
    """
    **Kam skirtas:** Iteratyviai surenka Python failus iš katalogo, atmesdamas nereikalingus kelius.

    **Tikslas:** Pateikti `InstructionDatasetBuilder` klasei švarų `.py` failų srautą be testų, migracijų ir kitų triukšmo šaltinių.

    **Argumentai:** Konstruktorius priima šakninį katalogą `root`.

    **Grąžinama:** `PythonSourceWalker` instanciją, kuri per `iter_files()` grąžina `Path` objektus.

    **Panaudojimo pavyzdžiai:**
    ```python
    walker = PythonSourceWalker(Path("data/raw"))
    for path in walker.iter_files():
        print(path)
    ```
    """

    #: **Kam skirtas:** Saugo katalogų pavadinimus, kurie turi būti praleidžiami.
    #: **Tikslas:** Neįtraukti testų, migracijų ir kitų dataset'ui netinkančių failų.
    EXCLUDE_DIRS: ClassVar[set[str]] = {
        "tests",
        "test",
        "migrations",
        "__pycache__",
        ".git",
        "docs",
        "node_modules",
        "examples",
        "example",
    }

    #: **Kam skirtas:** Saugo regex'ą failų vardams, kurie turi būti ignoruojami.
    #: **Tikslas:** Atfiltruoti testinius ir pagalbinius failus pagal vardą.
    EXCLUDE_NAME_RE: ClassVar[Pattern[str]] = re.compile(
        r"(test_|_test\.py$|conftest\.py$|setup\.py$)"
    )

    #: **Kam skirtas:** Saugo šakninį katalogą.
    #: **Tikslas:** Leisti instancijai kelis kartus iteruoti tą patį projekto medį.
    root: Path

    def __init__(self, root: Path) -> None:
        """
        **Kam skirtas:** Inicializuoja failų vaikštyklę su šakniniu katalogu.

        **Tikslas:** Nustatyti, nuo kur pradėti `.py` failų paiešką.

        **Argumentai:** `root` yra katalogas, kuriame bus vykdoma rekursinė paieška.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        walker = PythonSourceWalker(Path("project"))
        ```
        """
        self.root = root

    def iter_files(self) -> Iterator[Path]:
        """
        **Kam skirtas:** Grąžina visus tinkamus Python failus iš šakninio katalogo.

        **Tikslas:** Tiekti dataset builder'iui failus po vieną, be poreikio viską krauti į atmintį.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `Iterator[Path]` su visais atrinktais failais.

        **Panaudojimo pavyzdžiai:**
        ```python
        files = list(PythonSourceWalker(Path("src")).iter_files())
        ```
        """
        for path in self.root.rglob("*.py"):
            if any(part in self.EXCLUDE_DIRS for part in path.parts):
                continue
            if self.EXCLUDE_NAME_RE.search(path.name):
                continue
            yield path
