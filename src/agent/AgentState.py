"""Agento būsenos tipas."""
from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    """
    **Kam skirtas:** Apibrėžia bendrą būsenos struktūrą, kuria dalinasi visi agento mazgai.

    **Tikslas:** Užtikrinti, kad LangGraph mazgai naudotų vienodai pavadintus ir tipizuotus laukus.

    **Argumentai:** `TypedDict` tipo klasė tiesioginių konstruktoriaus argumentų nenaudoja, nes ji skirta tipizavimui.

    **Grąžinama:** Tiesiogiai nieko negražina, nes tai tipų aprašas.

    **Panaudojimo pavyzdžiai:**
    ```python
    state: AgentState = {
        "task_id": "001_blog_model",
        "task_title": "Blog modelis",
    }
    ```
    """

    #: **Kam skirtas:** Saugo užduoties identifikatorių.
    #: **Tikslas:** Leisti agentui ir rezultatams susieti būseną su konkrečia benchmark užduotimi.
    task_id: str

    #: **Kam skirtas:** Saugo užduoties pavadinimą.
    #: **Tikslas:** Naudoti jį prompt'uose ir ataskaitose.
    task_title: str

    #: **Kam skirtas:** Saugo pilną užduoties aprašą.
    #: **Tikslas:** Pateikti modeliams visą reikiamą kontekstą generavimui.
    task_description: str

    #: **Kam skirtas:** Saugo tikėtinų failų sąrašą.
    #: **Tikslas:** Nurodyti, kokius failus agentas turi sugeneruoti.
    expected_files: list[str]

    #: **Kam skirtas:** Saugo referencinių testų kodą.
    #: **Tikslas:** Leisti executor'iui sukonstruoti benchmark objektą ir vykdyti sandbox testus.
    reference_tests: str

    #: **Kam skirtas:** Saugo planuotojo sugeneruotą planą.
    #: **Tikslas:** Perduoti struktūrizuotą planą koderio mazgui.
    plan: str

    #: **Kam skirtas:** Saugo RAG retrieval kontekstą.
    #: **Tikslas:** Praturtinti kodavimo prompt'ą aktualia dokumentacija.
    retrieved_context: str

    #: **Kam skirtas:** Saugo sugeneruotus failus.
    #: **Tikslas:** Leisti critic'ui ir executor'iui dirbti su esamu sprendimu.
    generated_files: dict[str, str]

    #: **Kam skirtas:** Saugo agento sugeneruotus testus.
    #: **Tikslas:** Rezervuoti vietą galimam papildomam testų rašymo etapui.
    written_tests: str

    #: **Kam skirtas:** Saugo sandbox vykdymo rezultatą.
    #: **Tikslas:** Leisti routing logikai nuspręsti, ar užduotis jau pavyko.
    sandbox_passed: bool

    #: **Kam skirtas:** Saugo sandbox `stdout`.
    #: **Tikslas:** Leisti critic'ui ir ataskaitoms matyti testų išvestį.
    sandbox_stdout: str

    #: **Kam skirtas:** Saugo sandbox `stderr`.
    #: **Tikslas:** Leisti critic'ui analizuoti klaidų signalus.
    sandbox_stderr: str

    #: **Kam skirtas:** Saugo praeitų testų skaičių.
    #: **Tikslas:** Leisti kaupti struktūruotą vykdymo suvestinę.
    tests_passed: int

    #: **Kam skirtas:** Saugo nepraeitų testų skaičių.
    #: **Tikslas:** Leisti matyti, kiek testų dar liko nesėkmingi.
    tests_failed: int

    #: **Kam skirtas:** Saugo dabartinės iteracijos indeksą.
    #: **Tikslas:** Valdyti critic-koderio taisymo ciklą.
    iteration: int

    #: **Kam skirtas:** Saugo maksimalų iteracijų limitą.
    #: **Tikslas:** Apsaugoti agentą nuo begalinio ciklo.
    max_iterations: int

    #: **Kam skirtas:** Saugo critic mazgo grįžtamąjį ryšį.
    #: **Tikslas:** Perduoti kodavimo mazgui koncentruotą taisymo instrukciją.
    critic_feedback: str

    #: **Kam skirtas:** Saugo koderio kodo ištraukimo klaidą, jei jokių blokų nepavyko išgauti.
    #: **Tikslas:** Leisti critic'ui ir dashboard'ui pamatyti, kodėl negauta jokių failų.
    coder_error: str

    #: **Kam skirtas:** Saugo modelio darbo srities (workspace) kelią diske.
    #: **Tikslas:** Leisti tool-using koderiui skaityti/rašyti/vykdyti realiame kataloge.
    workspace_path: str

    #: **Kam skirtas:** Saugo planuotojo aiškiai įvardytas prielaidas (self-clarify).
    #: **Tikslas:** Best-practice — fiksuoti prielaidas, kai nėra žmogaus atsakyti į klausimus.
    assumptions: str

    #: **Kam skirtas:** Saugo per koderio tool-loop'ą atliktų įrankių iškvietimų skaičių.
    #: **Tikslas:** Telemetrija ir tool-loop biudžeto stebėjimas.
    tool_calls_made: int

    #: **Kam skirtas:** Saugo trumpą koderio tool-loop'o veiksmų santrauką.
    #: **Tikslas:** Parodyti dashboard'o sraute, kokius read/write/execute veiksmus modelis atliko.
    tool_log: str

    #: **Kam skirtas:** Saugo užbaigimo vėliavą.
    #: **Tikslas:** Leisti papildomoms topologijoms aiškiai pažymėti, kada darbas baigtas.
    done: bool

    #: **Kam skirtas:** Saugo suminius įvesties tokenus.
    #: **Tikslas:** Kaupti telemetriją per visą agento darbą.
    total_prompt_tokens: int

    #: **Kam skirtas:** Saugo suminius išvesties tokenus.
    #: **Tikslas:** Kaupti generavimo sąnaudas per visus LLM iškvietimus.
    total_completion_tokens: int

    #: **Kam skirtas:** Saugo sumines LLM vėlinimo sekundes.
    #: **Tikslas:** Atskirti modelio generavimo laiką nuo bendro `wall time`.
    total_latency_sec: float

    #: **Kam skirtas:** Saugo LLM iškvietimų skaičių.
    #: **Tikslas:** Leisti analizuoti agento ciklų intensyvumą.
    llm_calls: int

    # ----------------------------------------------------------------- dynamic router
    # Fields used by the dynamic, model-routed agent (RouterNode + tool nodes), where the
    # model chooses ONE tool per step instead of following the fixed pipeline.

    #: **Kam skirtas:** Saugo router'io pokalbio istoriją (system/user/assistant/tool žinutės).
    #: **Tikslas:** Leisti ReAct ciklui matyti ankstesnius veiksmus ir jų rezultatus.
    messages: list

    #: **Kam skirtas:** Eilė veiksmų, kuriuos modelis pasirinko viename ėjime (dar neįvykdytų).
    #: **Tikslas:** Router'is paima po vieną veiksmą iš eilės kiekviename žingsnyje.
    pending_actions: list

    #: **Kam skirtas:** Veiksmas, kurį dabar reikia įvykdyti: ``{"tool": str, "args": dict}``.
    #: **Tikslas:** Perduoti router'io sprendimą atitinkamam įrankio mazgui.
    next_action: dict

    #: **Kam skirtas:** Paskutinio įvykdyto veiksmo santrauka: ``{"tool", "target"}``.
    #: **Tikslas:** Leisti dashboard'ui parodyti, kokį įrankį modelis ką tik panaudojo.
    last_action: dict

    #: **Kam skirtas:** Paskutinio įrankio stebėjimas (observation) tekstu.
    #: **Tikslas:** Rodyti įrankio išvestį sraute ir grąžinti ją modeliui.
    last_observation: str

    #: **Kam skirtas:** Bendras atliktų įrankių žingsnių skaičius (biudžeto riba).
    #: **Tikslas:** Apriboti dinaminį ciklą ``max_steps`` žingsnių.
    step_count: int

    #: **Kam skirtas:** Maksimalus dinaminio agento žingsnių skaičius.
    #: **Tikslas:** Apsaugoti nuo begalinio veiksmų ciklo.
    max_steps: int

    #: **Kam skirtas:** Vėliava, ar router'is ką tik kvietė LLM (priėmė naują sprendimą).
    #: **Tikslas:** Leisti dashboard'ui parodyti „mąstymo" eilutę tik tada, kai modelis galvojo.
    did_think: bool

    #: **Kam skirtas:** Saugi, aukšto lygio etiketė apie dabartinį sprendimą (NE slaptas CoT).
    #: **Tikslas:** Parodyti trumpą „ką darau dabar" statusą sraute.
    router_thought: str

    #: **Kam skirtas:** Žymi, kad dinaminis agentas baigė darbą (modelis iškvietė ``finish``).
    #: **Tikslas:** Leisti grafui korektiškai pasiekti END.
    finished: bool

    #: **Kam skirtas:** Trumpas paaiškinimas, kodėl ciklas baigėsi (finish / biudžetas / nėra veiksmų).
    #: **Tikslas:** Telemetrija ir galutinės klaidos santrauka.
    stop_reason: str

    #: **Kam skirtas:** Modelio valdomas užduočių sąrašas: ``[{"content", "status"}, ...]``.
    #: **Tikslas:** Leisti agentui suplanuoti ir žymėti progresą (todo/doing/done); rodoma
    #: dashboard'o pokalbyje kaip checklist kortelė ir atskirame „Tasks" skydelyje.
    tasks: list

    #: **Kam skirtas:** Žymi, kad agentas jau bent kartą uždavė klausimą vartotojui (ask_user).
    #: **Tikslas:** Refiner (prompt_writer) režime priversti bent vieną patikslinimo ratą prieš
    #: leidžiant baigti (``require_ask``).
    asked: bool

    #: **Kam skirtas:** Kiek kartų agentas uždavė klausimą vartotojui per šį paleidimą.
    #: **Tikslas:** Refiner režime apriboti klausimų skaičių (``max_questions``) — po kelių
    #: esminių klausimų priverstinai pereiti prie struktūrizuoto prompto, kad modelis neįstrigtų
    #: begaliniame trivialių klausimų cikle.
    ask_count: int
