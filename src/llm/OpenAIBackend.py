"""OpenAI suderinamas backend."""
from __future__ import annotations

import time

from openai import OpenAI

from config import settings

from .LLMBackend import LLMBackend
from .LLMResponse import LLMResponse


class OpenAIBackend(LLMBackend):
    """
    **Kam skirtas:** Įgyvendina `LLMBackend` kontraktą per OpenAI suderinamą klientą.

    **Tikslas:** Leisti projektui naudoti oficialų OpenAI API ar kitus suderinamus serverius, pavyzdžiui, `OpenRouter` ar `vLLM`.

    **Argumentai:** Konstruktorius priima pasirenkamą modelio vardą, API raktą ir bazinį URL.

    **Grąžinama:** `OpenAIBackend` instanciją, galinčią vykdyti `complete()` užklausas.

    **Panaudojimo pavyzdžiai:**
    ```python
    backend = OpenAIBackend(model="gpt-4o-mini")
    response = backend.complete("Sukurk viewset'ą")
    ```
    """

    #: **Kam skirtas:** Saugo backend identifikatorių.
    #: **Tikslas:** Leisti fabrikui ir ataskaitoms atskirti šį backend'ą nuo kitų.
    name = "openai"

    #: **Kam skirtas:** Saugo aktyvų modelio vardą.
    #: **Tikslas:** Užtikrinti nuoseklų to paties modelio naudojimą visose užklausose.
    model: str

    #: **Kam skirtas:** Saugo OpenAI kliento objektą.
    #: **Tikslas:** Pakartotinai naudoti vieną HTTP klientą kelioms užklausoms.
    client: OpenAI

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja OpenAI suderinamą klientą ir pasirinktą modelį.

        **Tikslas:** Paruošti backend'ą taip, kad užklausoms nereikėtų kiekvieną kartą perduoti autentifikacijos ir URL konfigūracijos.

        **Argumentai:** `model` leidžia override'inti modelį, `api_key` perduoda autentifikaciją, o `base_url` nurodo konkretų API serverį.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        backend = OpenAIBackend(base_url="https://api.openai.com/v1")
        ```
        """
        self.model = model or settings.openai_model
        self.client = OpenAI(
            api_key=api_key or settings.openai_api_key or "sk-noop",
            base_url=base_url or settings.openai_base_url,
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        **Kam skirtas:** Išsiunčia vieną chat-completion užklausą į OpenAI suderinamą API.

        **Tikslas:** Konvertuoti `OpenAI` kliento atsakymą į bendrą projekto `LLMResponse` struktūrą.

        **Argumentai:** `prompt` yra vartotojo tekstas, `system` apibrėžia sisteminį kontekstą, `temperature` valdo atsitiktinumą, o `max_tokens` riboja generaciją.

        **Grąžinama:** `LLMResponse` objektą su tekstu, tokenų metrika ir latencija.

        **Panaudojimo pavyzdžiai:**
        ```python
        resp = backend.complete("Paaiškink DRF serializer'į", temperature=0.1)
        print(resp.model)
        ```
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        latency = time.perf_counter() - start

        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_sec=latency,
            model=self.model,
        )
