"""RAG retriever node."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..AgentState import AgentState
from .AgentNode import AgentNode

if TYPE_CHECKING:
    from src.rag import DjangoDocsRetriever


class RetrieverNode(AgentNode):
    """
    **Purpose:** Fetches relevant Django documentation context for the task.

    **Objective:** Enrich the state with documentation excerpts before code generation.

    **Arguments:** The constructor accepts a retriever instance or `None` and an optional `k` value.

    **Returns:** A `RetrieverNode` instance whose `run()` populates `retrieved_context`.

    **Usage examples:**
    ```python
    node = RetrieverNode(retriever, k=5)
    updates = node.run(state)
    ```
    """

    #: **Purpose:** Holds the default number of retrieval results.
    #: **Objective:** Provide enough context without overloading the prompt.
    DEFAULT_K = 5

    #: **Purpose:** Holds the RAG retriever.
    #: **Objective:** Let the node reuse the same retrieval dependency.
    retriever: "DjangoDocsRetriever | None"

    #: **Purpose:** Holds the desired number of chunks.
    #: **Objective:** Let a single node instance always use the same `k` value.
    k: int

    def __init__(self, retriever: "DjangoDocsRetriever | None", k: int = DEFAULT_K) -> None:
        """
        **Purpose:** Initializes the retriever node.

        **Objective:** Determine whether the node will use RAG and how many fragments it returns.

        **Arguments:** `retriever` is the documentation-search object or `None`; `k` is the number of fragments returned.

        **Returns:** `None`.

        **Usage examples:**
        ```python
        node = RetrieverNode(retriever=None)
        ```
        """
        self.retriever = retriever
        self.k = k

    def run(self, state: AgentState) -> dict:
        """
        **Purpose:** Loads the retrieval context from the task title and description.

        **Objective:** Populate the `retrieved_context` field before moving on to coding.

        **Arguments:** `state` must contain `task_title` and `task_description`.

        **Returns:** A `dict` with the `retrieved_context` field.

        **Usage examples:**
        ```python
        updates = RetrieverNode(retriever).run(state)
        ```
        """
        if self.retriever is None:
            return {**state, "retrieved_context": ""}

        query = f"{state['task_title']}\n{state['task_description']}"
        chunks = self.retriever.retrieve(query, k=self.k)
        context = self.retriever.format_context(chunks)
        return {**state, "retrieved_context": context}


def retriever_node(state: AgentState, retriever) -> dict:
    return RetrieverNode(retriever).run(state)
