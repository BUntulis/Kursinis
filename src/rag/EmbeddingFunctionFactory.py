"""Embedding funkcijos fabrikas."""
from __future__ import annotations

from chromadb.utils import embedding_functions


class EmbeddingFunctionFactory:
    """
    **Kam skirtas:** Kuria embedding funkcijas tiek indeksavimui, tiek paieškai.

    **Tikslas:** Užtikrinti, kad `indexer` ir `retriever` naudotų suderintą embedding modelį.

    **Argumentai:** Klasė papildomų konstruktoriaus argumentų nenaudoja ir veikia per klasės metodą `create()`.

    **Grąžinama:** Embedding funkcijos objektą, kurį supranta `chromadb`.

    **Panaudojimo pavyzdžiai:**
    ```python
    embedding_fn = EmbeddingFunctionFactory.create()
    ```
    """

    #: **Kam skirtas:** Saugo numatyto embedding modelio vardą.
    #: **Tikslas:** Naudoti vienodą modelį, jei vartotojas neperduoda savo reikšmės.
    DEFAULT_MODEL = "all-MiniLM-L6-v2"

    @classmethod
    def create(cls, model_name: str | None = None):
        """
        **Kam skirtas:** Sukuria `SentenceTransformer` pagrįstą embedding funkciją.

        **Tikslas:** Pateikti vienodą vektorizavimo mechanizmą visam RAG pipeline'ui.

        **Argumentai:** `model_name` leidžia override'inti numatytą embedding modelį.

        **Grąžinama:** `SentenceTransformerEmbeddingFunction` objektą.

        **Panaudojimo pavyzdžiai:**
        ```python
        fn = EmbeddingFunctionFactory.create("all-MiniLM-L6-v2")
        ```
        """
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_name or cls.DEFAULT_MODEL
        )
