"""GitHub scraper'io CLI."""
from __future__ import annotations

import argparse
from pathlib import Path

from .GitHubScraper import GitHubScraper


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--repos",
        nargs="+",
        default=None,
        help="GitHub owner/name slugai (default: curated sąrašas)",
    )
    parser.add_argument("--depth", type=int, default=GitHubScraper.DEFAULT_DEPTH)
    args = parser.parse_args()

    scraper = GitHubScraper(output=args.output, repos=args.repos, depth=args.depth)
    results = scraper.scrape()

    ok = sum(1 for value in results.values() if value)
    fail = len(results) - ok
    total_py = scraper.total_python_files()
    print(f"\n==> Klonuota {ok}, nepavyko {fail}. Iš viso .py failų: {total_py}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
