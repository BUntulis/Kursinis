"""Pipeline veiksmų Django CLI komanda."""
from __future__ import annotations

import subprocess

from django.core.management.base import BaseCommand, CommandParser

from dashboard.JobCommandBuilder import JobCommandBuilder
from dashboard.JobRunner import JobRunner


class PipelineCommand(BaseCommand):
    """
    Kam skirtas:
    Vykdyti pipeline veiksmus per `manage.py`.

    Tikslas:
    Suteikti tą patį veiksmų rinkinį, kurį turi Web sąsaja, ir CLI režimu.

    Argumentai:
    Argumentus aprašo `add_arguments()`.

    Grąžinama:
    Django management komandos instancija.

    Panaudojimo pavyzdžiai:
    ```powershell
    py -3 manage.py pipeline --action run_agent --task 001_blog_model
    ```
    """

    help = "Paleidžia pipeline veiksmus: run_agent, run_baseline, pytest, finetune ir kt."

    def add_arguments(self, parser: CommandParser) -> None:
        """Aprašo CLI argumentus."""
        parser.add_argument("--action", required=True)
        parser.add_argument("--background", action="store_true")
        parser.add_argument("--task", default="")
        parser.add_argument("--tasks", default="")
        parser.add_argument("--tag", default="")
        parser.add_argument("--backend", default="")
        parser.add_argument("--model", default="")
        parser.add_argument("--no-rag", action="store_true")
        parser.add_argument("--max-iter", type=int, default=0)
        parser.add_argument("--target", default="")
        parser.add_argument("--input", default="")
        parser.add_argument("--output", default="")
        parser.add_argument("--source", default="")
        parser.add_argument("--repos", default="")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--depth", type=int, default=0)

    def handle(self, *args, **options) -> None:
        """Paleidžia pasirinktą veiksmą sinchroniškai arba fone."""
        payload = {
            "task": options.get("task") or "",
            "tasks": options.get("tasks") or "",
            "tag": options.get("tag") or "",
            "backend": options.get("backend") or "",
            "model": options.get("model") or "",
            "no_rag": bool(options.get("no_rag")),
            "max_iter": options.get("max_iter") or "",
            "target": options.get("target") or "",
            "input": options.get("input") or "",
            "output": options.get("output") or "",
            "source": options.get("source") or "",
            "repos": options.get("repos") or "",
            "limit": options.get("limit") or "",
            "depth": options.get("depth") or "",
        }
        if options["background"]:
            job = JobRunner().start(options["action"], payload)
            self.stdout.write(self.style.SUCCESS(f"Paleista fone. Job ID: {job.id}"))
            return

        label, command, _params = JobCommandBuilder().build(options["action"], payload)
        self.stdout.write(f"==> {label}")
        self.stdout.write("$ " + " ".join(command))
        raise SystemExit(subprocess.call(command))
