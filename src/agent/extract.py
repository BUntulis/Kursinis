"""Kodo blokų ištraukimo fasadas."""
from __future__ import annotations

from .CodeBlockExtractor import CodeBlockExtractor


_default_extractor = CodeBlockExtractor()


def extract_files(text: str) -> dict[str, str]:
    return _default_extractor.extract(text)
