"""Django dokumentacijos indeksavimo klasė."""
from __future__ import annotations

from pathlib import Path

import chromadb

from .DocumentChunker import DocumentChunker
from .EmbeddingFunctionFactory import EmbeddingFunctionFactory


class DjangoDocsIndexer:
    """
    **Kam skirtas:** Kuria ChromaDB indeksą iš Django dokumentacijos failų.

    **Tikslas:** Pereiti nuo dokumentų katalogo iki užpildytos vektorinės kolekcijos, kurią gali naudoti retriever'is.

    **Argumentai:** Konstruktorius priima šaltinio katalogą, ChromaDB vietą, kolekcijos vardą ir pasirenkamą chunk'erį.

    **Grąžinama:** `DjangoDocsIndexer` instanciją, galinčią vykdyti `build()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    indexer = DjangoDocsIndexer(Path("django-src/docs"), Path(".chroma"))
    indexer.build()
    ```
    """

    #: **Kam skirtas:** Saugo palaikomus dokumentų plėtinius.
    #: **Tikslas:** Užtikrinti, kad būtų indeksuojami tik tekstiniai dokumentacijos failai.
    SUPPORTED_EXTENSIONS = {".txt", ".md", ".rst"}

    #: **Kam skirtas:** Saugo numatytą kolekcijos pavadinimą.
    #: **Tikslas:** Suvienodinti indeksavimo ir paieškos taikinį.
    DEFAULT_COLLECTION = "django_docs"

    #: **Kam skirtas:** Saugo vieno įrašymo paketo dydį.
    #: **Tikslas:** Valdyti atminties naudojimą indeksavimo metu.
    BATCH_SIZE = 500

    #: **Kam skirtas:** Saugo šaltinio katalogą.
    #: **Tikslas:** Nurodyti, iš kur rinkti dokumentacijos failus.
    source: Path

    #: **Kam skirtas:** Saugo ChromaDB katalogą.
    #: **Tikslas:** Nurodyti, kur bus laikomas sugeneruotas indeksas.
    chroma_path: Path

    #: **Kam skirtas:** Saugo kolekcijos vardą.
    #: **Tikslas:** Leisti naudoti atskiras kolekcijas skirtingiems eksperimentams.
    collection_name: str

    #: **Kam skirtas:** Saugo chunk'inimo strategiją.
    #: **Tikslas:** Leisti testuose ir eksperimentuose lengvai pakeisti fragmentavimo logiką.
    chunker: DocumentChunker

    def __init__(
        self,
        source: Path,
        chroma_path: Path,
        collection_name: str = DEFAULT_COLLECTION,
        chunker: DocumentChunker | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja indeksatorių su dokumentų ir indeksų keliais.

        **Tikslas:** Paruošti objektą vienam pilnam dokumentacijos indeksavimo ciklui.

        **Argumentai:** `source` yra dokumentų katalogas, `chroma_path` yra vektorinės bazės vieta, `collection_name` nurodo kolekciją, o `chunker` leidžia override'inti fragmentavimo strategiją.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        indexer = DjangoDocsIndexer(
            source=Path("docs"),
            chroma_path=Path(".chroma"),
            chunker=DocumentChunker(),
        )
        ```
        """
        self.source = source
        self.chroma_path = chroma_path
        self.collection_name = collection_name
        self.chunker = chunker or DocumentChunker()

    def build(self) -> None:
        """
        **Kam skirtas:** Sukuria arba perkuria visą dokumentacijos indeksą.

        **Tikslas:** Nuskaityti dokumentus, juos suskaidyti, suvektorizuoti ir įrašyti į ChromaDB.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        DjangoDocsIndexer(Path("docs"), Path(".chroma")).build()
        ```
        """
        client = chromadb.PersistentClient(path=str(self.chroma_path))
        self._reset_collection(client)
        collection = client.create_collection(
            self.collection_name,
            embedding_function=EmbeddingFunctionFactory.create(),
        )

        files = self._iter_doc_files()
        print(f"Rasta {len(files)} dokumentų {self.source}")

        docs: list[str] = []
        metadatas: list[dict] = []
        ids: list[str] = []

        for file_path in files:
            text = self._read_file(file_path)
            if text is None:
                continue
            for index, chunk in enumerate(self.chunker.chunk(text)):
                docs.append(chunk)
                metadatas.append(
                    {
                        "source": str(file_path.relative_to(self.source)),
                        "chunk": index,
                    }
                )
                ids.append(f"{file_path.stem}_{index}_{len(ids)}")

            if len(docs) >= self.BATCH_SIZE:
                collection.add(documents=docs, metadatas=metadatas, ids=ids)
                docs.clear()
                metadatas.clear()
                ids.clear()

        if docs:
            collection.add(documents=docs, metadatas=metadatas, ids=ids)

        print(f"Indeksuota {collection.count()} chunk'ų į {self.chroma_path}")

    def _reset_collection(self, client) -> None:
        """
        **Kam skirtas:** Ištrina seną kolekciją prieš naują indeksavimą.

        **Tikslas:** Padaryti indeksavimo procesą idempotentišką ir išvengti pasenusių įrašų kaupimosi.

        **Argumentai:** `client` yra ChromaDB klientas, per kurį valdoma kolekcija.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        indexer._reset_collection(client)
        ```
        """
        try:
            client.delete_collection(self.collection_name)
        except Exception:
            pass

    def _iter_doc_files(self) -> list[Path]:
        """
        **Kam skirtas:** Surenka visus palaikomų plėtinių dokumentus.

        **Tikslas:** Vienoje vietoje apibrėžti, kokie failai laikomi indeksuojama dokumentacija.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `list[Path]` su visais dokumentacijos failais.

        **Panaudojimo pavyzdžiai:**
        ```python
        files = indexer._iter_doc_files()
        ```
        """
        return [
            path
            for path in self.source.rglob("*")
            if path.suffix.lower() in self.SUPPORTED_EXTENSIONS and path.is_file()
        ]

    @staticmethod
    def _read_file(path: Path) -> str | None:
        """
        **Kam skirtas:** Saugiai perskaito dokumentacijos failą.

        **Tikslas:** Vienoje vietoje sutvarkyti I/O klaidų valdymą indeksavimo metu.

        **Argumentai:** `path` nurodo skaitomą failą.

        **Grąžinama:** Failo tekstą arba `None`, jei perskaityti nepavyko.

        **Panaudojimo pavyzdžiai:**
        ```python
        text = DjangoDocsIndexer._read_file(Path("intro.txt"))
        ```
        """
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
