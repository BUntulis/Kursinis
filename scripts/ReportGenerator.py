"""Ataskaitos generavimo fasado klasė."""
from __future__ import annotations

from pathlib import Path

from config import settings

from scripts.CSVReportWriter import CSVReportWriter
from scripts.MarkdownReportWriter import MarkdownReportWriter
from scripts.TaskMetadata import TaskMetadata
from scripts._RunsLoader import _RunsLoader


class ReportGenerator:
    """
    **Kam skirtas:** Apjungia run duomenų nuskaitymą ir CSV bei markdown ataskaitų kūrimą.

    **Tikslas:** Pateikti vieną aukšto lygio API visam rezultatų ataskaitos generavimo procesui.

    **Argumentai:** Konstruktorius priima pasirenkamą run kroviklį ir abiejų formatų writer'ius.

    **Grąžinama:** `ReportGenerator` instanciją, galinčią vykdyti `generate()`.

    **Panaudojimo pavyzdžiai:**
    ```python
    generator = ReportGenerator()
    generator.generate()
    ```
    """

    #: **Kam skirtas:** Saugo run duomenų kroviklį.
    #: **Tikslas:** Leisti pakeisti duomenų šaltinį testuose ar kitose integracijose.
    runs_loader: _RunsLoader

    #: **Kam skirtas:** Saugo CSV writer'į.
    #: **Tikslas:** Leisti pakeisti CSV formatavimo strategiją nekeičiat generatoriaus logikos.
    csv_writer: CSVReportWriter

    #: **Kam skirtas:** Saugo markdown writer'į.
    #: **Tikslas:** Leisti pakeisti markdown formatavimo strategiją nekeičiat generatoriaus logikos.
    md_writer: MarkdownReportWriter

    def __init__(
        self,
        runs_loader: _RunsLoader | None = None,
        csv_writer: CSVReportWriter | None = None,
        md_writer: MarkdownReportWriter | None = None,
    ) -> None:
        """
        **Kam skirtas:** Inicializuoja ataskaitos generatorių su visomis priklausomybėmis.

        **Tikslas:** Paruošti objektą vienam ar keliems ataskaitų generavimo ciklams.

        **Argumentai:** `runs_loader`, `csv_writer` ir `md_writer` leidžia įšvirkšti alternatyvias realizacijas.

        **Grąžinama:** `None`.

        **Panaudojimo pavyzdžiai:**
        ```python
        generator = ReportGenerator()
        ```
        """
        self.runs_loader = runs_loader or _RunsLoader()
        self.csv_writer = csv_writer or CSVReportWriter()
        self.md_writer = md_writer or MarkdownReportWriter()

    def generate(
        self,
        csv_path: Path | None = None,
        md_path: Path | None = None,
    ) -> tuple[Path, Path] | None:
        """
        **Kam skirtas:** Sugeneruoja CSV ir markdown ataskaitas iš visų rezultatų run'ų.

        **Tikslas:** Vienoje vietoje įvykdyti visą ataskaitos kūrimo pipeline'ą.

        **Argumentai:** `csv_path` ir `md_path` leidžia override'inti numatytas išvesties vietas.

        **Grąžinama:** `tuple[Path, Path]` su sugeneruotų failų keliais arba `None`, jei rezultatų nerasta.

        **Panaudojimo pavyzdžiai:**
        ```python
        paths = ReportGenerator().generate()
        ```
        """
        runs = self.runs_loader.load()
        if not runs:
            print("Nerasta rezultatų. Paleisk run_baseline.py ar run_agent.py")
            return None

        meta = TaskMetadata.from_loader()
        csv_path = csv_path or (settings.results_dir / "report.csv")
        md_path = md_path or (settings.results_dir / "report.md")

        self.csv_writer.write(csv_path, runs, meta)
        self.md_writer.write(md_path, runs, meta)
        print(f"Sugeneruota:\n  {md_path}\n  {csv_path}")
        return md_path, csv_path
