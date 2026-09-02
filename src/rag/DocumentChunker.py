"""Dokumentų chunk'inimo klasė."""
from __future__ import annotations


class DocumentChunker:
    """
    **Kam skirtas:** Suskaido dokumento tekstą į persidengiančius fragmentus.

    **Tikslas:** Paruošti tekstą vektoriniam indeksavimui taip, kad paieška neprarastų konteksto per fragmentų ribas.

    **Argumentai:** Konstruktorius priima fragmento dydį ir persidengimą.

    **Grąžinama:** `DocumentChunker` instanciją, galinčią vykdyti `chunk()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    chunker = DocumentChunker(size=800, overlap=100)
    chunks = chunker.chunk("Ilgas tekstas ...")
    ```
    """

    #: **Kam skirtas:** Saugo numatytą fragmento dydį simboliais.
    #: **Tikslas:** Leisti kurti subalansuotus fragmentus be papildomos konfigūracijos.
    DEFAULT_SIZE = 800

    #: **Kam skirtas:** Saugo numatytą fragmentų persidengimą.
    #: **Tikslas:** Išlaikyti semantinį tęstinumą tarp kaimyninių fragmentų.
    DEFAULT_OVERLAP = 100

    #: **Kam skirtas:** Saugo vieno fragmento dydį.
    #: **Tikslas:** Kontroliuoti, kiek simbolių tilps į vieną indeksuojamą fragmentą.
    size: int

    #: **Kam skirtas:** Saugo gretimų fragmentų persidengimą.
    #: **Tikslas:** Užtikrinti, kad sakiniai nebūtų grubiai nukirsti per vidurį.
    overlap: int

    def __init__(self, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP) -> None:
        """
        **Kam skirtas:** Inicializuoja dokumentų chunk'erį su dydžio ir persidengimo parametrais.

        **Tikslas:** Paruošti objektą, kuris su ta pačia strategija skaidys visus dokumentus.

        **Argumentai:** `size` nurodo fragmento ilgį, o `overlap` nurodo persidengimo ilgį simboliais.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        chunker = DocumentChunker(size=1000, overlap=150)
        ```
        """
        if overlap >= size:
            raise ValueError(f"overlap ({overlap}) turi būti mažesnis už size ({size})")
        self.size = size
        self.overlap = overlap

    def chunk(self, text: str) -> list[str]:
        """
        **Kam skirtas:** Suskaido tekstą į persidengiančių fragmentų sąrašą.

        **Tikslas:** Pateikti indeksatoriui sąrašą fragmentų, tinkamų vektorizuoti.

        **Argumentai:** `text` yra pilnas dokumento tekstas.

        **Grąžinama:** `list[str]` su visais sugeneruotais fragmentais.

        **Panaudojimo pavyzdžiai:**
        ```python
        chunks = DocumentChunker().chunk("A" * 2000)
        ```
        """
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + self.size, len(text))
            chunks.append(text[start:end])
            if end == len(text):
                break
            start = end - self.overlap
        return chunks
