"""Create starter data for the Lithuanian benchmark platform."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from dashboard.services.seed_data import seed_all


class Command(BaseCommand):
    """Seeds editable starter records."""

    help = "Sukuria pradinius modelius, užklausas, testų rinkinius ir šablonus."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--overwrite", action="store_true", help="Atnaujinti esamus pradinių duomenų įrašus.")

    def handle(self, *args, **options) -> None:
        counts = seed_all(overwrite=options["overwrite"])
        self.stdout.write(
            self.style.SUCCESS(
                "Pradiniai duomenys paruošti: "
                f"modeliai={counts['models']}, užklausos={counts['prompts']}, "
                f"testų rinkiniai={counts['test_suites']}, šablonai={counts['templates']}."
            )
        )
