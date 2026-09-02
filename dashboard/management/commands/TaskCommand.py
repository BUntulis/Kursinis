"""Benchmark užduočių Django CLI komanda."""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandParser

from dashboard.TaskStore import TaskStore


class TaskCommand(BaseCommand):
    """
    Kam skirtas:
    Valdyti benchmark užduotis per `manage.py`.

    Tikslas:
    Leisti list/get/save/delete veiksmus atlikti CLI režimu.

    Argumentai:
    Argumentus aprašo `add_arguments()`.

    Grąžinama:
    Django management komandos instancija.

    Panaudojimo pavyzdžiai:
    ```powershell
    py -3 manage.py tasks list
    ```
    """

    help = "Valdo benchmark YAML užduotis."

    def add_arguments(self, parser: CommandParser) -> None:
        """Aprašo subkomandas užduočių valdymui."""
        subparsers = parser.add_subparsers(dest="command", required=True)
        subparsers.add_parser("list")

        get_parser = subparsers.add_parser("get")
        get_parser.add_argument("task_id")

        delete_parser = subparsers.add_parser("delete")
        delete_parser.add_argument("task_id")

        save_parser = subparsers.add_parser("save")
        save_parser.add_argument("--id", required=True)
        save_parser.add_argument("--title", required=True)
        save_parser.add_argument("--difficulty", default="unknown")
        save_parser.add_argument("--category", default="general")
        save_parser.add_argument("--description", required=True)
        save_parser.add_argument("--expected-files", default="")
        save_parser.add_argument("--reference-tests-file", required=True)
        save_parser.add_argument("--original-id", default="")

    def handle(self, *args, **options) -> None:
        """Įvykdo pasirinktą užduočių valdymo komandą."""
        store = TaskStore()
        command = options["command"]
        if command == "list":
            for task in store.list():
                self.stdout.write(f"{task['id']} | {task.get('difficulty')} | {task.get('title')}")
            return
        if command == "get":
            self.stdout.write(str(store.get(options["task_id"])))
            return
        if command == "delete":
            store.delete(options["task_id"])
            self.stdout.write(self.style.SUCCESS("Ištrinta."))
            return
        reference_tests = Path(options["reference_tests_file"]).read_text(encoding="utf-8")
        expected_files = [
            value.strip()
            for value in options["expected_files"].split(",")
            if value.strip()
        ]
        saved = store.save(
            {
                "id": options["id"],
                "title": options["title"],
                "difficulty": options["difficulty"],
                "category": options["category"],
                "description": options["description"],
                "expected_files": expected_files,
                "reference_tests": reference_tests,
            },
            original_id=options["original_id"] or None,
        )
        self.stdout.write(self.style.SUCCESS(f"Išsaugota: {saved['id']}"))
