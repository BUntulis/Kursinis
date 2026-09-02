"""LLM backend fabrikas."""
from __future__ import annotations

from typing import Callable, ClassVar

from .LLMBackend import LLMBackend


class LLMFactory:
    """
    **Kam skirtas:** Laiko registrą, kuris pagal vardą sukuria konkretaus LLM backend'o instanciją.

    **Tikslas:** Leisti projektui lanksčiai perjungti backend'us ir registruoti naujas realizacijas be `if/elif` grandinių.

    **Argumentai:** Klasė priima registruojamus `factory` kviečiamuosius objektus per metodą `register()`.

    **Grąžinama:** `LLMBackend` instanciją per `create()` metodą.

    **Panaudojimo pavyzdžiai:**
    ```python
    LLMFactory.register("fake", lambda: FakeBackend())
    backend = LLMFactory.create("fake")
    ```
    """

    #: **Kam skirtas:** Saugo vardų ir backend kūrimo funkcijų registrą.
    #: **Tikslas:** Leisti vienoje vietoje valdyti visas prieinamas backend realizacijas.
    _registry: ClassVar[dict[str, Callable[[], LLMBackend]]] = {}

    @classmethod
    def register(cls, name: str, factory: Callable[[], LLMBackend]) -> None:
        """
        **Kam skirtas:** Užregistruoja naują backend kūrimo funkciją.

        **Tikslas:** Papildyti globalų backend registrą nauja realizacija.

        **Argumentai:** `name` yra backend vardas, o `factory` yra kviečiamasis objektas, grąžinantis `LLMBackend` instanciją.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        LLMFactory.register("fake", lambda: FakeBackend())
        ```
        """
        cls._registry[name.lower()] = factory

    @classmethod
    def create(cls, name: str | None = None) -> LLMBackend:
        """
        **Kam skirtas:** Sukuria backend instanciją pagal vardą arba numatytą projekto konfigūraciją.

        **Tikslas:** Pateikti vieną įėjimo tašką backend parinkimui visame projekte.

        **Argumentai:** `name` leidžia aiškiai nurodyti backend vardą; jei jis nepateiktas, naudojama `config.settings` objekto `llm_backend` reikšmė.

        **Grąžinama:** `LLMBackend` realizacijos instancija.

        **Panaudojimo pavyzdžiai:**
        ```python
        backend = LLMFactory.create("ollama")
        backend = LLMFactory.create()
        ```
        """
        from config import settings

        backend_name = (name or settings.llm_backend).lower()
        if backend_name not in cls._registry:
            raise ValueError(
                f"Nežinomas LLM backend: {backend_name!r}. "
                f"Užregistruoti: {sorted(cls._registry)}"
            )
        return cls._registry[backend_name]()
