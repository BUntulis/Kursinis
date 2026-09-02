"""Projekto konfigūracijos klasė."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """
    **Kam skirtas:** Saugo visas projekto konfigūracijos reikšmes vienoje nekintamoje klasėje.

    **Tikslas:** Centralizuoti aplinkos kintamųjų nuskaitymą ir pateikti tipizuotą API kitoms projekto dalims.

    **Argumentai:** Klasė priima laukus per automatiškai sugeneruotą `dataclass` konstruktorių, tačiau numatytoji paskirtis yra naudoti numatytąsias reikšmes iš aplinkos.

    **Grąžinama:** `Settings` objekto instancija su visomis sukonfigūruotomis reikšmėmis.

    **Panaudojimo pavyzdžiai:**
    ```python
    from config.Settings import Settings

    settings = Settings()
    print(settings.llm_backend)
    ```
    """

    #: **Kam skirtas:** Nurodo, kurį LLM backend'ą turi naudoti projektas.
    #: **Tikslas:** Leisti perjungti tarp `ollama` ir `openai` nekeičiat kodo.
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama")

    #: **Kam skirtas:** Saugo Ollama HTTP serverio adresą.
    #: **Tikslas:** Leisti backend'ui prisijungti prie lokaliai ar nuotoliniu būdu veikiančios Ollama instancijos.
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")

    #: **Kam skirtas:** Nurodo numatytą Ollama modelio pavadinimą.
    #: **Tikslas:** Užtikrinti, kad backend'as turėtų aiškų modelio fallback'ą.
    #: Hardware rationale: ``qwen2.5-coder:7b-instruct`` at Q4 quantization needs
    #: roughly 4 GB of VRAM, which fits the 4 GB budget of the RTX 3060 Laptop GPU.
    #: Larger tags (e.g. ``qwen2.5-coder:14b``) exceed 4 GB and spill to slower
    #: CPU/RAM. Run ``python manage.py recommend_models`` to re-check this for the
    #: detected hardware.
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b-instruct")

    #: **Kam skirtas:** Per-request timeout (sekundėmis) Ollama generavimo iškvietimams.
    #: **Tikslas:** Apsaugoti agento giją nuo amžino blokavimosi — `ollama`/`httpx` klientas
    #: pagal nutylėjimą laukia neribotai, todėl lėtas (pvz. didelis modelis CPU režimu) ar
    #: pakibęs užklausa įstrigdytų pokalbį ties „Analyzing…". Pasiekus laiką, iškvietimas
    #: meta klaidą → ėjimas pažymimas FAILED ir parodoma „Retry / pakeisti modelį" juosta.
    ollama_timeout_sec: int = int(os.getenv("OLLAMA_TIMEOUT_SEC", "300"))

    #: **Kam skirtas:** Saugo OpenAI API raktą.
    #: **Tikslas:** Leisti autentifikuotis prie OpenAI suderinamos API.
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    #: **Kam skirtas:** Saugo OpenAI API bazinį URL.
    #: **Tikslas:** Palaikyti tiek oficialų OpenAI endpoint'ą, tiek suderinamus proxy serverius.
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    #: **Kam skirtas:** Nurodo numatytą OpenAI modelį.
    #: **Tikslas:** Leisti backend'ui iškart vykdyti užklausas be papildomo modelio perdavimo.
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    #: **Kam skirtas:** Saugo ChromaDB katalogo kelią.
    #: **Tikslas:** Centralizuoti RAG vektorinės bazės lokaciją.
    chroma_path: Path = ROOT / os.getenv("CHROMA_PATH", ".chroma")

    #: **Kam skirtas:** Nurodo embedding modelio vardą.
    #: **Tikslas:** Laikyti vienoje vietoje numatytą embedinimo modelio konfigūraciją.
    embed_model: str = os.getenv("EMBED_MODEL", "nomic-embed-text")

    #: **Kam skirtas:** Saugo maksimalų sandbox testų vykdymo laiką sekundėmis.
    #: **Tikslas:** Apsaugoti benchmark paleidimus nuo užstrigusių testų.
    sandbox_timeout_sec: int = int(os.getenv("SANDBOX_TIMEOUT_SEC", "60"))

    #: **Autonomous mode** — large timeout cap (seconds) for model generation + tests
    #: so a slow run is not killed prematurely ("never stop because it's slow"). Big,
    #: but bounded, so a truly hung process still eventually releases.
    autonomous_timeout_cap_sec: int = int(os.getenv("AUTONOMOUS_TIMEOUT_CAP_SEC", "7200"))

    #: **Autonomous mode** — sandbox pytest timeout cap (seconds).
    autonomous_sandbox_timeout_sec: int = int(os.getenv("AUTONOMOUS_SANDBOX_TIMEOUT_SEC", "3600"))

    #: **Autonomous mode** — resource sampling interval (seconds) for the monitor thread.
    autonomous_monitor_interval_sec: float = float(os.getenv("AUTONOMOUS_MONITOR_INTERVAL_SEC", "1.5"))

    #: **Autonomous mode** — how many times to retry a failed/timed-out generation.
    autonomous_generation_retries: int = int(os.getenv("AUTONOMOUS_GENERATION_RETRIES", "2"))

    #: **Tool-using agent** — outer Coder->Executor->Critic iterations (the "10 iterations total").
    agent_max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "10"))

    #: **Tool-using agent** — give models read/write/execute tools by default.
    agent_enable_tools: bool = os.getenv("AGENT_ENABLE_TOOLS", "1") not in {"0", "false", "False", ""}

    #: **Dynamic agent** — total tool steps the model may take in one run (the whole
    #: plan/read/grep/write/edit/run/test loop runs here, so this is larger than the old
    #: per-turn inner budget; each step ≈ 2 LangGraph supersteps, well under RECURSION_LIMIT).
    agent_max_tool_steps: int = int(os.getenv("AGENT_MAX_TOOL_STEPS", "30"))

    #: **Tool-using agent** — timeout (seconds) for a single ``run_shell`` tool call.
    agent_shell_timeout_sec: int = int(os.getenv("AGENT_SHELL_TIMEOUT_SEC", "180"))

    #: **Chat context window** — the token budget a chat's running history is measured against for
    #: the context meter + auto-compact. Local models typically expose 8K; keep conservative.
    chat_context_window_tokens: int = int(os.getenv("CHAT_CONTEXT_WINDOW_TOKENS", "8000"))

    #: **Auto-compact** — when the estimated history exceeds this fraction of the window, the next
    #: build turn summarizes older turns into chat memory before running.
    chat_auto_compact_at: float = float(os.getenv("CHAT_AUTO_COMPACT_AT", "0.8"))

    #: **Per-project venv** — packages installed into every project's own virtualenv at creation.
    #: The agent can ``pip install`` more on demand. Override with AGENT_PROJECT_PACKAGES (comma-sep).
    agent_project_packages: list = field(default_factory=lambda: [
        p.strip() for p in os.getenv(
            "AGENT_PROJECT_PACKAGES", "django,djangorestframework,pillow,pytest,pytest-django"
        ).split(",") if p.strip()
    ])

    #: **Kam skirtas:** Nurodo benchmark užduočių katalogą.
    #: **Tikslas:** Suteikti vieningą kelią užduočių krovikliui.
    benchmark_dir: Path = ROOT / "benchmark" / "tasks"

    #: **Kam skirtas:** Nurodo rezultatų katalogą.
    #: **Tikslas:** Centralizuoti eksperimentų išvesčių saugojimo vietą.
    results_dir: Path = ROOT / "results"


settings = Settings()

__all__ = ["Settings", "settings"]
