"""Baseline eksperimento runner klasė."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import ClassVar

from config import settings
from src.agent.extract import CodeBlockExtractor
from src.benchmark import BenchmarkTask
from src.eval.sandbox import DjangoSandbox
from src.llm import LLMBackend, get_backend


class BaselineRunner:
    """
    **Kam skirtas:** Paleidžia vieną LLM iškvietimą ir sandbox tikrinimą be agentinio ciklo.

    **Tikslas:** Suteikti kontrolinę bazę agentinio pipeline'o palyginimui.

    **Argumentai:** Konstruktorius priima backend vardą arba LLM instanciją, taip pat pasirenkamą sandbox ir extractor'į.

    **Grąžinama:** `BaselineRunner` instanciją, galinčią vykdyti benchmark užduotis.

    **Panaudojimo pavyzdžiai:**
    ```python
    runner = BaselineRunner(backend_name="ollama")
    result = runner.run_one(task)
    ```
    """

    #: **Kam skirtas:** Saugo sisteminį prompt'ą baseline modeliui.
    #: **Tikslas:** Liepti modeliui generuoti tik failų kodo blokus.
    SYSTEM: ClassVar[str] = (
        "Tu esi Django programuotojas. Sugeneruok prašomus failus.\n"
        "Kiekvienam failui — atskiras ```python blokas su pirmąja eilute "
        "`# <relative_path>`.\n"
        "Be ekstra paaiškinimų."
    )

    #: **Kam skirtas:** Saugo vartotojo prompt'o šabloną.
    #: **Tikslas:** Vienodai sudėti benchmark užduotį į vieną generavimo užklausą.
    USER_TEMPLATE: ClassVar[str] = (
        "Užduotis: {title}\n\n{description}\n\nSugeneruok šiuos failus: {expected_files}"
    )

    #: **Kam skirtas:** Saugo generacijos temperatūrą.
    #: **Tikslas:** Išlaikyti stabilų baseline modelio elgesį.
    TEMPERATURE = 0.2

    #: **Kam skirtas:** Saugo maksimalų generuojamų tokenų kiekį.
    #: **Tikslas:** Apriboti atsakymo ilgį ir API sąnaudas.
    MAX_TOKENS = 3000

    #: **Kam skirtas:** Saugo kiek `stdout` uodegos išsaugoti.
    #: **Tikslas:** Neleisti rezultatų JSON failui per daug išsipūsti.
    STDOUT_TAIL_BYTES = 2000

    #: **Kam skirtas:** Saugo kiek `stderr` uodegos išsaugoti.
    #: **Tikslas:** Neleisti rezultatų JSON failui per daug išsipūsti.
    STDERR_TAIL_BYTES = 1000

    #: **Kam skirtas:** Saugo LLM backend'ą.
    #: **Tikslas:** Vykdyti visas baseline užklausas per vieną pasirinktą modelį.
    llm: LLMBackend

    #: **Kam skirtas:** Saugo sandbox objektą.
    #: **Tikslas:** Leisti testuose arba eksperimentuose pakeisti vykdymo aplinką.
    sandbox: DjangoSandbox

    #: **Kam skirtas:** Saugo kodo blokų ištraukiklį.
    #: **Tikslas:** Paversti modelio markdown atsakymą į failų žemėlapį.
    extractor: CodeBlockExtractor

    #: **Kam skirtas:** Saugo visų paleidimų rezultatus.
    #: **Tikslas:** Leisti po kelių užduočių suformuoti bendrą suvestinę.
    _results: list[dict]

    def __init__(
        self,
        backend_name: str | None = None,
        llm: LLMBackend | None = None,
        sandbox: DjangoSandbox | None = None,
        extractor: CodeBlockExtractor | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja baseline runner'į su pasirinktu backend'u ir priklausomybėmis.

        **Tikslas:** Paruošti objektą kelių benchmark užduočių vykdymui be agentinio ciklo.

        **Argumentai:** `backend_name` leidžia nurodyti backend'ą vardu, `llm` leidžia perduoti jau sukurtą instanciją, `sandbox` leidžia pakeisti testų vykdymą, o `extractor` leidžia pakeisti markdown parsing'ą.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        runner = BaselineRunner(backend_name="openai")
        ```
        """
        self.llm = llm or get_backend(backend_name)
        self.sandbox = sandbox or DjangoSandbox()
        self.extractor = extractor or CodeBlockExtractor()
        self._results = []

    def run_one(self, task: BenchmarkTask) -> dict:
        """
        **Kam skirtas:** Paleidžia baseline modelį prieš vieną benchmark užduotį.

        **Tikslas:** Sugeneruoti vienos užduoties rezultatą be papildomų planavimo ar taisymo ciklų.

        **Argumentai:** `task` yra benchmark užduotis.

        **Grąžinama:** `dict` su generacijos ir sandbox rezultatais.

        **Panaudojimo pavyzdžiai:**
        ```python
        result = runner.run_one(task)
        ```
        """
        prompt = self.USER_TEMPLATE.format(
            title=task.title,
            description=task.description,
            expected_files=", ".join(task.expected_files),
        )

        started = time.perf_counter()
        resp = self.llm.complete(
            prompt,
            system=self.SYSTEM,
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
        )
        gen_time = time.perf_counter() - started

        files = self.extractor.extract(resp.text)
        sandbox_result = self.sandbox.run(task, files)

        result = {
            "task_id": task.id,
            "passed": sandbox_result.passed,
            "tests_passed": sandbox_result.tests_passed,
            "tests_failed": sandbox_result.tests_failed,
            "generation_time_sec": gen_time,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "model": resp.model,
            "generated_files": files,
            "stdout_tail": sandbox_result.stdout[-self.STDOUT_TAIL_BYTES:],
            "stderr_tail": sandbox_result.stderr[-self.STDERR_TAIL_BYTES:],
        }
        self._results.append(result)
        return result

    def run_many(self, tasks: list[BenchmarkTask]) -> list[dict]:
        """
        **Kam skirtas:** Paleidžia baseline modelį per kelių užduočių sąrašą.

        **Tikslas:** Sukaupti visų benchmark užduočių rezultatus vienam eksperimento failui.

        **Argumentai:** `tasks` yra benchmark užduočių sąrašas.

        **Grąžinama:** `list[dict]` su visų paleidimų rezultatais.

        **Panaudojimo pavyzdžiai:**
        ```python
        results = runner.run_many(tasks)
        ```
        """
        for task in tasks:
            print(f"--- {task.id}: {task.title} ---")
            result = self.run_one(task)
            print(
                f"  passed={result['passed']} "
                f"tests={result['tests_passed']}/"
                f"{result['tests_passed'] + result['tests_failed']} "
                f"time={result['generation_time_sec']:.1f}s"
            )
        return self._results

    def save(self, output: Path, backend_label: str) -> dict:
        """
        **Kam skirtas:** Išsaugo baseline eksperimento rezultatus į JSON failą.

        **Tikslas:** Turėti lengvai palyginamą failą su visais benchmark paleidimais.

        **Argumentai:** `output` nurodo išsaugojimo vietą, o `backend_label` identifikuoja naudotą backend'ą.

        **Grąžinama:** `dict` su išsaugota suvestine.

        **Panaudojimo pavyzdžiai:**
        ```python
        summary = runner.save(Path("results/baseline.json"), "ollama")
        ```
        """
        summary = {
            "backend": backend_label,
            "total": len(self._results),
            "passed": sum(1 for item in self._results if item["passed"]),
            "results": self._results,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    @staticmethod
    def override_model_via_env(backend: str | None, model: str | None) -> None:
        """
        **Kam skirtas:** Per aplinkos kintamuosius override'ina modelio vardą iš CLI.

        **Tikslas:** Suderinti `--model` argumentą su esama konfigūracijos schema.

        **Argumentai:** `backend` nurodo backend tipą, o `model` nurodo naują modelio vardą.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        BaselineRunner.override_model_via_env("ollama", "qwen2.5-coder:7b")
        ```
        """
        if not model:
            return
        if (backend or settings.llm_backend) == "ollama":
            os.environ["OLLAMA_MODEL"] = model
        else:
            os.environ["OPENAI_MODEL"] = model
