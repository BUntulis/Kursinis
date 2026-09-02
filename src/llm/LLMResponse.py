"""LLM atsakymo duomenų klasė."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    """
    **Kam skirtas:** Saugo vieningo formato atsakymą iš bet kurio LLM backend'o.

    **Tikslas:** Standartizuoti tekstą, tokenų skaitiklius ir latenciją taip, kad agento mazgai nepriklausytų nuo konkretaus tiekėjo API.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `LLMResponse` instancija su sugeneruotu tekstu ir telemetrija.

    **Panaudojimo pavyzdžiai:**
    ```python
    response = LLMResponse(text="Sveikas", prompt_tokens=10, completion_tokens=5)
    print(response.text)
    ```
    """

    #: **Kam skirtas:** Saugo modelio sugeneruotą tekstą.
    #: **Tikslas:** Pateikti vieningą būdą gauti LLM atsakymo turinį.
    text: str

    #: **Kam skirtas:** Saugo įvesties tokenų skaičių.
    #: **Tikslas:** Leisti rinkti sąnaudų ir našumo telemetriją.
    prompt_tokens: int = 0

    #: **Kam skirtas:** Saugo sugeneruotų tokenų skaičių.
    #: **Tikslas:** Leisti vertinti modelio išvesties apimtį ir kaštus.
    completion_tokens: int = 0

    #: **Kam skirtas:** Saugo vienos užklausos trukmę sekundėmis.
    #: **Tikslas:** Leisti skaičiuoti vėlinimo metrikas benchmark'uose.
    latency_sec: float = 0.0

    #: **Kam skirtas:** Saugo faktiškai naudoto modelio vardą.
    #: **Tikslas:** Rezultatų suvestinėse tiksliai parodyti, koks modelis atsakė.
    model: str = ""

    #: **Kam skirtas:** Saugo modelio prašomus įrankių iškvietimus (native tool calling).
    #: **Tikslas:** Leisti tool-using agentui vykdyti read/write/execute įrankius.
    #: Kiekvienas elementas: ``{"name": str, "arguments": dict}``.
    tool_calls: list = field(default_factory=list)
