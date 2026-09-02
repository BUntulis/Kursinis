"""Planning node."""
from __future__ import annotations

import re

from src.llm import LLMBackend

from ..AgentState import AgentState
from .LLMNode import LLMNode


class PlannerNode(LLMNode):
    """
    **Purpose:** Generates a high-level plan for implementing the task.

    **Objective:** Turn a benchmark task into a clear list of files, classes, and methods before code generation.

    **Arguments:** The constructor inherits the `LLMNode` arguments and accepts an `LLMBackend`.

    **Returns:** A `PlannerNode` instance whose `run()` populates the state field `plan`.

    **Usage examples:**
    ```python
    planner = PlannerNode(llm_backend)
    updates = planner.run(state)
    ```
    """

    #: **Purpose:** Holds the system planning prompt.
    #: **Objective:** Instruct the model to return a structured plan rather than code.
    SYSTEM = (
        "You are a senior Django developer planning an implementation. Break the task into a "
        "concrete plan: which files to create, and the classes, fields, and relations in each. "
        "Answer briefly with bullet points.\n\n"
        "You run autonomously — there is no human to answer questions — so after the plan, list any "
        "clarifying questions you have, then resolve each with a reasonable assumption. End your "
        "answer with a section that starts with a line containing exactly 'ASSUMPTIONS:' followed by "
        "the bullet-point assumptions you will proceed under."
    )

    #: **Purpose:** Holds the user prompt template.
    #: **Objective:** Build the planning request uniformly for every task.
    USER_TEMPLATE = (
        "Task: {title}\n\n"
        "{description}\n\n"
        "Expected files: {expected_files}\n\n"
        "Provide: (1) a plan of files and their essential elements (classes, fields, methods); "
        "(2) clarifying questions; (3) an 'ASSUMPTIONS:' section with the assumptions you will follow."
    )

    #: **Purpose:** Marker that separates the plan from the explicit assumptions block.
    ASSUMPTIONS_MARKER = "ASSUMPTIONS:"
    #: Line-anchored matcher: only an "ASSUMPTIONS:" at the start of a line counts,
    #: so the word appearing mid-prose ("document your assumptions:") never triggers a split.
    _ASSUMPTIONS_RE = re.compile(r"(?im)^[ \t]*ASSUMPTIONS:[ \t]*")

    #: **Purpose:** Holds the planning temperature.
    #: **Objective:** Keep planning as deterministic as possible.
    TEMPERATURE = 0.1

    def __init__(
        self,
        llm: LLMBackend,
        *,
        system_prompt: str | None = None,
        user_template: str | None = None,
    ) -> None:
        """
        **Purpose:** Initializes the planner with an optional prompt override.

        **Objective:** Allow a dashboard PromptTemplate to replace the hardcoded planning prompts
        while defaulting to the class constants.

        **Arguments:** `llm` is the backend; `system_prompt`/`user_template` are optional overrides.

        **Returns:** `None`.
        """
        super().__init__(llm)
        self.system_prompt = system_prompt or self.SYSTEM
        self.user_template = user_template or self.USER_TEMPLATE

    def run(self, state: AgentState) -> dict:
        """
        **Purpose:** Generates a plan from the task metadata.

        **Objective:** Populate the `plan` field that the coder node will later use.

        **Arguments:** `state` must contain `task_title`, `task_description`, and optionally `expected_files`.

        **Returns:** A `dict` with the updated `plan`, `iteration`, and telemetry fields.

        **Usage examples:**
        ```python
        updates = PlannerNode(llm).run(state)
        ```
        """
        prompt = self.user_template.format(
            title=state["task_title"],
            description=state["task_description"],
            expected_files=", ".join(state.get("expected_files", [])),
        )
        resp, telemetry = self._call_llm(
            state,
            prompt,
            system=self.system_prompt,
            temperature=self.TEMPERATURE,
        )
        plan, assumptions = self._split_assumptions(resp.text)
        return {
            **state,
            "plan": plan,
            "assumptions": assumptions,
            "iteration": state.get("iteration", 0),
            **telemetry,
        }

    @classmethod
    def _split_assumptions(cls, text: str) -> tuple[str, str]:
        """Split the planner output into (plan, assumptions) on a line-anchored ASSUMPTIONS marker."""
        text = text or ""
        match = cls._ASSUMPTIONS_RE.search(text)
        if not match:
            return text.strip(), ""
        # plan may be empty if the model led with the marker — that's fine; we do not
        # fall back to the full text (which would duplicate the assumptions into the plan).
        return text[: match.start()].strip(), text[match.end():].strip()


def planner_node(state: AgentState, llm: LLMBackend) -> dict:
    return PlannerNode(llm).run(state)
