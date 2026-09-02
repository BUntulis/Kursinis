"""Agento LangGraph surinkimo klasė."""
from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph import END, StateGraph

from src.llm import LLMBackend, get_backend

from .AgentState import AgentState
from .nodes import DEFAULT_APPROVAL_TOOLS, TOOL_NODE_NAMES, RouterNode, make_tool_node

if TYPE_CHECKING:
    from src.rag import DjangoDocsRetriever


class AgentGraphBuilder:
    """
    **Kam skirtas:** Surenka agento mazgus į pilną LangGraph vykdymo grafą.

    **Tikslas:** Vienoje vietoje apibrėžti agento topologiją, maršrutizavimą ir numatytas priklausomybes.

    **Argumentai:** Konstruktorius priima pasirenkamą LLM backend'ą, retriever'į ir `use_rag` vėliavą.

    **Grąžinama:** `AgentGraphBuilder` instanciją, galinčią per `build()` sukurti sukompiluotą grafą.

    **Panaudojimo pavyzdžiai:**
    ```python
    builder = AgentGraphBuilder(use_rag=True)
    agent = builder.build()
    ```
    """

    #: **Kam skirtas:** Saugo numatytą maksimalų iteracijų skaičių (paliktą suderinamumui).
    #: **Tikslas:** Naudoti jį kaip atsarginę ribą, jei būsenoje nėra `max_iterations`.
    DEFAULT_MAX_ITERATIONS = 10

    #: **Kam skirtas:** Numatytas dinaminio agento žingsnių (įrankių iškvietimų) biudžetas.
    #: **Tikslas:** Riboti, kiek veiksmų modelis gali atlikti, kol pats neiškvietė `finish`.
    DEFAULT_MAX_STEPS = 24

    #: **Kam skirtas:** Saugo LangGraph rekursijos limitą.
    #: **Tikslas:** Apsaugoti agentą nuo per ilgo ciklinio vykdymo. Dinaminiame grafe vienas
    #: veiksmas = ~2 super-žingsniai (router + įrankis), todėl limitas didesnis nei senojo
    #: fiksuoto pipeline'o (palaiko ~120 veiksmų).
    RECURSION_LIMIT = 250

    #: **Kam skirtas:** Saugo naudojamą LLM backend'ą.
    #: **Tikslas:** Leisti visiems LLM mazgams dalintis ta pačia backend instancija.
    llm: LLMBackend

    #: **Kam skirtas:** Saugo pasirinktą RAG retriever'į arba `None`.
    #: **Tikslas:** Leisti grafui veikti ir su įjungtu, ir su išjungtu RAG režimu.
    retriever: "DjangoDocsRetriever | None"

    #: **Kam skirtas:** Saugo pasirenkamus mazgų prompt'ų pakeitimus.
    #: **Tikslas:** Leisti dashboard'ui injektuoti PromptTemplate tekstus į planner/coder/critic mazgus.
    prompt_overrides: dict

    def __init__(
        self,
        llm: LLMBackend | None = None,
        retriever: "DjangoDocsRetriever | None" = None,
        use_rag: bool = True,
        prompt_overrides: dict | None = None,
        sandbox_timeout: int | None = None,
        enable_tools: bool = False,
        max_tool_steps: int | None = None,
        shell_timeout: int | None = None,
        interaction_handler=None,
        allowed_tools: set | None = None,
        require_ask: bool = False,
        max_questions: int = 0,
        design_gate=None,
        finish_gate=None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja grafo konstruktorių su numatytomis priklausomybėmis.

        **Tikslas:** Paruošti objektą, kuris gali sudėti agento mazgus ir jų ryšius.

        **Argumentai:** `llm` leidžia injektuoti konkretų backend'ą, `retriever` leidžia pateikti jau sukurtą RAG objektą, `use_rag` leidžia jį visiškai išjungti, o `prompt_overrides` leidžia pakeisti mazgų sisteminius/vartotojo prompt'us (raktai: ``planner_system``, ``planner_user``, ``coder_system``, ``coder_user``, ``critic_system``, ``critic_user``).

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        builder = AgentGraphBuilder(llm=my_backend, use_rag=False)
        builder = AgentGraphBuilder(prompt_overrides={"coder_system": "..."})
        ```
        """
        self.llm = llm or get_backend()
        self.retriever = self._resolve_retriever(retriever, use_rag)
        self.prompt_overrides = prompt_overrides or {}
        self.sandbox_timeout = sandbox_timeout
        self.enable_tools = enable_tools
        self.max_tool_steps = max_tool_steps
        self.shell_timeout = shell_timeout
        # Interactive (project-chat) mode: a callable handler(kind, payload) the agent uses to
        # ask the user questions + request approval for risky tools. None = autonomous (benchmark).
        self.interaction_handler = interaction_handler
        # Optional whitelist of canonical tool-node names (plus implicit `finish`). None = all tools.
        # The Prompt Writer passes {"ask"} so the refiner cannot write code into the sandbox.
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        # Refiner: force at least one clarifying question before the model may finish, and cap the
        # number of questions (0 = no cap) so a weak model can't loop forever asking trivia.
        self.require_ask = bool(require_ask)
        self.max_questions = int(max_questions or 0)
        # Design-first gate: a zero-arg callable, True once the sandbox has a front-end prototype.
        # While False the router blocks `finish` / design-approval asks (see RouterNode.design_gate).
        self.design_gate = design_gate
        # Finish gate: a zero-arg callable returning a veto message (or "") consulted when the
        # model queues `finish` — e.g. "no page is reachable, wire urls.py first".
        self.finish_gate = finish_gate

    @staticmethod
    def _resolve_retriever(
        retriever: "DjangoDocsRetriever | None",
        use_rag: bool,
    ) -> "DjangoDocsRetriever | None":
        """
        **Kam skirtas:** Parenka retriever'į pagal perduotus argumentus ir aplinkos būseną.

        **Tikslas:** Leisti grafui saugiai veikti net tada, kai RAG priklausomybės nėra prieinamos.

        **Argumentai:** `retriever` yra jau paruošta retriever instancija, o `use_rag` nurodo, ar בכלל reikia naudoti RAG.

        **Grąžinama:** `DjangoDocsRetriever` instanciją arba `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        retriever = AgentGraphBuilder._resolve_retriever(None, use_rag=False)
        ```
        """
        if not use_rag:
            return None
        if retriever is not None:
            return retriever
        try:
            from src.rag import DjangoDocsRetriever

            return DjangoDocsRetriever()
        except Exception as exc:  # pragma: no cover
            print(f"[warn] RAG nepavyko sukrauti ({exc}), tęsiu be jo.")
            return None

    @staticmethod
    def route_after_executor(state: AgentState) -> str:
        """
        **Kam skirtas:** Nusprendžia, kur po executor mazgo turi judėti grafas.

        **Tikslas:** Užbaigti vykdymą, jei testai praėjo arba pasiektas iteracijų limitas, kitu atveju grąžinti srautą į critic mazgą.

        **Argumentai:** `state` yra dabartinė agento būsena po executor žingsnio.

        **Grąžinama:** `"done"` arba `"retry"` reikšmę.

        **Panaudojimo pavyzdžiai:**
        ```python
        decision = AgentGraphBuilder.route_after_executor(state)
        ```
        """
        if state.get("sandbox_passed"):
            return "done"
        if state.get("iteration", 0) >= state.get(
            "max_iterations", AgentGraphBuilder.DEFAULT_MAX_ITERATIONS
        ):
            return "done"
        return "retry"

    @staticmethod
    def route_next(state: AgentState) -> str:
        """
        **Kam skirtas:** Nukreipia dinaminį grafą po router'io į pasirinktą įrankio mazgą.

        **Tikslas:** Realizuoti modelio sprendimą — vykdyti pasirinktą įrankį arba baigti darbą.

        **Argumentai:** `state` po router'io žingsnio (turi `next_action` arba `finished`).

        **Grąžinama:** Įrankio mazgo vardą (`plan`/`read`/`write`/…) arba `"end"`.
        """
        if state.get("finished"):
            return "end"
        action = state.get("next_action") or {}
        tool = action.get("tool")
        return tool if tool in TOOL_NODE_NAMES else "end"

    def build(self):
        """
        **Kam skirtas:** Sukuria ir sukompiliuoja DINAMINĮ, modelio valdomą agento grafą.

        **Tikslas:** Vietoj fiksuoto Planner→Retriever→Coder→Executor→Critic pipeline'o, modelis
        pats kiekviename žingsnyje renkasi kitą veiksmą (plan/retrieve/read/grep/list/write/edit/
        shell/test/finish). `router` mazgas priima sprendimą, o sąlyginės briaunos nukreipia į
        atitinkamą įrankio mazgą ir grįžta atgal į `router`, kol modelis iškviečia `finish` arba
        išnaudoja žingsnių biudžetą. Kiekvienas įrankio mazgas = atskiras grafo žingsnis, todėl
        dashboard'as gali transliuoti po vieną `CommandLog` eilutę kiekvienam veiksmui.

        **Argumentai:** Metodas papildomų argumentų nepriima, nes naudoja konstruktoriaus būseną.

        **Grąžinama:** Sukompiluotą LangGraph objektą.

        **Panaudojimo pavyzdžiai:**
        ```python
        agent = AgentGraphBuilder().build()
        result = agent.invoke(initial_state)
        ```
        """
        graph = StateGraph(AgentState)

        overrides = self.prompt_overrides
        # Respect an explicitly configured budget (even a small one); fall back to the
        # default only when none was provided. (Do NOT max() — that would silently ignore
        # a caller asking for fewer than DEFAULT_MAX_STEPS.)
        max_steps = int(self.max_tool_steps) if self.max_tool_steps else self.DEFAULT_MAX_STEPS

        allowed = self.allowed_tools
        tool_names = [n for n in TOOL_NODE_NAMES if allowed is None or n in allowed]

        graph.add_node(
            "router",
            RouterNode(
                self.llm,
                # the dashboard's coder_system override (if any) steers the agent persona
                system_prompt=overrides.get("router_system") or overrides.get("coder_system"),
                max_steps=max_steps,
                interactive=self.interaction_handler is not None,
                allowed_tools=allowed,
                require_ask=self.require_ask,
                max_questions=self.max_questions,
                design_gate=self.design_gate,
                finish_gate=self.finish_gate,
            ),
        )
        for name in tool_names:
            graph.add_node(
                name,
                make_tool_node(
                    name,
                    llm=self.llm,
                    retriever=self.retriever,
                    sandbox_timeout=self.sandbox_timeout,
                    shell_timeout=self.shell_timeout,
                    interaction_handler=self.interaction_handler,
                    approval_tools=DEFAULT_APPROVAL_TOOLS,
                ),
            )

        graph.set_entry_point("router")
        routes = {name: name for name in tool_names}
        routes["end"] = END

        def _route_next(state: AgentState) -> str:
            # Same as route_next, but a tool outside the allowed set ends the run rather than
            # routing to a node that doesn't exist in this (restricted) graph.
            if state.get("finished"):
                return "end"
            tool = (state.get("next_action") or {}).get("tool")
            return tool if tool in routes and tool != "end" else "end"

        graph.add_conditional_edges("router", _route_next, routes)
        for name in tool_names:
            graph.add_edge(name, "router")

        return graph.compile()
