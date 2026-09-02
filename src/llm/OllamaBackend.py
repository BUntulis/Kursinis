"""Ollama backend realizacija."""
from __future__ import annotations

import json
import re
import time

import ollama

from config import settings

from .LLMBackend import LLMBackend
from .LLMResponse import LLMResponse


class OllamaBackend(LLMBackend):
    """
    **Kam skirtas:** Įgyvendina `LLMBackend` kontraktą per Ollama HTTP klientą.

    **Tikslas:** Leisti projektui naudoti lokaliai arba nuotoliniu būdu veikiančią Ollama instanciją kaip teksto generavimo backend'ą.

    **Argumentai:** Konstruktorius priima pasirenkamą modelio vardą ir host adresą.

    **Grąžinama:** `OllamaBackend` instanciją, galinčią vykdyti `complete()` užklausas.

    **Panaudojimo pavyzdžiai:**
    ```python
    backend = OllamaBackend(model="qwen2.5-coder:7b-instruct")
    response = backend.complete("Parašyk Django modelį")
    ```
    """

    #: **Kam skirtas:** Saugo backend identifikatorių.
    #: **Tikslas:** Leisti fabrikui ir rezultatų suvestinėms atpažinti šį backend'ą.
    name = "ollama"

    #: **Kam skirtas:** Saugo aktyvų modelio vardą.
    #: **Tikslas:** Užtikrinti, kad kiekviena užklausa būtų siunčiama tam pačiam pasirinktai modeliui.
    model: str

    #: **Kam skirtas:** Saugo Ollama kliento objektą.
    #: **Tikslas:** Pakartotinai naudoti vieną klientą kelioms užklausoms.
    client: ollama.Client

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        """
        **Kam skirtas:** Inicializuoja Ollama klientą ir pasirinktą modelį.

        **Tikslas:** Paruošti backend'ą chat užklausoms be papildomo konfigūravimo kiekvieno iškvietimo metu.

        **Argumentai:** `model` leidžia override'inti numatytą modelį, o `host` nurodo Ollama serverio adresą.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        backend = OllamaBackend(host="http://localhost:11434")
        ```
        """
        # IMPORTANT: pass an explicit timeout. The ollama/httpx client defaults to NO timeout (waits
        # forever), so a slow generation (e.g. a large model running on CPU) or a hung daemon would
        # block the agent thread indefinitely — the chat freezes on "Analyzing…/Starting the agent…".
        # With a timeout the call raises instead, the turn is marked FAILED, and the Retry / change-
        # model bar appears. Tune via OLLAMA_TIMEOUT_SEC.
        self.client = ollama.Client(host=host or settings.ollama_host, timeout=settings.ollama_timeout_sec)
        self.model = self._resolve_model(model or settings.ollama_model)
        #: Optional zero-arg callable checked between streamed chunks; return True to abort the
        #: current generation with LLMCancelled (the chat Stop button sets this per turn).
        self.cancel_check = None

    #: **Purpose:** Coder-specialised model families — tier 0 of the "best installed" ranking.
    #: **Objective:** Pick a sensible installed model when the configured one is missing.
    FALLBACK_PREFERENCES = (
        "qwen3-coder",
        "qwen2.5-coder",
        "codeqwen",
        "deepseek-coder-v2",
        "deepseek-coder",
        "codestral",
        "devstral",
        "yi-coder",
        "codegemma",
        "codellama",
        "starcoder2",
        "starcoder",
        "granite-code",
        "opencoder",
    )

    #: **Purpose:** Strong general/instruct families — tier 1 (after coder models, before unknowns).
    GENERAL_FAMILIES = (
        "qwen3", "qwq", "qwen2.5", "qwen2",
        "llama4", "llama3", "deepseek-r1",
        "mistral", "mixtral", "gemma3", "gemma2",
        "phi4", "phi3", "granite3", "command-r", "glm4",
    )

    #: **Purpose:** Model base names known to support native Ollama tool/function calling.
    #: **Objective:** Decide per-model whether to send native ``tools=`` or fall back to the
    #: universal text tool-protocol (e.g. codellama/deepseek-coder/starcoder are NOT tool-trained).
    TOOL_CAPABLE_BASES = (
        "llama3.1", "llama3.2", "llama3.3", "llama4",
        "mistral", "mistral-nemo", "mixtral",
        "qwen2.5", "qwen2.5-coder", "qwen3", "qwq",
        "firefunction", "command-r", "granite3", "smollm2",
    )

    @property
    def supports_tools(self) -> bool:
        """True when the resolved model base is known to support native tool calling."""
        base = (self.model or "").split(":", 1)[0].lower()
        return any(base == name or base.startswith(name) for name in self.TOOL_CAPABLE_BASES)

    def _resolve_model(self, requested: str) -> str:
        """
        **Kam skirtas:** Patikrina, ar pageidaujamas modelis įdiegtas, kitaip parenka atsarginį.

        **Tikslas:** Apsaugoti agento vykdymą nuo nutrūkimo, kai sukonfigūruotas modelis nėra ištrauktas.

        **Argumentai:** `requested` yra pageidaujamo modelio vardas.

        **Grąžinama:** Įdiegto modelio vardą (arba `requested`, jei patikrinti nepavyko).
        """
        try:
            installed = self._installed_models()
        except Exception as exc:  # pragma: no cover - network/CLI failure
            print(f"[OllamaBackend] Could not list installed models ({exc}); using '{requested}'.")
            return requested
        if not installed or requested in installed:
            return requested
        # The EXACT tag isn't pulled. Prefer another tag of the same base/family (e.g. the
        # configured `qwen2.5-coder:7b-instruct` → an installed `qwen2.5-coder:14b`) so we honour
        # the chosen model family. Returning `requested` here (because the base matches) would send
        # a tag Ollama doesn't have and 404 at chat time — only the EXACT tag is actually runnable.
        base = requested.split(":", 1)[0].lower()
        same_base = [c for c in installed if c.split(":", 1)[0].lower() == base]
        if same_base:
            chosen = min(same_base, key=self._tag_rank)  # the BIGGEST tag of the chosen family
            print(f"[OllamaBackend] Tag '{requested}' is not installed; using same-family '{chosen}'.")
            return chosen
        fallback = self._pick_fallback(installed)
        print(
            f"[OllamaBackend] Model '{requested}' is not installed; "
            f"falling back to '{fallback}'. Installed: {sorted(installed)}"
        )
        return fallback

    def _installed_models(self) -> list[str]:
        """Return the list of installed Ollama model tags (handles old/new client shapes)."""
        data = self.client.list()
        raw = getattr(data, "models", None)
        if raw is None and isinstance(data, dict):
            raw = data.get("models", [])
        names: list[str] = []
        for item in raw or []:
            name = getattr(item, "model", None) or getattr(item, "name", None)
            if not name and isinstance(item, dict):
                name = item.get("model") or item.get("name")
            if name:
                names.append(str(name))
        return names

    @classmethod
    def list_installed(cls, host: str | None = None, timeout: float = 2.0) -> list[str]:
        """Installed Ollama model tags (sorted) — for UIs that let the user pick a runnable model.

        Best-effort + BOUNDED: the ollama/httpx client defaults to no timeout (waits forever), so a
        hung daemon would stall the caller; we pass an explicit timeout and return [] on any failure.
        """
        inst = cls.__new__(cls)
        try:
            inst.client = ollama.Client(host=host or settings.ollama_host, timeout=timeout)
            return sorted(inst._installed_models())
        except Exception:  # pragma: no cover - network/CLI failure/timeout
            return []

    #: Parses the parameter size out of a tag (``qwen2.5-coder:14b`` → 14, ``…:7b-instruct`` → 7).
    _SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
    #: Mixture-of-experts sizes (``mixtral:8x7b`` → 8×7 = 56 effective parameters).
    _MOE_RE = re.compile(r"(\d+)x(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

    #: Tags that are not chat/code generators (never a sensible "best" pick).
    _NON_CHAT_MARKERS = ("embed", "rerank", "bge-", "minilm")
    #: Vision-first model bases — usable, but never the best pick for a CODING agent.
    _VISION_MARKERS = ("llava", "moondream", "bakllava", "vision")

    @classmethod
    def _tag_size(cls, tag: str) -> float:
        """Effective parameter size parsed from the tag (0 when the tag carries no size)."""
        m = cls._MOE_RE.search(tag)
        if m:
            return float(m.group(1)) * float(m.group(2))
        m = cls._SIZE_RE.search(tag)
        return float(m.group(1)) if m else 0.0

    @classmethod
    def _tag_rank(cls, tag: str) -> tuple:
        """Sort key: best model first — (tier, -size, tag).

        Tier 0 = coder families (FALLBACK_PREFERENCES), tier 1 = strong general families,
        tier 2 = unknown, tier 3 = vision models. A known-TINY model (under 3B) loses its
        family privilege (+2 tiers): a 1.5b coder draft model must not outrank a 30b model.
        """
        base = tag.split(":", 1)[0].lower()
        size = cls._tag_size(tag)
        if base.endswith("vl") or any(m in base for m in cls._VISION_MARKERS):
            tier = 3
        elif any(base.startswith(f) for f in cls.FALLBACK_PREFERENCES):
            tier = 0
        elif any(base.startswith(f) for f in cls.GENERAL_FAMILIES):
            tier = 1
        else:
            tier = 2
        if 0 < size < 3:
            tier += 2
        return (tier, -size, tag)

    @classmethod
    def _pick_fallback(cls, installed: list[str]) -> str:
        """The BEST installed model: preferred coder family first, then largest size."""
        usable = [t for t in installed if not any(m in t.lower() for m in cls._NON_CHAT_MARKERS)]
        pool = usable or installed
        return min(pool, key=cls._tag_rank)

    @classmethod
    def best_installed(cls, host: str | None = None, timeout: float = 2.0) -> str:
        """The best installed model tag overall (coder families first, biggest size wins) — drives
        the chat's "Auto" model choice. Returns "" when Ollama is unreachable or nothing is pulled.
        """
        installed = cls.list_installed(host=host, timeout=timeout)
        return cls._pick_fallback(installed) if installed else ""

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        **Kam skirtas:** Išsiunčia vieną užklausą į Ollama ir sugrąžina standartizuotą atsakymą.

        **Tikslas:** Paversti Ollama specifinį API formatą į projekto `LLMResponse` struktūrą.

        **Argumentai:** `prompt` yra pagrindinis tekstas, `system` nurodo sisteminį kontekstą, `temperature` valdo kūrybiškumą, o `max_tokens` riboja generaciją.

        **Grąžinama:** `LLMResponse` objektą su tekstu, tokenais, latencija ir modelio vardu.

        **Panaudojimo pavyzdžiai:**
        ```python
        resp = backend.complete("Sugeneruok serializerį", max_tokens=512)
        print(resp.text)
        ```
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.perf_counter()
        content, _calls, p_tok, c_tok = self._stream_chat({
            "model": self.model,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        })
        latency = time.perf_counter() - start

        return LLMResponse(
            text=content,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            latency_sec=latency,
            model=self.model,
        )

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        **Kam skirtas:** Multi-turn pokalbis su pasirenkamais native įrankiais.

        **Tikslas:** Leisti tool-using agentui vesti pokalbį; native ``tools=`` siunčiama tik
        tool-capable modeliams (`supports_tools`), kiti modeliai naudoja tą patį `messages`
        srautą be įrankių (agentas tuomet parsina tekstinį tool-protokolą).

        **Argumentai:** `messages` — Ollama chat žinučių sąrašas; `tools` — įrankių schemos.

        **Grąžinama:** `LLMResponse` su tekstu ir (jei modelis grąžino) `tool_calls`.
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools and self.supports_tools:
            kwargs["tools"] = tools

        from .LLMBackend import LLMCancelled

        start = time.perf_counter()
        try:
            content, raw_calls, p_tok, c_tok = self._stream_chat(kwargs)
        except LLMCancelled:
            raise
        except Exception:
            if "tools" not in kwargs:
                raise
            # Older Ollama daemons reject stream=True together with tools — fall back to one
            # blocking call (not Stop-abortable mid-generation, but native tool models keep working).
            resp = self.client.chat(**kwargs)
            message = resp["message"]
            content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
            raw_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
            p_tok = int(resp.get("prompt_eval_count", 0) or 0)
            c_tok = int(resp.get("eval_count", 0) or 0)
        latency = time.perf_counter() - start

        tool_calls = self._parse_tool_calls(raw_calls)
        return LLMResponse(
            text=content or "",
            tool_calls=tool_calls,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            latency_sec=latency,
            model=self.model,
        )

    def _stream_chat(self, kwargs) -> tuple[str, list, int, int]:
        """Consume a STREAMING chat call; returns (content, raw_tool_calls, prompt_tok, completion_tok).

        Streaming exists for one reason: ``self.cancel_check`` is consulted between chunks, so the
        chat Stop button aborts the CURRENT generation in ~a second instead of waiting out the whole
        80s+ blocking call. (Prompt-eval time before the first chunk is still not abortable.)"""
        from .LLMBackend import LLMCancelled

        content_parts: list = []
        raw_calls: list = []
        p_tok = c_tok = 0
        stream = self.client.chat(**kwargs, stream=True)
        try:
            for chunk in stream:
                if self.cancel_check is not None and self.cancel_check():
                    raise LLMCancelled("stopped by user")
                msg = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                if msg is not None:
                    piece = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                    if piece:
                        content_parts.append(piece)
                    calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
                    if calls:
                        raw_calls.extend(calls)
                pe = chunk.get("prompt_eval_count") if isinstance(chunk, dict) else getattr(chunk, "prompt_eval_count", None)
                ce = chunk.get("eval_count") if isinstance(chunk, dict) else getattr(chunk, "eval_count", None)
                if pe:
                    p_tok = int(pe)
                if ce:
                    c_tok = int(ce)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return "".join(content_parts), raw_calls, p_tok, c_tok

    @staticmethod
    def _parse_tool_calls(raw_calls) -> list:
        """Normalise Ollama native tool calls to ``[{"name", "arguments"}]``."""
        calls: list = []
        for item in raw_calls or []:
            fn = item.get("function") if isinstance(item, dict) else getattr(item, "function", None)
            if fn is None:
                continue
            name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append({"name": name, "arguments": args or {}})
        return calls
