"""Generuoja išsamią palyginimo ataskaitą iš `results/*.json`."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.ReportGenerator import ReportGenerator  # noqa: E402


def main() -> None:
    ReportGenerator().generate()


if __name__ == "__main__":
    main()
