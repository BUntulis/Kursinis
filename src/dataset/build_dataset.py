"""Konvertuoja scrap'intą Django kodą į instruction-tuning JSONL."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterator

from .DocumentedFunctionExtractor import DocumentedFunctionExtractor
from .InstructionDatasetBuilder import InstructionDatasetBuilder
from .PythonSourceWalker import PythonSourceWalker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/django_instructions.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="0 = be limito")
    args = parser.parse_args()

    builder = InstructionDatasetBuilder(
        input_dir=args.input,
        output_path=args.output,
        limit=args.limit,
    )
    try:
        total, written = builder.build()
    except FileNotFoundError as exc:
        print(f"[KLAIDA] {exc}")
        return 1

    print(f"==> Iš viso kandidatų: {total}, įrašyta unikalių: {written}")
    print(f"==> {args.output.resolve()}")
    return 0


_default_extractor = DocumentedFunctionExtractor()


def extract_pairs(path: Path) -> Iterator[dict]:
    return _default_extractor.extract(path)


if __name__ == "__main__":
    raise SystemExit(main())
