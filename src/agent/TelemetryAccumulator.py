"""LLM telemetrijos kaupiklis."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .AgentState import AgentState

if TYPE_CHECKING:
    from src.llm import LLMResponse


class TelemetryAccumulator:
    """
    **Kam skirtas:** Atnaujina LLM telemetrijos laukus agento būsenoje.

    **Tikslas:** Sukoncentruoti tokenų, latencijos ir iškvietimų skaičiaus kaupimą vienoje vietoje.

    **Argumentai:** Klasė naudojama kaip statinis namespace ir nepriima konstruktoriaus argumentų.

    **Grąžinama:** Per metodus grąžina naujus žodynus su telemetrijos laukais.

    **Panaudojimo pavyzdžiai:**
    ```python
    telemetry = TelemetryAccumulator.increment(state, response)
    initial = TelemetryAccumulator.initial()
    ```
    """

    @staticmethod
    def increment(state: AgentState, resp: "LLMResponse") -> dict:
        """
        **Kam skirtas:** Padidina telemetrijos skaitiklius pagal vieną LLM atsakymą.

        **Tikslas:** Leisti mazgams paprastai prijungti naują telemetrijos žingsnį prie bendros būsenos.

        **Argumentai:** `state` yra dabartinė agento būsena, o `resp` yra naujai gautas `LLMResponse`.

        **Grąžinama:** `dict` su atnaujintais telemetrijos laukais.

        **Panaudojimo pavyzdžiai:**
        ```python
        return {**state, **TelemetryAccumulator.increment(state, resp)}
        ```
        """
        return {
            "total_prompt_tokens": state.get("total_prompt_tokens", 0) + resp.prompt_tokens,
            "total_completion_tokens": state.get("total_completion_tokens", 0) + resp.completion_tokens,
            "total_latency_sec": state.get("total_latency_sec", 0.0) + resp.latency_sec,
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    @staticmethod
    def initial() -> dict:
        """
        **Kam skirtas:** Sugeneruoja pradinius telemetrijos laukus naujai agento būsenai.

        **Tikslas:** Užtikrinti, kad visi skaitikliai prasidėtų nuo nulio ir turėtų vienodą struktūrą.

        **Argumentai:** Metodas papildomų argumentų nepriima.

        **Grąžinama:** `dict` su nuliais užpildytais telemetrijos laukais.

        **Panaudojimo pavyzdžiai:**
        ```python
        state = {**TelemetryAccumulator.initial(), "task_id": "001"}
        ```
        """
        return {
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_latency_sec": 0.0,
            "llm_calls": 0,
        }
