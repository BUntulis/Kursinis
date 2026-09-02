"""Dashboard Django app konfigūracijos klasė."""
from __future__ import annotations

from django.apps import AppConfig


class DashboardConfig(AppConfig):
    """
    Kam skirtas:
    Django app konfigūracijai.

    Tikslas:
    Užregistruoti dashboard app'ą su stabilia `label` ir modelių importu.

    Argumentai:
    Django pats inicializuoja šią klasę.

    Grąžinama:
    `DashboardConfig` instancija Django app registre.

    Panaudojimo pavyzdžiai:
    ```python
    INSTALLED_APPS = ["dashboard.apps.DashboardConfig"]
    ```
    """

    #: App'o numatytasis pirminio rakto tipas.
    default_auto_field = "django.db.models.BigAutoField"

    #: Python importo kelias iki dashboard app'o.
    name = "dashboard"

    #: Žmogui skaitomas app'o pavadinimas Django admin sąsajoje.
    verbose_name = "Kursinis Dashboard"
