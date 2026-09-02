"""Code critique node."""
from __future__ import annotations

from src.llm import LLMBackend

from ..AgentState import AgentState
from .LLMNode import LLMNode


class CriticNode(LLMNode):
    """
    **Purpose:** Analyzes sandbox errors and produces a concise fix instruction.

    **Objective:** Turn long `stdout` and `stderr` output into focused feedback for the coder node.

    **Arguments:** The constructor inherits the `LLMNode` arguments and accepts an `LLMBackend`.

    **Returns:** A `CriticNode` instance whose `run()` populates `critic_feedback`.

    **Usage examples:**
    ```python
    critic = CriticNode(llm_backend)
    updates = critic.run(state)
    ```
    """

    #: **Purpose:** Holds the system critic prompt.
    #: **Objective:** Instruct the model to describe the errors and the fix, but not to write the code itself.
    SYSTEM = (
        "You are a senior Django code reviewer. You receive a test failure and the code. "
        "Your job is to briefly (3-6 sentences) explain what is wrong and HOW to fix it. "
        "Do not write the code itself, only the instruction."
    )

    #: **Purpose:** Holds the user prompt template for the critic node.
    #: **Objective:** Combine the generated code and the test output into a single request consistently.
    USER_TEMPLATE = (
        "Generated files:\n{files}\n\n"
        "Test output:\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}\n\n"
        "What is wrong? How to fix it?"
    )

    #: **Purpose:** Holds the generation temperature.
    #: **Objective:** Keep the critique as consistent and as little creative as possible.
    TEMPERATURE = 0.1

    #: **Purpose:** Holds the maximum critic response length.
    #: **Objective:** Limit the feedback to a practical, short format.
    MAX_TOKENS = 500

    #: **Purpose:** Holds the maximum files-text budget in the prompt.
    #: **Objective:** Prevent generating an overly long critic prompt.
    FILES_BUDGET = 4000

    #: **Purpose:** Holds the maximum `stdout` text budget.
    #: **Objective:** Include only the most important tail of the test output in the prompt.
    STDOUT_BUDGET = 2000

    #: **Purpose:** Holds the maximum `stderr` text budget.
    #: **Objective:** Include only the most significant part of the error output in the prompt.
    STDERR_BUDGET = 1000

    def __init__(
        self,
        llm: LLMBackend,
        *,
        system_prompt: str | None = None,
        user_template: str | None = None,
    ) -> None:
        """
        **Purpose:** Initializes the critic with an optional prompt override.

        **Objective:** Allow a dashboard PromptTemplate to replace the hardcoded review prompts
        while defaulting to the class constants.

        **Arguments:** `llm` is the backend; `system_prompt`/`user_template` are optional overrides.

        **Returns:** `None`.
        """
        super().__init__(llm)
        self.system_prompt = system_prompt or self.SYSTEM
        self.user_template = user_template or self.USER_TEMPLATE

    def run(self, state: AgentState) -> dict:
        """
        **Purpose:** Generates critic feedback based on the test failures.

        **Objective:** Populate `critic_feedback` and increment the iteration counter before the next coding cycle.

        **Arguments:** `state` must contain the `generated_files`, `sandbox_stdout`, and `sandbox_stderr` fields.

        **Returns:** A `dict` with `critic_feedback`, a new `iteration` value, and telemetry.

        **Usage examples:**
        ```python
        updates = CriticNode(llm).run(state)
        ```
        """
        prompt = self.user_template.format(
            files=self._format_files(state)[: self.FILES_BUDGET],
            stdout=(state.get("sandbox_stdout") or "")[-self.STDOUT_BUDGET:],
            stderr=(state.get("sandbox_stderr") or "")[-self.STDERR_BUDGET:],
        )
        resp, telemetry = self._call_llm(
            state,
            prompt,
            system=self.system_prompt,
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
        )
        return {
            **state,
            "critic_feedback": resp.text,
            "iteration": state.get("iteration", 0) + 1,
            **telemetry,
        }

    @staticmethod
    def _format_files(state: AgentState) -> str:
        """
        **Purpose:** Combines the generated files into a single text block.

        **Objective:** Give the critic model the entire current code context in a uniformly formatted form.

        **Arguments:** `state` must contain the `generated_files` map.

        **Returns:** A `str` with `# path` headers before each file.

        **Usage examples:**
        ```python
        files_text = CriticNode._format_files(state)
        ```
        """
        return "\n\n".join(
            f"# {path}\n{code}"
            for path, code in state.get("generated_files", {}).items()
        )


def critic_node(state: AgentState, llm: LLMBackend) -> dict:
    return CriticNode(llm).run(state)
