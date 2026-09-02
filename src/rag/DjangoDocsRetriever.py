"""Django dokumentacijos retriever'is."""
from __future__ import annotations

import chromadb

from config import settings

from .EmbeddingFunctionFactory import EmbeddingFunctionFactory
from .RetrievedChunk import RetrievedChunk


class DjangoDocsRetriever:
    """
    **Kam skirtas:** Atlieka top-k paiešką Django dokumentacijos ChromaDB kolekcijoje.

    **Tikslas:** Pateikti agentui aktualų kontekstą pagal natūralios kalbos užklausą.

    **Argumentai:** Konstruktorius priima kolekcijos pavadinimą ir pasirenkamą ChromaDB kelią.

    **Grąžinama:** `DjangoDocsRetriever` instanciją, galinčią vykdyti `retrieve()` ir `format_context()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    retriever = DjangoDocsRetriever()
    chunks = retriever.retrieve("Django model Meta options")
    ```
    """

    #: **Kam skirtas:** Saugo numatytą kolekcijos pavadinimą.
    #: **Tikslas:** Užtikrinti vienodą indeksavimo ir paieškos taikinį.
    DEFAULT_COLLECTION = "django_docs"

    #: **Kam skirtas:** Saugo numatytą grąžinamų rezultatų kiekį.
    #: **Tikslas:** Pateikti stabilų top-k retrieval kiekį be papildomo perdavimo.
    DEFAULT_K = 5

    #: **Kam skirtas:** Saugo ChromaDB klientą.
    #: **Tikslas:** Leisti pakartotinai naudoti tą pačią vektorinės bazės jungtį.
    client: chromadb.PersistentClient

    #: **Kam skirtas:** Saugo pasirinktą dokumentacijos kolekciją.
    #: **Tikslas:** Leisti vykdyti paiešką konkrečiame vektorinės bazės rinkinyje.
    collection: object

    def __init__(
        self,
        collection_name: str = DEFAULT_COLLECTION,
        chroma_path: str | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja retriever'į su ChromaDB klientu ir kolekcija.

        **Tikslas:** Paruošti objektą pakartotinėms paieškos užklausoms.

        **Argumentai:** `collection_name` nurodo kolekciją, o `chroma_path` leidžia override'inti duomenų bazės vietą.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        retriever = DjangoDocsRetriever(chroma_path=".chroma")
        ```
        """
        self.client = chromadb.PersistentClient(
            path=chroma_path or str(settings.chroma_path)
        )
        self.collection = self.client.get_collection(
            collection_name,
            embedding_function=EmbeddingFunctionFactory.create(),
        )

    def retrieve(self, query: str, k: int = DEFAULT_K) -> list[RetrievedChunk]:
        """
        **Kam skirtas:** Suranda artimiausius dokumentacijos fragmentus pagal užklausą.

        **Tikslas:** Grąžinti struktūruotus retrieval rezultatus agento prompt'ui.

        **Argumentai:** `query` yra natūralios kalbos užklausa, o `k` nurodo, kiek rezultatų reikia.

        **Grąžinama:** `list[RetrievedChunk]` su top-k artimiausiais fragmentais.

        **Panaudojimo pavyzdžiai:**
        ```python
        chunks = retriever.retrieve("Django middleware", k=3)
        ```
        """
        result = self.collection.query(query_texts=[query], n_results=k)
        chunks: list[RetrievedChunk] = []
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            chunks.append(
                RetrievedChunk(
                    text=doc,
                    source=str(meta.get("source", "")),
                    score=1.0 - float(dist),
                )
            )
        return chunks

    @staticmethod
    def format_context(chunks: list[RetrievedChunk]) -> str:
        """
        **Kam skirtas:** Sujungia retrieval rezultatus į prompt'ui patogų tekstą.

        **Tikslas:** Pateikti koderio mazgui nuosekliai sunumeruotus dokumentacijos šaltinius.

        **Argumentai:** `chunks` yra retrieval rezultatai, gauti iš `retrieve()`.

        **Grąžinama:** `str` su sujungtais dokumentacijos fragmentais.

        **Panaudojimo pavyzdžiai:**
        ```python
        context = DjangoDocsRetriever.format_context(chunks)
        ```
        """
        return "\n\n---\n\n".join(
            f"[Šaltinis {index}: {chunk.source}]\n{chunk.text}"
            for index, chunk in enumerate(chunks, 1)
        )
