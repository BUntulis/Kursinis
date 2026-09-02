"""RAG paieškos fragmento klasė."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetrievedChunk:
    """
    **Kam skirtas:** Saugo vieną retrieval rezultato fragmentą su jo šaltiniu ir panašumo balu.

    **Tikslas:** Pateikti tipizuotą struktūrą tarp ChromaDB rezultatų ir agento prompt'o formatavimo.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių.

    **Grąžinama:** `RetrievedChunk` instancija su tekstu, šaltiniu ir balu.

    **Panaudojimo pavyzdžiai:**
    ```python
    chunk = RetrievedChunk(text="QuerySet docs", source="ref/models/querysets.txt", score=0.92)
    ```
    """

    #: **Kam skirtas:** Saugo dokumentacijos fragmento tekstą.
    #: **Tikslas:** Perduoti retrieval turinį į prompt'ą ar ataskaitą.
    text: str

    #: **Kam skirtas:** Saugo fragmento šaltinio kelią.
    #: **Tikslas:** Leisti nurodyti, iš kur buvo paimtas konkretus kontekstas.
    source: str

    #: **Kam skirtas:** Saugo artimumo balą.
    #: **Tikslas:** Leisti įvertinti retrieval rezultato kokybę.
    score: float
