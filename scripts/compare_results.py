"""Suformuoja palyginimo lentelę iš `results/*.json` failų."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.ResultsComparator import ResultsComparator  # noqa: E402


def main() -> None:
    ResultsComparator().render()


if __name__ == "__main__":
    main()
