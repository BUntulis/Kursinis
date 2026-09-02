"""Pipeline veiksmų komandų konstruktorius."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


class JobCommandBuilder:
    """
    Kam skirtas:
    Paversti Web arba CLI veiksmą į konkrečią subprocess komandą.

    Tikslas:
    Užtikrinti, kad Web mygtukai ir Django management komanda paleistų tas pačias esamas projekto komandas.

    Argumentai:
    Konstruktorius priima projekto šaknies kelią.

    Grąžinama:
    `JobCommandBuilder` instancija.

    Panaudojimo pavyzdžiai:
    ```python
    label, command, params = JobCommandBuilder().build("run_agent", {"task": "001_blog_model"})
    ```
    """

    #: Projekto šaknis.
    root: Path

    def __init__(self, root: Path | None = None) -> None:
        """Inicializuoja komandų konstruktorių."""
        self.root = root or Path(__file__).resolve().parent.parent

    def build(self, action: str, payload: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        """
        Kam skirtas:
        Sukonstruoti komandą pagal veiksmą.

        Tikslas:
        Centralizuoti visų pipeline veiksmų argumentų map'inimą.

        Argumentai:
        `action` nurodo veiksmą, o `payload` turi Web arba CLI perduotus parametrus.

        Grąžinama:
        Trijų reikšmių tuple: `label`, `command`, `params`.

        Panaudojimo pavyzdžiai:
        ```python
        label, command, params = builder.build("pytest", {})
        ```
        """
        builders = {
            "check_ollama": self._check_ollama,
            "run_benchmark": self._run_benchmark,
            "run_baseline": self._run_baseline,
            "run_agent": self._run_agent,
            "generate_report": self._generate_report,
            "pytest": self._pytest,
            "build_dataset": self._build_dataset,
            "scrape_repos": self._scrape_repos,
            "index_docs": self._index_docs,
            "finetune": self._finetune,
        }
        if action not in builders:
            raise ValueError(f"Nežinomas veiksmas: {action}")
        label, command = builders[action](payload)
        return label, [str(part) for part in command], payload

    def _script(self, name: str) -> Path:
        """Grąžina kelią iki `scripts/` failo."""
        return self.root / "scripts" / name

    def _check_ollama(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria Ollama patikros komandą."""
        return "Ollama patikra", [sys.executable, self._script("check_ollama.py")]

    def _run_benchmark(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria vidinį benchmark orkestravimo darbą."""
        benchmark_id = payload.get("benchmark_id")
        if not benchmark_id:
            raise ValueError("Trūksta `benchmark_id`.")
        return f"Benchmark #{benchmark_id}", ["internal:run_benchmark", str(benchmark_id)]

    def _run_baseline(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria baseline benchmark paleidimo komandą."""
        command: list[Any] = [sys.executable, self._script("run_baseline.py")]
        command.extend(["--tasks", payload.get("tasks") or payload.get("task") or "all"])
        if payload.get("backend"):
            command.extend(["--backend", payload["backend"]])
        if payload.get("model"):
            command.extend(["--model", payload["model"]])
        if payload.get("output"):
            command.extend(["--output", payload["output"]])
        return "Baseline benchmark", command

    def _run_agent(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria agentinio benchmark paleidimo komandą."""
        command: list[Any] = [sys.executable, self._script("run_agent.py")]
        if payload.get("task"):
            command.extend(["--task", payload["task"]])
        else:
            command.extend(["--tasks", payload.get("tasks") or "all"])
        if payload.get("no_rag"):
            command.append("--no-rag")
        if payload.get("max_iter"):
            command.extend(["--max-iter", str(payload["max_iter"])])
        if payload.get("tag"):
            command.extend(["--tag", payload["tag"]])
        if payload.get("output"):
            command.extend(["--output", payload["output"]])
        return "Agentinis benchmark", command

    def _generate_report(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria ataskaitos generavimo komandą."""
        return "Ataskaitos generavimas", [sys.executable, self._script("generate_report.py")]

    def _pytest(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria testų paleidimo komandą."""
        target = payload.get("target") or "tests"
        return "Testų paleidimas", [sys.executable, "-m", "pytest", target, "-q"]

    def _build_dataset(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria instruction dataset generavimo komandą."""
        command: list[Any] = [sys.executable, "-m", "src.dataset.build_dataset"]
        command.extend(["--input", payload.get("input") or "data/raw"])
        command.extend(["--output", payload.get("output") or "data/django_instructions.jsonl"])
        if payload.get("limit"):
            command.extend(["--limit", str(payload["limit"])])
        return "Dataset kūrimas", command

    def _scrape_repos(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria GitHub repo scraping komandą."""
        command: list[Any] = [sys.executable, "-m", "src.scraper.github_scraper"]
        command.extend(["--output", payload.get("output") or "data/raw"])
        repos = payload.get("repos") or []
        if isinstance(repos, str):
            repos = [repo.strip() for repo in repos.split(",") if repo.strip()]
        if repos:
            command.append("--repos")
            command.extend(repos)
        if payload.get("depth"):
            command.extend(["--depth", str(payload["depth"])])
        return "GitHub scraping", command

    def _index_docs(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria RAG dokumentacijos indeksavimo komandą."""
        source = payload.get("source") or "django-src/docs"
        output = payload.get("output") or ".chroma"
        return "RAG indeksavimas", [sys.executable, "-m", "src.rag.indexer", "--source", source, "--output", output]

    def _finetune(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        """Sukuria LoRA fine-tuning komandą."""
        command: list[Any] = [sys.executable, self.root / "training" / "lora_finetune.py"]
        if payload.get("dataset"):
            command.extend(["--dataset", payload["dataset"]])
        if payload.get("base"):
            command.extend(["--base", payload["base"]])
        if payload.get("output"):
            command.extend(["--output", payload["output"]])
        for key, cli_name in {
            "epochs": "--epochs",
            "batch": "--batch",
            "grad_accum": "--grad-accum",
            "lr": "--lr",
            "max_seq_len": "--max-seq-len",
            "lora_r": "--lora-r",
            "lora_alpha": "--lora-alpha",
            "lora_dropout": "--lora-dropout",
            "seed": "--seed",
        }.items():
            if payload.get(key) not in (None, ""):
                command.extend([cli_name, str(payload[key])])
        return "LoRA fine-tuning", command
