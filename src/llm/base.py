"""LLM backend registravimo fasadas."""
from __future__ import annotations

from .LLMBackend import LLMBackend
from .LLMFactory import LLMFactory
from .LLMResponse import LLMResponse


def _make_ollama() -> LLMBackend:
    from .OllamaBackend import OllamaBackend

    return OllamaBackend()


def _make_openai() -> LLMBackend:
    from .OpenAIBackend import OpenAIBackend

    return OpenAIBackend()


if "ollama" not in LLMFactory._registry:
    LLMFactory.register("ollama", _make_ollama)
if "openai" not in LLMFactory._registry:
    LLMFactory.register("openai", _make_openai)


def get_backend(name: str | None = None) -> LLMBackend:
    return LLMFactory.create(name)
