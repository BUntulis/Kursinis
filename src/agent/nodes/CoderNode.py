"""Code generation node."""
from __future__ import annotations

from src.llm import LLMBackend

from ..AgentState import AgentState
from ..CodeBlockExtractor import CodeBlockExtractor
from .LLMNode import LLMNode


class CoderNode(LLMNode):
    """
    **Purpose:** Generates Django files from the plan, documentation, and critic feedback.

    **Objective:** Turn the task state into a concrete `path -> code` map.

    **Arguments:** The constructor accepts an `LLMBackend` and an optional `CodeBlockExtractor`.

    **Returns:** A `CoderNode` instance whose `run()` populates `generated_files`.

    **Usage examples:**
    ```python
    coder = CoderNode(llm_backend)
    updates = coder.run(state)
    ```
    """

    #: **Purpose:** Holds the system coder prompt.
    #: **Objective:** Force the model to return ONLY code blocks, each marked with its file path.
    SYSTEM = (
        "You are an expert Django developer. Your only output is code.\n\n"
        "For every file you generate, output a fenced markdown code block.\n"
        "The FIRST LINE inside every code block must be a comment with\n"
        "the file's relative path. Nothing else on that line.\n\n"
        "Format:\n"
        "```python\n"
        "# app_name/filename.py\n"
        "[full file content here]\n"
        "```\n\n"
        "Rules:\n"
        "- Output ONLY code blocks. No introduction. No explanation. No summary. "
        "No prose of any kind before or after the blocks.\n"
        "- Every expected file must have its own separate code block.\n"
        "- Import everything the code needs. Write complete, working code.\n"
        "- Use Django 5.x syntax."
    )

    #: **Purpose:** Holds the user prompt template.
    #: **Objective:** Build the entire coding request in one place.
    USER_TEMPLATE = (
        "Task: {title}\n\n"
        "{description}\n\n"
        "Plan:\n{plan}\n\n"
        "{rag_section}\n\n"
        "{feedback_section}\n\n"
        "Generate the following files: {expected_files}\n\n"
        "Each code block's first line must be `# <relative_path>`.\n\n"
        "Output ONLY the code blocks for these files: {expected_files}\n"
        "Do not write any text outside the code blocks."
    )

    #: **Purpose:** Holds the simplified single-file retry prompt.
    #: **Objective:** Recover when the model answered with prose instead of code blocks.
    RETRY_TEMPLATE = (
        "Task: {title}\n\n"
        "{description}\n\n"
        "Output ONLY a single ```python code block for the file `{first_file}`. "
        "The FIRST line inside the block must be `# {first_file}`. "
        "Do not write any explanation before or after the code block."
    )

    #: **Purpose:** Holds the RAG section template.
    #: **Objective:** Include retrieved documentation only when it is available.
    RAG_TEMPLATE = "Relevant Django documentation:\n{context}\n"

    #: **Purpose:** Holds the critic section template.
    #: **Objective:** Include fix instructions only during failed iterations.
    FEEDBACK_TEMPLATE = (
        "IMPORTANT: the previous attempt failed. Errors:\n{feedback}\n\n"
        "Fix the code taking these errors into account.\n"
    )

    #: **Purpose:** Holds the generation temperature.
    #: **Objective:** Keep a balance between creativity and stability.
    TEMPERATURE = 0.2

    #: **Purpose:** Holds the maximum number of generated tokens.
    #: **Objective:** Limit the response length and the API cost.
    MAX_TOKENS = 3000

    #: **Purpose:** Holds the code-block extractor.
    #: **Objective:** Turn the LLM markdown response into a map of generated files.
    extractor: CodeBlockExtractor

    def __init__(
        self,
        llm: LLMBackend,
        extractor: CodeBlockExtractor | None = None,
        *,
        system_prompt: str | None = None,
        user_template: str | None = None,
    ) -> None:
        """
        **Purpose:** Initializes the coder node with a backend and a code-block parser.

        **Objective:** Prepare all objects needed for code generation and for extracting it from markdown.

        **Arguments:** `llm` is the generation backend, `extractor` lets you override code-block parsing,
        and `system_prompt`/`user_template` optionally replace the hardcoded prompts (e.g. from a
        dashboard PromptTemplate) while keeping the defaults as a fallback.

        **Returns:** `None`.

        **Usage examples:**
        ```python
        coder = CoderNode(llm_backend, extractor=CodeBlockExtractor())
        coder = CoderNode(llm_backend, system_prompt="...", user_template="Task: {title}...")
        ```
        """
        super().__init__(llm)
        self.extractor = extractor or CodeBlockExtractor()
        self.system_prompt = system_prompt or self.SYSTEM
        self.user_template = user_template or self.USER_TEMPLATE

    def run(self, state: AgentState) -> dict:
        """
        **Purpose:** Generates files from the current agent state.

        **Objective:** Populate the `generated_files` field with a code map and add telemetry.

        **Arguments:** `state` must contain the task description, the plan, and optionally RAG and critic data.

        **Returns:** A `dict` with `generated_files` and telemetry fields.

        **Usage examples:**
        ```python
        updates = CoderNode(llm).run(state)
        ```
        """
        expected = state.get("expected_files", [])
        prompt = self._build_prompt(state)
        resp, telemetry = self._call_llm(
            state,
            prompt,
            system=self.system_prompt,
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
        )
        files, extract_error = self._safe_extract(resp.text, expected)

        # Post-processing safety net: if the model answered with prose and no code
        # blocks at all (or extraction failed), retry once asking only for the
        # first expected file.
        if not files:
            print(
                "[CoderNode] No code blocks extracted from the response; "
                "retrying once with a simplified single-file prompt."
            )
            retry_state = {**state, **telemetry}
            resp, telemetry = self._call_llm(
                retry_state,
                self._retry_prompt(state),
                system=self.system_prompt,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
            )
            files, extract_error = self._safe_extract(resp.text, expected)

        updates = {
            **state,
            "generated_files": files,
            **telemetry,
        }
        if not files:
            print("[CoderNode] WARNING: still no files extracted from the model response.")
            # Surface the failure so the Critic (and the dashboard log) can react to it.
            updates["coder_error"] = extract_error or (
                "The model response contained no extractable code blocks. "
                f"First 500 chars:\n{(resp.text or '')[:500]}"
            )
        return updates

    def _safe_extract(self, text: str, expected: list[str]) -> tuple[dict[str, str], str | None]:
        """
        **Purpose:** Runs the extractor without letting its ValueError abort the graph.

        **Objective:** Convert a total-extraction failure into ``({}, message)`` so ``run()`` can
        retry and ultimately hand the error text to the Critic instead of crashing the pipeline.

        **Arguments:** `text` is the raw LLM markdown and `expected` is the expected-file list.

        **Returns:** ``(files, error_message_or_None)``.
        """
        try:
            return self.extractor.extract(text, expected_files=expected), None
        except ValueError as exc:
            print(f"[CoderNode] Code-block extraction failed: {exc}")
            return {}, str(exc)

    def _retry_prompt(self, state: AgentState) -> str:
        """
        **Purpose:** Builds a minimal single-file prompt used to recover from a prose answer.

        **Objective:** Maximize the chance of getting at least one valid code block.

        **Arguments:** `state` provides the task and the `expected_files` list.

        **Returns:** A `str` retry prompt for the first expected file.
        """
        expected = state.get("expected_files", [])
        first_file = expected[0] if expected else "app/models.py"
        return self.RETRY_TEMPLATE.format(
            title=state["task_title"],
            description=state["task_description"],
            first_file=first_file,
        )

    def _build_prompt(self, state: AgentState) -> str:
        """
        **Purpose:** Assembles the full coding prompt from the state fields.

        **Objective:** Centralize prompt building in one method so it is easy to test and change.

        **Arguments:** `state` provides the task, plan, RAG, and critic information.

        **Returns:** A `str` containing the full prompt.

        **Usage examples:**
        ```python
        prompt = coder._build_prompt(state)
        ```
        """
        return self.user_template.format(
            title=state["task_title"],
            description=state["task_description"],
            plan=state.get("plan", "(none)"),
            rag_section=self._rag_section(state),
            feedback_section=self._feedback_section(state),
            expected_files=", ".join(state.get("expected_files", [])),
        )

    def _rag_section(self, state: AgentState) -> str:
        """
        **Purpose:** Prepares the RAG part of the prompt.

        **Objective:** Include the documentation context only when it actually exists.

        **Arguments:** `state` may contain a `retrieved_context` field.

        **Returns:** A `str` with the RAG section, or an empty string.

        **Usage examples:**
        ```python
        rag = coder._rag_section(state)
        ```
        """
        ctx = state.get("retrieved_context")
        return self.RAG_TEMPLATE.format(context=ctx) if ctx else ""

    def _feedback_section(self, state: AgentState) -> str:
        """
        **Purpose:** Prepares the critic-feedback part of the prompt.

        **Objective:** Include fix instructions only when they were received from a previous cycle.

        **Arguments:** `state` may contain a `critic_feedback` field.

        **Returns:** A `str` with the feedback section, or an empty string.

        **Usage examples:**
        ```python
        feedback = coder._feedback_section(state)
        ```
        """
        fb = state.get("critic_feedback")
        return self.FEEDBACK_TEMPLATE.format(feedback=fb) if fb else ""


def coder_node(state: AgentState, llm: LLMBackend) -> dict:
    return CoderNode(llm).run(state)
