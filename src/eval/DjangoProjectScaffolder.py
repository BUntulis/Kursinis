"""Laikino Django projekto scaffold'inimo klasė."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import ClassVar

from src.benchmark import BenchmarkTask

from .GeneratedCodeAnalyzer import GeneratedCodeAnalyzer


class DjangoProjectScaffolder:
    """
    **Kam skirtas:** Sugeneruoja minimalų Django projekto skeletą laikinoje direktorijoje.

    **Tikslas:** Paversti sugeneruotų failų rinkinį paleidžiamu Django projektu, kuriame galima vykdyti referencinius testus.

    **Argumentai:** Konstruktorius priima projekto šaknį, kodo analizatorių ir benchmark užduotį.

    **Grąžinama:** `DjangoProjectScaffolder` instanciją, galinčią vykdyti `scaffold()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    scaffolder = DjangoProjectScaffolder(root, analyzer, task)
    scaffolder.scaffold()
    ```
    """

    #: **Kam skirtas:** Saugo `settings.py` šabloną.
    #: **Tikslas:** Vienoje vietoje apibrėžti visą minimalią Django konfigūraciją sandbox projektui.
    SETTINGS_PY: ClassVar[str] = '''\
SECRET_KEY = "test-secret-key-for-sandbox-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "urls_root"
DATABASES = {{
    "default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    {extra_apps}
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    {extra_middleware}
]
TEMPLATES = [{{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {{"context_processors": []}},
}}]
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
{auth_user_model}
'''

    #: **Kam skirtas:** Saugo root URL modulio šabloną.
    #: **Tikslas:** Automatiškai sugeneruoti vieną bendrą maršrutų įėjimo tašką.
    URLS_ROOT_PY: ClassVar[str] = (
        "from django.urls import path, include\n\n"
        "urlpatterns = [\n{includes}\n]\n"
    )

    #: **Kam skirtas:** Saugo `pytest.ini` šabloną.
    #: **Tikslas:** Konfigūruoti pytest taip, kad jis žinotų Django nustatymų modulį.
    PYTEST_INI: ClassVar[str] = (
        "[pytest]\n"
        "DJANGO_SETTINGS_MODULE = settings\n"
        "python_files = test_*.py *_tests.py tests.py\n"
        "addopts = -p no:cacheprovider --tb=short --no-header\n"
    )

    #: **Kam skirtas:** Saugo laikino projekto šakninę direktoriją.
    #: **Tikslas:** Žinoti, kur turi būti įrašyti visi sandbox failai.
    root: Path

    #: **Kam skirtas:** Saugo sugeneruoto kodo analizatorių.
    #: **Tikslas:** Naudoti automatiškai aptiktą informaciją `settings.py` ir URL generavimui.
    analyzer: GeneratedCodeAnalyzer

    #: **Kam skirtas:** Saugo benchmark užduotį.
    #: **Tikslas:** Iš jos paimti referencinius testus ir kitą vykdymui reikalingą kontekstą.
    task: BenchmarkTask

    def __init__(
        self,
        root: Path,
        analyzer: GeneratedCodeAnalyzer,
        task: BenchmarkTask,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja laikino Django projekto scaffold'erį.

        **Tikslas:** Paruošti visą reikalingą informaciją vienam projekto sukūrimo ciklui.

        **Argumentai:** `root` nurodo laikino projekto katalogą, `analyzer` pateikia sugeneruoto kodo analizę, o `task` pateikia referencinius testus.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder = DjangoProjectScaffolder(root, analyzer, task)
        ```
        """
        self.root = root
        self.analyzer = analyzer
        self.task = task

    def scaffold(self) -> None:
        """
        **Kam skirtas:** Sukuria visą laikiną Django projektą.

        **Tikslas:** Vienu iškvietimu įrašyti sugeneruotus failus, `settings.py`, URL, testus ir `pytest.ini`.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder.scaffold()
        ```
        """
        self._write_generated_files()
        for app in self.analyzer.apps:
            self._ensure_app_skeleton(app)
        self._write_settings()
        self._write_urls()
        self._write_reference_tests()
        self._write_pytest_ini()

    def _write_generated_files(self) -> None:
        """
        **Kam skirtas:** Įrašo visus sugeneruotus failus į laikino projekto katalogą.

        **Tikslas:** Fiziškai perkelti agento sugeneruotą sprendimą į paleidžiamą failų struktūrą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._write_generated_files()
        ```
        """
        for rel_path, content in self.analyzer.files.items():
            target = self.root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _ensure_app_skeleton(self, app: str) -> None:
        """
        **Kam skirtas:** Sukuria minimalią Django app struktūrą, jei jos trūksta.

        **Tikslas:** Užtikrinti, kad kiekvienas aptiktas app'as turėtų `__init__.py` ir, jei reikia, `apps.py`.

        **Argumentai:** `app` yra Django app'o pavadinimas.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._ensure_app_skeleton("blog")
        ```
        """
        app_dir = self.root / app
        app_dir.mkdir(exist_ok=True)
        (app_dir / "__init__.py").touch(exist_ok=True)
        apps_py = app_dir / "apps.py"
        if f"{app}/apps.py" not in self.analyzer.files and not apps_py.exists():
            apps_py.write_text(
                textwrap.dedent(
                    f"""\
                    from django.apps import AppConfig


                    class {app.capitalize()}Config(AppConfig):
                        default_auto_field = "django.db.models.BigAutoField"
                        name = "{app}"
                    """
                ),
                encoding="utf-8",
            )

    def _write_settings(self) -> None:
        """
        **Kam skirtas:** Sugeneruoja sandbox `settings.py` failą.

        **Tikslas:** Automatiškai sukonfigūruoti apps, middleware ir `AUTH_USER_MODEL` pagal sugeneruotą kodą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._write_settings()
        ```
        """
        extra_apps = ",\n    ".join(f'"{app}"' for app in self.analyzer.apps)
        if extra_apps:
            extra_apps += ","
        extra_middleware = ",\n    ".join(f'"{item}"' for item in self.analyzer.middleware)
        if extra_middleware:
            extra_middleware += ","
        auth_line = (
            f'AUTH_USER_MODEL = "{self.analyzer.custom_user}"'
            if self.analyzer.custom_user
            else ""
        )
        (self.root / "settings.py").write_text(
            self.SETTINGS_PY.format(
                extra_apps=extra_apps,
                extra_middleware=extra_middleware,
                auth_user_model=auth_line,
            ),
            encoding="utf-8",
        )

    def _write_urls(self) -> None:
        """
        **Kam skirtas:** Sugeneruoja root URL modulį.

        **Tikslas:** Sujungti visų sugeneruotų app'ų URL modulius į vieną įėjimo tašką.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._write_urls()
        ```
        """
        includes = (
            "\n".join(
                f'    path("", include("{app}.urls")),'
                for app in self.analyzer.apps
                if self.analyzer.has_urls(app)
            )
            or "    # no urls"
        )
        (self.root / "urls_root.py").write_text(
            self.URLS_ROOT_PY.format(includes=includes),
            encoding="utf-8",
        )

    def _write_reference_tests(self) -> None:
        """
        **Kam skirtas:** Įrašo benchmark referencinius testus į laikiną projektą.

        **Tikslas:** Paruošti pytest vykdymui tikslinį testų failą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._write_reference_tests()
        ```
        """
        (self.root / "test_reference.py").write_text(
            self.task.reference_tests,
            encoding="utf-8",
        )

    def _write_pytest_ini(self) -> None:
        """
        **Kam skirtas:** Įrašo `pytest.ini` konfigūraciją.

        **Tikslas:** Užtikrinti, kad pytest žinotų, kaip paleisti Django testus sandbox aplinkoje.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        scaffolder._write_pytest_ini()
        ```
        """
        (self.root / "pytest.ini").write_text(self.PYTEST_INI, encoding="utf-8")
