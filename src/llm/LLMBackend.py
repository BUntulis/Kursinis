"""LLM backend sąsajos klasė."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .LLMResponse import LLMResponse


class LLMCancelled(Exception):
    """Raised by a backend mid-generation when its ``cancel_check`` says the user pressed Stop —
    lets the caller abort the CURRENT LLM call instead of waiting out the whole generation."""


class LLMBackend(ABC):
    """
    **Kam skirtas:** Apibrėžia bendrą kontraktą visiems kalbos modelių backend'ams.

    **Tikslas:** Leisti agentui dirbti per stabilų interfeisą, nepriklausomai nuo to, ar naudojamas `Ollama`, ar `OpenAI` suderinamas tiekėjas.

    **Argumentai:** Klasė yra abstrakti ir skirta paveldėjimui, todėl realius argumentus priima tik konkretūs paveldėtojai.

    **Grąžinama:** Tiesiogiai negražina nieko, nes tai abstraktus bazinis tipas.

    **Panaudojimo pavyzdžiai:**
    ```python
    class FakeBackend(LLMBackend):
        name = "fake"

        def complete(self, prompt: str, **kwargs) -> LLMResponse:
            return LLMResponse(text="ok")
    ```
    """

    #: **Kam skirtas:** Saugo backend'o identifikatorių.
    #: **Tikslas:** Leisti `LLMFactory` registrui susieti vardą su konkrečia realizacija.
    name: str

    #: **Kam skirtas:** Nurodo, ar backend'as palaiko native function/tool calling.
    #: **Tikslas:** Leisti tool-using agentui pasirinkti native įrankius ar tekstinį protokolą.
    #: Numatytai `False` — paveldėtojai (pvz. tool-capable Ollama modeliai) gali perrašyti.
    supports_tools: bool = False

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        **Kam skirtas:** Atlieka multi-turn pokalbio užklausą (su pasirenkamais įrankiais).

        **Tikslas:** Suteikti tool-using agentui vieningą sąsają; numatytoji realizacija
        sulieja žinutes į vieną prompt'ą ir naudoja `complete()` (tekstinis protokolas),
        kad veiktų net backend'ai be native tool calling.

        **Argumentai:** `messages` yra `{"role","content"}` (ir galimai `tool_calls`/`tool`) sąrašas,
        `tools` yra įrankių schemų sąrašas (ignoruojamas, jei `supports_tools` yra `False`).

        **Grąžinama:** `LLMResponse` su tekstu ir (jei palaikoma) `tool_calls`.
        """
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), None)
        convo = "\n\n".join(
            f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
            for m in messages
            if m.get("role") != "system" and m.get("content")
        )
        return self.complete(convo, system=system, temperature=temperature, max_tokens=max_tokens)

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        **Kam skirtas:** Atlieka vieną chat-completion užklausą per konkretų backend'ą.

        **Tikslas:** Sugeneruoti tekstinį atsakymą pagal vartotojo ir, jei pateikta, sisteminį prompt'ą.

        **Argumentai:** `prompt` yra vartotojo užklausa, `system` apibrėžia rolę, `temperature` valdo atsitiktinumą, o `max_tokens` riboja išvesties ilgį.

        **Grąžinama:** `LLMResponse` objektas su tekstu ir telemetrija.

        **Panaudojimo pavyzdžiai:**
        ```python
        backend.complete("Paaiškink queryset'ą", system="Tu esi Django ekspertas.")
        ```
        """
