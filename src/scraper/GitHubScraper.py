"""GitHub projektų scraper klasė."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import ClassVar


class GitHubScraper:
    """
    **Kam skirtas:** Klonuoja pasirinktus GitHub Django projektus į lokalų katalogą.

    **Tikslas:** Surinkti šaltinio kodą būsimo fine-tuning ar dataset kūrimo etapams.

    **Argumentai:** Konstruktorius priima išvesties katalogą, repo sąrašą ir klonavimo gylį.

    **Grąžinama:** `GitHubScraper` instanciją, galinčią vykdyti `scrape()` ir `total_python_files()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    scraper = GitHubScraper(output=Path("data/raw"))
    results = scraper.scrape()
    ```
    """

    #: **Kam skirtas:** Saugo numatytą rekomenduojamų repo sąrašą.
    #: **Tikslas:** Leisti iškart surinkti populiarių Django projektų rinkinį.
    DEFAULT_REPOS: ClassVar[list[str]] = [
        "django/django",
        "encode/django-rest-framework",
        "django/channels",
        "jazzband/django-debug-toolbar",
        "jazzband/django-redis",
        "wagtail/wagtail",
        "saleor/saleor",
        "viewflow/django-fsm",
        "pennersr/django-allauth",
        "django-extensions/django-extensions",
        "celery/django-celery-beat",
    ]

    #: **Kam skirtas:** Saugo numatytą `git clone` gylį.
    #: **Tikslas:** Taupyti diską ir spartinti klonavimą.
    DEFAULT_DEPTH = 1

    #: **Kam skirtas:** Saugo išvesties katalogą.
    #: **Tikslas:** Nurodyti, kur bus klonuojami repo.
    output: Path

    #: **Kam skirtas:** Saugo klonuojamų repo sąrašą.
    #: **Tikslas:** Leisti vienai instancijai valdyti konkretų repo rinkinį.
    repos: list[str]

    #: **Kam skirtas:** Saugo `git clone` gylį.
    #: **Tikslas:** Valdyti, kiek istorijos bus atsisiunčiama.
    depth: int

    def __init__(
        self,
        output: Path,
        repos: list[str] | None = None,
        depth: int = DEFAULT_DEPTH,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja scraper'į su išvesties katalogu ir repo sąrašu.

        **Tikslas:** Paruošti objektą vienam ar keliems klonavimo ciklams.

        **Argumentai:** `output` nurodo katalogą, `repos` leidžia override'inti repo sąrašą, o `depth` leidžia valdyti klonavimo gylį.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scraper = GitHubScraper(Path("data/raw"), repos=["django/django"], depth=1)
        ```
        """
        self.output = output
        self.repos = repos or self.DEFAULT_REPOS
        self.depth = depth

    def scrape(self) -> dict[str, bool]:
        """
        **Kam skirtas:** Klonuoja visus sukonfigūruotus repo.

        **Tikslas:** Sukurti lokalų Django projektų rinkinį tolimesniam apdorojimui.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `dict[str, bool]`, kuriame raktas yra repo slug'as, o reikšmė nurodo sėkmę.

        **Panaudojimo pavyzdžiai:**
        ```python
        results = scraper.scrape()
        ```
        """
        self.output.mkdir(parents=True, exist_ok=True)
        print(f"==> Klonuojama į {self.output.resolve()}")
        return {slug: self._clone_one(slug) for slug in self.repos}

    def total_python_files(self) -> int:
        """
        **Kam skirtas:** Suskaičiuoja jau atsisiųstuose repo esančius `.py` failus.

        **Tikslas:** Greitai įvertinti surinkto dataset'o dydį.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `int` su visų Python failų skaičiumi.

        **Panaudojimo pavyzdžiai:**
        ```python
        total = scraper.total_python_files()
        ```
        """
        if not self.output.exists():
            return 0
        return sum(
            self._count_py(repo_dir)
            for repo_dir in self.output.iterdir()
            if repo_dir.is_dir()
        )

    def _clone_one(self, slug: str) -> bool:
        """
        **Kam skirtas:** Klonuoja vieną konkretų GitHub repo.

        **Tikslas:** Izoliuoti vieno repo klonavimo ir klaidų valdymo logiką.

        **Argumentai:** `slug` yra `owner/name` formato repo identifikatorius.

        **Grąžinama:** `True`, jei repo sėkmingai suklonuotas arba jau egzistavo, kitu atveju `False`.

        **Panaudojimo pavyzdžiai:**
        ```python
        ok = scraper._clone_one("django/django")
        ```
        """
        target = self.output / slug.split("/")[-1]
        if target.exists():
            print(f"[skip] {slug} jau yra: {target}")
            return True

        url = f"https://github.com/{slug}.git"
        print(f"[clone] {url} -> {target}")
        try:
            subprocess.run(
                ["git", "clone", f"--depth={self.depth}", "--quiet", url, str(target)],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            print(f"[KLAIDA] {slug}: {exc}")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return False

    @staticmethod
    def _count_py(root: Path) -> int:
        """
        **Kam skirtas:** Suskaičiuoja `.py` failus viename repo kataloge.

        **Tikslas:** Leisti greitai agreguoti Python failų kiekį be papildomos būsenos.

        **Argumentai:** `root` yra repo katalogas.

        **Grąžinama:** `int` su `.py` failų skaičiumi.

        **Panaudojimo pavyzdžiai:**
        ```python
        total = GitHubScraper._count_py(Path("data/raw/django"))
        ```
        """
        return sum(1 for _ in root.rglob("*.py"))
