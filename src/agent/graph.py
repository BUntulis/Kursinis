"""Agento grafo fasadas."""
from __future__ import annotations

from src.llm import LLMBackend

from .AgentGraphBuilder import AgentGraphBuilder


def build_agent(
    llm: LLMBackend | None = None,
    retriever=None,
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
):
    return AgentGraphBuilder(
        llm=llm,
        retriever=retriever,
        use_rag=use_rag,
        prompt_overrides=prompt_overrides,
        sandbox_timeout=sandbox_timeout,
        enable_tools=enable_tools,
        max_tool_steps=max_tool_steps,
        shell_timeout=shell_timeout,
        interaction_handler=interaction_handler,
        allowed_tools=allowed_tools,
        require_ask=require_ask,
        max_questions=max_questions,
        design_gate=design_gate,
        finish_gate=finish_gate,
    ).build()


__all__ = ["AgentGraphBuilder", "build_agent"]
