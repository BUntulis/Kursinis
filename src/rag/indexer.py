"""Django docs indeksavimo CLI."""
from __future__ import annotations

import argparse
from pathlib import Path

from .DocumentChunker import DocumentChunker
from .DjangoDocsIndexer import DjangoDocsIndexer


def main() -> None:
    parser = argparse.ArgumentParser(description="Django docs RAG indekserio CLI")
    parser.add_argument("--source", type=Path, required=True, help="Django docs root folder")
    parser.add_argument("--output", type=Path, default=Path(".chroma"), help="ChromaDB path")
    args = parser.parse_args()

    DjangoDocsIndexer(
        source=args.source,
        chroma_path=args.output,
        chunker=DocumentChunker(),
    ).build()


if __name__ == "__main__":
    main()
