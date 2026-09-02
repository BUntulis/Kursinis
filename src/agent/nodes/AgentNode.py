"""Bazinė agento mazgo klasė."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..AgentState import AgentState


class AgentNode(ABC):
    """
    **Kam skirtas:** Apibrėžia bazinį kontraktą visiems agento mazgams.

    **Tikslas:** Užtikrinti, kad kiekvienas mazgas priimtų `AgentState` ir grąžintų žodyną su būsenos atnaujinimais.

    **Argumentai:** Klasė yra abstrakti, todėl realius argumentus priima tik paveldėtojai.

    **Grąžinama:** Tiesiogiai nieko negražina, nes tai bazinis abstraktus tipas.

    **Panaudojimo pavyzdžiai:**
    ```python
    class CustomNode(AgentNode):
        def run(self, state: AgentState) -> dict:
            return {"done": True}
    ```
    """

    @abstractmethod
    def run(self, state: AgentState) -> dict:
        """
        **Kam skirtas:** Apibrėžia vieno mazgo vykdymo žingsnį.

        **Tikslas:** Priversti visus paveldėtojus įgyvendinti vienodą `run()` API.

        **Argumentai:** `state` yra bendra agento būsena prieš šio mazgo vykdymą.

        **Grąžinama:** `dict` su būsenos pakeitimais.

        **Panaudojimo pavyzdžiai:**
        ```python
        updates = node.run(state)
        ```
        """

    def __call__(self, state: AgentState) -> dict:
        """
        **Kam skirtas:** Leidžia mazgo instanciją naudoti kaip kviečiamą objektą.

        **Tikslas:** Suderinti klasės instanciją su LangGraph API, kuri tikisi `Callable`.

        **Argumentai:** `state` yra dabartinė agento būsena.

        **Grąžinama:** `dict` su būsenos atnaujinimais, gautais iš `run()`.

        **Panaudojimo pavyzdžiai:**
        ```python
        result = node(state)
        ```
        """
        return self.run(state)
