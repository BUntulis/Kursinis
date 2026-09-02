"""Patikrina ar Ollama serveris pasiekiamas ir ar reikalingi modeliai įkelti.

Naudojimas:
    python scripts/check_ollama.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

import json

# Užtikrinti, kad veikia paleidžiant tiesiogiai (`python scripts/check_ollama.py`)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


REQUIRED_MODELS = [
    settings.ollama_model,       # generavimui
    "nomic-embed-text",          # embeddings (RAG)
]


def main() -> int:
    host = settings.ollama_host.rstrip("/")
    url = f"{host}/api/tags"
    print(f"==> GET {url}")
    try:
        with urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except URLError as e:
        print(f"[KLAIDA] Ollama serveris nepasiekiamas: {e}")
        print("Paleiskite Ollama (Windows: ollama programa auto-startuoja).")
        return 1

    installed = {m["name"] for m in data.get("models", [])}
    print(f"Įkelti modeliai: {sorted(installed) or '(nieko)'}")

    def _matches(needed: str, installed_names: set[str]) -> bool:
        # Tikslus match arba toks pat bazinis vardas (be tag).
        if needed in installed_names:
            return True
        base_needed = needed.split(":")[0]
        return any(m.split(":")[0] == base_needed for m in installed_names)

    missing = [n for n in REQUIRED_MODELS if not _matches(n, installed)]

    if missing:
        print(f"[TRŪKSTA] {missing}")
        print("Paleiskite: scripts/setup_ollama.ps1 arba `ollama pull <model>`")
        return 2

    print("==> OK: visi reikalingi modeliai įkelti.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
