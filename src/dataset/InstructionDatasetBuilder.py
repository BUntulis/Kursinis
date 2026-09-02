"""Instruction dataset kūrimo klasė."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .DocumentedFunctionExtractor import DocumentedFunctionExtractor
from .PythonSourceWalker import PythonSourceWalker


class InstructionDatasetBuilder:
    """
    **Kam skirtas:** Orkestruoja `.py` failų surinkimą, funkcijų ištraukimą ir JSONL dataset'o įrašymą.

    **Tikslas:** Vienoje vietoje sujungti failų skenavimą, AST analizę ir deduplikaciją.

    **Argumentai:** Konstruktorius priima įvesties katalogą, išvesties failą, ribą ir pasirenkamas priklausomybes.

    **Grąžinama:** `InstructionDatasetBuilder` instanciją, kuri per `build()` sukuria dataset'ą.

    **Panaudojimo pavyzdžiai:**
    ```python
    builder = InstructionDatasetBuilder(Path("data/raw"), Path("data/out.jsonl"))
    total, written = builder.build()
    ```
    """

    #: **Kam skirtas:** Saugo įvesties katalogą.
    #: **Tikslas:** Nurodyti, iš kur rinkti šaltinio failus dataset'ui.
    input_dir: Path

    #: **Kam skirtas:** Saugo išvesties JSONL failą.
    #: **Tikslas:** Nurodyti, kur įrašyti galutinį instruction dataset'ą.
    output_path: Path

    #: **Kam skirtas:** Saugo maksimalų įrašų limitą.
    #: **Tikslas:** Leisti greitai pasigaminti mažesnį dataset'ą bandymams.
    limit: int

    #: **Kam skirtas:** Saugo failų vaikštyklę.
    #: **Tikslas:** Leisti testuose ir eksperimentuose pakeisti failų atrankos strategiją.
    walker: PythonSourceWalker

    #: **Kam skirtas:** Saugo AST extractor'į.
    #: **Tikslas:** Leisti pakeisti pavyzdžių ištraukimo logiką nekeičiat builder'io.
    extractor: DocumentedFunctionExtractor

    def __init__(
        self,
        input_dir: Path,
        output_path: Path,
        limit: int = 0,
        walker: PythonSourceWalker | None = None,
        extractor: DocumentedFunctionExtractor | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja dataset builder'į su visomis priklausomybėmis.

        **Tikslas:** Paruošti objektą, galintį atlikti visą dataset kūrimo procesą.

        **Argumentai:** `input_dir` yra šaltinio katalogas, `output_path` yra JSONL failas, `limit` riboja įrašų kiekį, `walker` ir `extractor` leidžia įšvirkšti alternatyvias realizacijas.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        builder = InstructionDatasetBuilder(
            input_dir=Path("data/raw"),
            output_path=Path("data/django_instructions.jsonl"),
            limit=100,
        )
        ```
        """
        self.input_dir = input_dir
        self.output_path = output_path
        self.limit = limit
        self.walker = walker or PythonSourceWalker(input_dir)
        self.extractor = extractor or DocumentedFunctionExtractor()

    def build(self) -> tuple[int, int]:
        """
        **Kam skirtas:** Sukuria galutinį JSONL dataset failą.

        **Tikslas:** Pereiti per visus failus, ištraukti pavyzdžius, pašalinti dublikatus ir įrašyti rezultatą į diską.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `tuple[int, int]`, kur pirmas skaičius yra visi kandidatai, o antras yra įrašyti unikalūs pavyzdžiai.

        **Panaudojimo pavyzdžiai:**
        ```python
        total, written = builder.build()
        ```
        """
        if not self.input_dir.exists():
            raise FileNotFoundError(
                f"{self.input_dir} neegzistuoja. Pirmiau paleiskite scraper'į."
            )

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        total = written = 0

        with self.output_path.open("w", encoding="utf-8") as out:
            for py_file in self.walker.iter_files():
                for pair in self.extractor.extract(py_file):
                    total += 1
                    digest = hashlib.md5(pair["output"].encode()).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    out.write(json.dumps(pair, ensure_ascii=False) + "\n")
                    written += 1
                    if self.limit and written >= self.limit:
                        return total, written
        return total, written
