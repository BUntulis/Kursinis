"""Sugeneruoto kodo analizės klasė."""
from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import ClassVar, Pattern


class GeneratedCodeAnalyzer:
    """
    **Kam skirtas:** Analizuoja sugeneruotų failų rinkinį ir ištraukia informaciją, reikalingą sandbox projekto konfigūracijai.

    **Tikslas:** Automatiškai aptikti Django app'us, custom `User` modelį, middleware ir URL modulius.

    **Argumentai:** Konstruktorius priima `generated_files` žodyną, kuriame raktas yra failo kelias, o reikšmė yra failo turinys.

    **Grąžinama:** `GeneratedCodeAnalyzer` instanciją su pagalbinėmis analizės savybėmis.

    **Panaudojimo pavyzdžiai:**
    ```python
    analyzer = GeneratedCodeAnalyzer({"blog/models.py": "class Post(models.Model): ..."})
    print(analyzer.apps)
    ```
    """

    #: **Kam skirtas:** Saugo regex'ą custom `User` modeliui aptikti.
    #: **Tikslas:** Nustatyti, ar reikia nustatyti `AUTH_USER_MODEL`.
    CUSTOM_USER_RE: ClassVar[Pattern[str]] = re.compile(
        r"class\s+User\s*\(\s*AbstractBaseUser"
    )

    #: **Kam skirtas:** Saugo regex'ą middleware klasėms aptikti.
    #: **Tikslas:** Automatiškai surinkti custom middleware į `settings.py`.
    MIDDLEWARE_CLASS_RE: ClassVar[Pattern[str]] = re.compile(
        r"class\s+(\w*Middleware)\s*[:\(]"
    )

    #: **Kam skirtas:** Saugo sugeneruotų failų rinkinį.
    #: **Tikslas:** Leisti analizės metodams dirbti su vienu bendru įvesties šaltiniu.
    files: dict[str, str]

    def __init__(self, files: dict[str, str]) -> None:
        """
        **Kam skirtas:** Inicializuoja analizatorių su sugeneruoto kodo rinkiniu.

        **Tikslas:** Paruošti objektą pakartotinėms užklausoms apie apps, middleware ir vartotojo modelį.

        **Argumentai:** `files` yra `path -> code` žemėlapis.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        analyzer = GeneratedCodeAnalyzer(generated_files)
        ```
        """
        self.files = files

    @cached_property
    def apps(self) -> list[str]:
        """
        **Kam skirtas:** Grąžina aptiktų Django app'ų sąrašą.

        **Tikslas:** Leisti scaffolder'iui automatiškai sukonstruoti `INSTALLED_APPS`.

        **Argumentai:** Savybė papildomų argumentų nepriima.

        **Grąžinama:** `list[str]` su top-level katalogų vardais.

        **Panaudojimo pavyzdžiai:**
        ```python
        apps = analyzer.apps
        ```
        """
        result: set[str] = set()
        for path in self.files:
            parts = Path(path).parts
            if len(parts) >= 2 and parts[0] not in {".", ".."}:
                result.add(parts[0])
        return sorted(result)

    @cached_property
    def custom_user(self) -> str | None:
        """
        **Kam skirtas:** Aptinka custom `User` modelį sugeneruotuose failuose.

        **Tikslas:** Nustatyti, ar sandbox `settings.py` reikia `AUTH_USER_MODEL`.

        **Argumentai:** Savybė papildomų argumentų nepriima.

        **Grąžinama:** `<app>.User` eilutę arba `None`, jei custom modelio nėra.

        **Panaudojimo pavyzdžiai:**
        ```python
        auth_model = analyzer.custom_user
        ```
        """
        for path, code in self.files.items():
            if not path.endswith("models.py"):
                continue
            if self.CUSTOM_USER_RE.search(code):
                return f"{Path(path).parts[0]}.User"
        return None

    @cached_property
    def middleware(self) -> list[str]:
        """
        **Kam skirtas:** Surenka sugeneruotų middleware klasių importo kelius.

        **Tikslas:** Leisti sandbox projektui automatiškai prijungti custom middleware.

        **Argumentai:** Savybė papildomų argumentų nepriima.

        **Grąžinama:** `list[str]` su pilnais middleware importo keliais.

        **Panaudojimo pavyzdžiai:**
        ```python
        middleware = analyzer.middleware
        ```
        """
        found: list[str] = []
        for path, code in self.files.items():
            if not path.endswith("middleware.py"):
                continue
            app = Path(path).parts[0]
            for match in self.MIDDLEWARE_CLASS_RE.finditer(code):
                found.append(f"{app}.middleware.{match.group(1)}")
        return found

    def has_urls(self, app: str) -> bool:
        """
        **Kam skirtas:** Patikrina, ar sugeneruotas app'as turi `urls.py`.

        **Tikslas:** Nuspręsti, ar root URL konfigūracijoje reikia prijungti konkretaus app'o maršrutus.

        **Argumentai:** `app` yra top-level Django app'o vardas.

        **Grąžinama:** `True`, jei `urls.py` egzistuoja, kitu atveju `False`.

        **Panaudojimo pavyzdžiai:**
        ```python
        if analyzer.has_urls("blog"):
            ...
        ```
        """
        return f"{app}/urls.py" in self.files
