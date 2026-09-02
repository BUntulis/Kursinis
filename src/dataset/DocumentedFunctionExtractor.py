"""Dokumentuotų funkcijų ir klasių ištraukiklis."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator


class DocumentedFunctionExtractor:
    """
    **Kam skirtas:** Iš vieno Python failo per AST ištraukia dokumentuotas funkcijas ir klases kaip instruction-tuning poras.

    **Tikslas:** Konvertuoti esamą kodą ir jo docstring'us į `instruction` ir `output` pavyzdžius modelio mokymui.

    **Argumentai:** Klasė papildomų konstruktoriaus argumentų nenaudoja, bet remiasi klasės konstantomis filtravimui.

    **Grąžinama:** `DocumentedFunctionExtractor` instanciją, kuri per `extract()` grąžina kandidatų iteratorių.

    **Panaudojimo pavyzdžiai:**
    ```python
    extractor = DocumentedFunctionExtractor()
    pairs = list(extractor.extract(Path("blog/models.py")))
    ```
    """

    #: **Kam skirtas:** Saugo minimalų kodo eilučių skaičių.
    #: **Tikslas:** Atmesti per trumpus pavyzdžius, kurie modeliui duoda mažai signalo.
    MIN_OUTPUT_LINES = 3

    #: **Kam skirtas:** Saugo maksimalų kodo eilučių skaičių.
    #: **Tikslas:** Neleisti vienam pavyzdžiui būti per ilgam ir išpūsti dataset'ą.
    MAX_OUTPUT_LINES = 80

    #: **Kam skirtas:** Saugo minimalų docstring ilgio limitą.
    #: **Tikslas:** Užtikrinti, kad užduoties aprašas būtų pakankamai informatyvus.
    MIN_DOC_CHARS = 20

    #: **Kam skirtas:** Saugo maksimalų docstring ilgio limitą.
    #: **Tikslas:** Atmesti pernelyg ilgus aprašus, kurie blogina dataset kokybę.
    MAX_DOC_CHARS = 1200

    def extract(self, path: Path) -> Iterator[dict]:
        """
        **Kam skirtas:** Ištraukia visus tinkamus instruction pavyzdžius iš vieno failo.

        **Tikslas:** Pereiti per AST medį ir grąžinti tik tuos mazgus, kurie atitinka filtrus.

        **Argumentai:** `path` nurodo Python failą, iš kurio reikia ištraukti pavyzdžius.

        **Grąžinama:** `Iterator[dict]` su `instruction`, `input`, `output` ir `_meta` laukais.

        **Panaudojimo pavyzdžiai:**
        ```python
        for pair in extractor.extract(Path("app/views.py")):
            print(pair["instruction"])
        ```
        """
        source = self._read_source(path)
        if source is None:
            return
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return

        src_lines = source.splitlines()
        for node in ast.walk(tree):
            pair = self._node_to_pair(node, src_lines, path)
            if pair is not None:
                yield pair

    @staticmethod
    def _read_source(path: Path) -> str | None:
        """
        **Kam skirtas:** Saugiai perskaito failo tekstą UTF-8 koduote.

        **Tikslas:** Vienoje vietoje sutvarkyti dekodavimo ir I/O klaidų valdymą.

        **Argumentai:** `path` nurodo skaitytiną failą.

        **Grąžinama:** Failo tekstą arba `None`, jei failo perskaityti nepavyko.

        **Panaudojimo pavyzdžiai:**
        ```python
        source = DocumentedFunctionExtractor._read_source(Path("app.py"))
        ```
        """
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None

    def _node_to_pair(
        self,
        node: ast.AST,
        src_lines: list[str],
        path: Path,
    ) -> dict | None:
        """
        **Kam skirtas:** Paverčia vieną AST mazgą į instruction-tuning porą.

        **Tikslas:** Filtruoti tik tinkamas klases ir funkcijas bei sukonstruoti vienodą išvesties formatą.

        **Argumentai:** `node` yra AST mazgas, `src_lines` yra originalaus failo eilutės, o `path` nurodo šaltinio failą.

        **Grąžinama:** `dict` su pavyzdžio duomenimis arba `None`, jei mazgas netinka.

        **Panaudojimo pavyzdžiai:**
        ```python
        pair = extractor._node_to_pair(node, source.splitlines(), path)
        ```
        """
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return None

        doc = ast.get_docstring(node)
        if doc is None:
            return None
        doc = doc.strip()
        if not (self.MIN_DOC_CHARS <= len(doc) <= self.MAX_DOC_CHARS):
            return None

        end = getattr(node, "end_lineno", None)
        if end is None:
            return None

        code = "\n".join(src_lines[node.lineno - 1: end])
        n_lines = code.count("\n") + 1
        if not (self.MIN_OUTPUT_LINES <= n_lines <= self.MAX_OUTPUT_LINES):
            return None

        kind = "klasę" if isinstance(node, ast.ClassDef) else "funkciją"
        return {
            "instruction": f"Parašyk Django Python {kind} pagal aprašymą:\n{doc}",
            "input": "",
            "output": code,
            "_meta": {
                "source": str(path),
                "name": node.name,
                "kind": type(node).__name__,
            },
        }
