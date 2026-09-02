"""Sandbox vykdymo rezultato klasė."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SandboxResult:
    """
    **Kam skirtas:** Saugo vienos sandbox testų sesijos rezultatą.

    **Tikslas:** Pateikti struktūruotą būdą perduoti pytest vykdymo informaciją tarp sandbox, agento ir ataskaitų.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `SandboxResult` instancija su testų statusu ir logais.

    **Panaudojimo pavyzdžiai:**
    ```python
    result = SandboxResult(passed=True, returncode=0, stdout="ok", stderr="")
    ```
    """

    #: **Kam skirtas:** Saugo bendrą testų sėkmės statusą.
    #: **Tikslas:** Leisti greitai patikrinti, ar sprendimas praėjo benchmark'ą.
    passed: bool

    #: **Kam skirtas:** Saugo proceso grįžimo kodą.
    #: **Tikslas:** Pateikti žemesnio lygio vykdymo signalą apie pytest baigtį.
    returncode: int

    #: **Kam skirtas:** Saugo `stdout` išvestį.
    #: **Tikslas:** Leisti critic'ui ir diagnostikai naudoti testų pranešimus.
    stdout: str

    #: **Kam skirtas:** Saugo `stderr` išvestį.
    #: **Tikslas:** Leisti matyti klaidas, kurios išėjo per standartinę klaidų srovę.
    stderr: str

    #: **Kam skirtas:** Saugo visų paleistų testų kiekį.
    #: **Tikslas:** Leisti ataskaitoms ir agentui matyti bendrą vykdymo apimtį.
    tests_run: int = 0

    #: **Kam skirtas:** Saugo praėjusių testų skaičių.
    #: **Tikslas:** Leisti tiksliai atvaizduoti sandbox sėkmės statistiką.
    tests_passed: int = 0

    #: **Kam skirtas:** Saugo nepraėjusių testų skaičių.
    #: **Tikslas:** Leisti greitai įvertinti likusių klaidų mastą.
    tests_failed: int = 0

    #: **Kam skirtas:** Saugo testų klaidų skaičių.
    #: **Tikslas:** Atskirti loginį nepraėjimą nuo vykdymo klaidų.
    tests_errors: int = 0
