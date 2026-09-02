"""Bazinė LLM mazgo klasė."""
from __future__ import annotations

from src.llm import LLMBackend, LLMResponse

from ..AgentState import AgentState
from ..TelemetryAccumulator import TelemetryAccumulator
from .AgentNode import AgentNode


class LLMNode(AgentNode):
    """
    **Kam skirtas:** Papildo bazinį mazgą bendru LLM iškvietimo helper'iu.

    **Tikslas:** Kad visi LLM kviečiantys mazgai telemetriją ir backend sąveiką tvarkytų vienodai.

    **Argumentai:** Konstruktorius priima `LLMBackend` instanciją.

    **Grąžinama:** `LLMNode` tipo bazinę instanciją, skirtą paveldėjimui.

    **Panaudojimo pavyzdžiai:**
    ```python
    class DemoNode(LLMNode):
        def run(self, state: AgentState) -> dict:
            resp, telemetry = self._call_llm(state, "Sveikas")
            return {**telemetry, "text": resp.text}
    ```
    """

    #: **Kam skirtas:** Saugo backend'ą, per kurį vykdomos modelio užklausos.
    #: **Tikslas:** Leisti paveldėtojams naudoti tą pačią LLM instanciją viso gyvavimo metu.
    llm: LLMBackend

    def __init__(self, llm: LLMBackend) -> None:
        """
        **Kam skirtas:** Inicializuoja LLM mazgą su konkrečiu backend'u.

        **Tikslas:** Paruošti paveldėtojams bendrą LLM prieigos tašką.

        **Argumentai:** `llm` yra backend instancija, kuri įgyvendina `complete()`.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        node = SomeLLMNode(llm_backend)
        ```
        """
        self.llm = llm

    def _call_llm(
        self,
        state: AgentState,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> tuple[LLMResponse, dict]:
        """
        **Kam skirtas:** Atlieka standartizuotą LLM iškvietimą ir suskaičiuoja telemetriją.

        **Tikslas:** Kad paveldėtojai nereikėtų kartoti `complete()` ir telemetrijos kaupimo logikos.

        **Argumentai:** `state` yra dabartinė būsena, `prompt` yra vartotojo tekstas, `system` yra sisteminis prompt'as, `temperature` valdo atsitiktinumą, o `max_tokens` riboja generacijos ilgį.

        **Grąžinama:** `tuple[LLMResponse, dict]`, kur antroji reikšmė yra telemetrijos laukų žodynas.

        **Panaudojimo pavyzdžiai:**
        ```python
        resp, telemetry = self._call_llm(state, prompt, system=self.SYSTEM)
        ```
        """
        resp = self.llm.complete(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        telemetry = TelemetryAccumulator.increment(state, resp)
        return resp, telemetry
